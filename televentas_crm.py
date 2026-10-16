"""
televentas_crm.py — Persistencia del CRM de Televentas en Google Sheets.

Contabilium no guarda la gestión comercial (llamadas, resultados,
seguimientos, notas). Esa capa vive acá, en una Google Sheet dedicada
(sección `[gsheets_televentas]` en secrets), append-only.

Modelo:
  - Tab `actividad_televentas`: una fila por interacción (llamada /
    WhatsApp). Append-only: nunca se pisa; el estado actual de cada lead
    se DERIVA de su última fila (`estado_actual_por_lead`). Esto hace la
    persistencia simple y auditable (igual patrón que el histórico de
    costos de Contabilidad y el log de comisiones).

Se apoya en los helpers genéricos de credenciales/apertura de `gsheets`
(mismo Service Account del resto de las apps GSU).
"""

from __future__ import annotations

from datetime import date, timedelta

import gspread
import pandas as pd

import gsheets  # reutiliza _open_sheet / _ensure_worksheet / errores
import televentas_data  # clave_documento (cruce tolerante al cero inicial)

GsheetsError = gsheets.GsheetsError

# Todo lo que se escribe va con RAW, NO con USER_ENTERED.
# USER_ENTERED le pide a Sheets que interprete el valor "como si lo tipeara
# una persona", y eso rompía dos cosas en silencio:
#   - los RUT todo-dígitos pasaban a NÚMERO y perdían el cero inicial
#     ("012345680017" → "12345680017"), con lo cual el lead nunca volvía a
#     cruzar y quedaba "Sin gestionar" para siempre (15,8% de la base);
#   - las fechas pasaban a DATE y volvían con el formato regional de la
#     Sheet, rompiendo la comparación contra "YYYY-MM-DD".
# RAW guarda exactamente el string que mandamos.
_ESCRITURA = "RAW"

TAB_ACTIVIDAD = "actividad_televentas"
TAB_IMPORTACIONES = "importaciones_televentas"

# Cada fila = un cliente perteneciente a una lista importada (nombrada).
# Append-only; una importación se identifica por su `nombre`.
IMPORTACIONES_COLS = [
    "nombre",         # nombre de la lista, ej "SELECCIONADOS POR ERNESTO 04 07 26"
    "documento",      # RUT/CI del cliente (join con los leads)
    "codigo",         # código Contabilium (informativo)
    "razon_social",   # informativo
    "fecha_carga",    # "YYYY-MM-DD HH:MM"
    "agente",         # quién la subió
    "comentario",     # lo que anotó el vendedor de calle sobre ese cliente
    "problema_pago",  # "1" si la planilla lo marcó como problema de pago
]

# Schema original (sin `comentario` / `problema_pago`). Las listas subidas
# antes de agosto 2026 están escritas con estas 6 columnas; se leen igual y
# el encabezado se migra solo en la próxima escritura.
_IMPORTACIONES_COLS_V1 = IMPORTACIONES_COLS[:6]

ACTIVIDAD_COLS = [
    "timestamp",            # "YYYY-MM-DD HH:MM" — cuándo se registró
    "documento",            # RUT/CI del cliente (join con los leads)
    "razon_social",         # copia para lectura directa del Sheet
    "agente",               # quién gestionó (una sola por ahora)
    "canal",                # Llamada / WhatsApp
    "resultado",            # disposición controlada (ver RESULTADOS)
    "nota",                 # texto libre
    "proximo_seguimiento",  # "YYYY-MM-DD" o "" — cuándo rellamar
    "monto_pedido",         # $ del pedido si se cargó (0 si no)
    "nro_orden",            # Nº de orden de Contabilium si se cargó
]

# Disposiciones posibles de una gestión. Vocabulario controlado para que
# el tablero de actividad sea consistente.
#
# Se AGREGAN valores, nunca se renombran: la Sheet guarda el texto literal y
# las filas viejas se comparan por igualdad. Y lo que es un contacto efectivo
# tiene que empezar con "Contactado" (el tablero de Actividad los cuenta así).
RESULTADOS = [
    "No atendió",
    "WhatsApp enviado",
    "Volver a llamar",
    "Contactado - interesado",
    "Contactado - no necesita ahora",
    "Contactado - pide visita",
    "Contactado - no interesado",
    "Pedido cargado",
    "Datos actualizados",
    "Sin respuesta - derivado a vendedor",
    "Vendedor respondió / lo visitó",
    "No reactiva - pausar",
    "Número equivocado / no existe",
    "No molestar / baja",
]

# Mapeo de la última disposición → estado del lead (para el pipeline).
_ESTADO_POR_RESULTADO = {
    "No atendió": "Pendiente",
    "WhatsApp enviado": "En seguimiento",
    "Volver a llamar": "En seguimiento",
    "Contactado - interesado": "En seguimiento",
    "Contactado - no necesita ahora": "En seguimiento",
    "Contactado - pide visita": "Derivado a vendedor",
    "Contactado - no interesado": "Descartado",
    "Pedido cargado": "Compró",
    "Datos actualizados": "En seguimiento",
    "Sin respuesta - derivado a vendedor": "Derivado a vendedor",
    "Vendedor respondió / lo visitó": "En seguimiento",
    "No reactiva - pausar": "Pausado",
    "Número equivocado / no existe": "Datos inválidos",
    "No molestar / baja": "No contactar",
}


# =====================================================================
# Circuito de llamado a inactivos (diagrama de Ernesto y Gustavo, oct-2026)
# =====================================================================
#
# Los plazos son los del diagrama, pasados de horas a días:
#   - no atiende → 2º intento de llamada a las 24 h;
#   - no atiende el 2º → WhatsApp a las 24 h;
#   - después, cinco esperas de respuesta: 24 h, 48 h, 72 h, 24 h y 24 h;
#   - sin respuesta a la quinta → se deriva por mail al vendedor / jefe, que
#     tiene 15 días para contestarle a televentas;
#   - si atendió (con o sin pedido) → llamada de control a los 15 días.
# La lectura de las cinco esperas como "reenviar WhatsApp en cada una" está
# anotada como a confirmar en _learning/decisions.md (2026-10-16).
DIAS_SEGUNDO_LLAMADO = 1
ESPERAS_WHATSAPP = [1, 2, 3, 1, 1]
DIAS_LLAMADA_CONTROL = 15
DIAS_RESPUESTA_VENDEDOR = 15

# Opciones de pausa para "No reactiva - pausar". El diagrama dice "no tenerlo
# en cuenta por un tiempo" sin decir cuánto: lo elige quien registra.
PAUSAS_DIAS = [30, 60, 90, 180]

_CONTACTO_CON_CONTROL = {
    "Contactado - interesado", "Contactado - no necesita ahora",
    "Pedido cargado", "Datos actualizados",
    # No es un contacto con el cliente (por eso no empieza con "Contactado"),
    # pero el diagrama sigue igual: ver si reactiva.
    "Vendedor respondió / lo visitó",
}
RESPUESTA_VENDEDOR = "Vendedor respondió / lo visitó"
_DERIVADOS = {"Contactado - pide visita", "Sin respuesta - derivado a vendedor"}
_SIN_PASO = {"Contactado - no interesado", "Número equivocado / no existe",
             "No molestar / baja"}

PASO_INICIAL = "📞 Llamar (1er intento)"

# Desde cuándo rige el circuito. A una gestión anterior no se le calcula
# fecha: se anotó sin saber que habría circuito, y aplicárselo para atrás
# llenaba "Seguimientos de hoy" de atrasados de julio (75 el día del
# lanzamiento). La fecha que alguien cargó a mano sí se respeta, como antes.
# Decisión de Mariano, 2026-10-16.
INICIO_CIRCUITO = date(2026, 10, 16)


def _a_fecha(valor) -> date | None:
    """'YYYY-MM-DD…' / Timestamp / date → date. None si no se puede leer."""
    if valor is None or valor == "":
        return None
    ts = pd.to_datetime(valor, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.date()


def _racha_final(resultados: list[str], valor: str) -> int:
    """Cuántas de las últimas gestiones seguidas tienen ese resultado."""
    n = 0
    for r in reversed(resultados):
        if r != valor:
            break
        n += 1
    return n


def siguiente_paso(gestiones: pd.DataFrame) -> dict:
    """Qué toca hacer con un cliente según el circuito, y para cuándo.

    Pura. `gestiones` son las filas de actividad de UN cliente, en cualquier
    orden. Devuelve {"accion": str, "fecha": "YYYY-MM-DD" | ""}.

    La fecha que cargó a mano quien registró (`proximo_seguimiento` de la
    última gestión) gana sobre la calculada: el circuito propone, la persona
    decide. Un resultado que cierra el circuito (no interesado, número
    equivocado, baja) devuelve acción vacía.
    """
    if gestiones is None or gestiones.empty:
        return {"accion": PASO_INICIAL, "fecha": ""}

    g = gestiones.copy()
    g["_ts"] = pd.to_datetime(g["timestamp"], errors="coerce")
    g = g.sort_values("_ts", kind="stable")
    resultados = [str(r or "") for r in g["resultado"]]
    ult = g.iloc[-1]
    res = resultados[-1]
    base = _a_fecha(ult["_ts"])
    manual = _a_fecha(ult.get("proximo_seguimiento"))

    def _mas(dias: int) -> str:
        return (base + timedelta(days=dias)).isoformat() if base else ""

    if res == "No atendió":
        if _racha_final(resultados, "No atendió") == 1:
            accion, fecha = "📞 2º intento de llamada", _mas(DIAS_SEGUNDO_LLAMADO)
        else:
            accion, fecha = "💬 Mandar WhatsApp", _mas(DIAS_SEGUNDO_LLAMADO)
    elif res == "WhatsApp enviado":
        n = _racha_final(resultados, "WhatsApp enviado")
        espera = ESPERAS_WHATSAPP[min(n, len(ESPERAS_WHATSAPP)) - 1]
        if n < len(ESPERAS_WHATSAPP):
            accion = (f"💬 ¿Respondió? Si no, reenviar WhatsApp "
                      f"({n + 1}º de {len(ESPERAS_WHATSAPP)})")
        else:
            accion = "✉️ Derivar por mail al vendedor / jefe (no respondió)"
        fecha = _mas(espera)
    elif res in _CONTACTO_CON_CONTROL:
        accion, fecha = "📞 Llamada de control", _mas(DIAS_LLAMADA_CONTROL)
    elif res in _DERIVADOS:
        accion = "📞 Control: ¿el vendedor respondió / lo visitó?"
        fecha = _mas(DIAS_RESPUESTA_VENDEDOR)
    elif res == "No reactiva - pausar":
        accion, fecha = "⏸️ Pausado — volver a llamar al terminar", ""
    elif res in _SIN_PASO:
        return {"accion": "", "fecha": ""}
    else:  # "Volver a llamar" o un texto que no conocemos
        accion, fecha = "📞 Volver a llamar", ""

    if manual:
        fecha = manual.isoformat()
    elif base is None or base < INICIO_CIRCUITO:
        fecha = ""
    return {"accion": accion, "fecha": fecha}


def fecha_pausa(dias: int, hoy: date) -> str:
    """Hasta cuándo queda pausado un cliente que no reactiva."""
    return (hoy + timedelta(days=int(dias))).isoformat()


DERIVACIONES_COLS = [
    "clave_documento", "documento", "razon_social", "agente", "motivo",
    "fecha_derivacion", "vence", "estado", "dias_restantes", "fecha_respuesta",
    "dias_respuesta",
]


def derivaciones(df_actividad: pd.DataFrame, hoy: date) -> pd.DataFrame:
    """Una fila por cada vez que televentas le pasó un cliente al vendedor.

    Pura. El diagrama le da al vendedor `DIAS_RESPUESTA_VENDEDOR` días para
    contestarle a televentas. Estado de cada derivación:
      - "Respondida": después se registró «Vendedor respondió / lo visitó».
      - "Reemplazada": antes de la respuesta hubo otra derivación del mismo
        cliente (cuenta la nueva, para no castigar dos veces al vendedor).
      - "Vencida": sin respuesta y pasó el plazo.
      - "Pendiente": sin respuesta y dentro del plazo.
    `dias_restantes` es negativo cuando está vencida.
    """
    if df_actividad is None or df_actividad.empty:
        return pd.DataFrame(columns=DERIVACIONES_COLS)
    df = df_actividad.copy()
    df["_ts"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["_ts"]).sort_values("_ts", kind="stable")
    df["_clave"] = df["documento"].map(televentas_data.clave_documento)

    filas = []
    for clave, g in df.groupby("_clave"):
        g = g.reset_index(drop=True)
        for i, r in g.iterrows():
            if r["resultado"] not in _DERIVADOS:
                continue
            fecha = r["_ts"].date()
            vence = fecha + timedelta(days=DIAS_RESPUESTA_VENDEDOR)
            despues = g.iloc[i + 1:]
            resp = despues[despues["resultado"] == RESPUESTA_VENDEDOR]
            otra = despues[despues["resultado"].isin(_DERIVADOS)]
            f_resp = resp["_ts"].iloc[0] if not resp.empty else None
            f_otra = otra["_ts"].iloc[0] if not otra.empty else None
            if f_resp is not None and (f_otra is None or f_resp <= f_otra):
                estado = "Respondida"
            elif f_otra is not None:
                estado, f_resp = "Reemplazada", None
            elif hoy > vence:
                estado = "Vencida"
            else:
                estado = "Pendiente"
            filas.append({
                "clave_documento": clave,
                "documento": str(r["documento"]),
                "razon_social": str(r.get("razon_social") or ""),
                "agente": str(r.get("agente") or ""),
                "motivo": r["resultado"],
                "fecha_derivacion": fecha.isoformat(),
                "vence": vence.isoformat(),
                "estado": estado,
                "dias_restantes": (vence - hoy).days if estado in ("Pendiente", "Vencida") else None,
                "fecha_respuesta": f_resp.date().isoformat() if f_resp is not None else "",
                "dias_respuesta": (f_resp.date() - fecha).days if f_resp is not None else None,
            })
    if not filas:
        return pd.DataFrame(columns=DERIVACIONES_COLS)
    return pd.DataFrame(filas)[DERIVACIONES_COLS]


# =====================================================================
# ¿Compraron después del contacto?
# =====================================================================

REACTIVACION_COLS = [
    "clave_documento", "documento", "primer_contacto", "gestiones",
    "contacto_efectivo", "intentos_hasta_contacto", "pedido_televentas",
    "estado", "fecha_compra", "dias_hasta_compra", "monto_ventana",
    "sin_compras_previas",
]


def es_contacto_efectivo(resultado: str) -> bool:
    """Hablamos con el cliente. Mismo criterio que el tablero de Actividad."""
    r = str(resultado or "")
    return r.startswith("Contactado") or r == "Pedido cargado"


def reactivacion(
    df_actividad: pd.DataFrame,
    df_compras: pd.DataFrame,
    hoy: date,
    desde: date,
    dias_ventana: int,
    dias_inactivo: int = 90,
    datos_desde: date | None = None,
) -> pd.DataFrame:
    """Por cliente trabajado: ¿compró en los días siguientes al primer contacto?

    Pura. Un cliente cuenta desde su PRIMERA gestión con fecha >= `desde`
    (cualquier resultado, también "No atendió": es cuando se lo empezó a
    trabajar). `df_compras` son facturas (documento, fecha, monto) de
    cualquier vendedor: el encabezado de Contabilium no dice quién vendió,
    así que esto NO atribuye la venta a televentas.

    estado:
      - "Compró": factura con fecha POSTERIOR al día del primer contacto y
        dentro de `dias_ventana` (el mismo día no cuenta: puede ser una
        compra que ya venía).
      - "En ventana": no compró todavía y la ventana no terminó. No es un
        "no": se excluye del porcentaje.
      - "No compró": terminó la ventana sin factura.
    sin_compras_previas: True/False si no hubo facturas en los
    `dias_inactivo` días anteriores al contacto; None si los datos cargados
    (`datos_desde`) no alcanzan para saberlo.
    """
    if df_actividad is None or df_actividad.empty:
        return pd.DataFrame(columns=REACTIVACION_COLS)
    act = df_actividad.copy()
    act["_ts"] = pd.to_datetime(act["timestamp"], errors="coerce")
    act = act.dropna(subset=["_ts"])
    act = act[act["_ts"].dt.date >= desde].sort_values("_ts", kind="stable")
    if act.empty:
        return pd.DataFrame(columns=REACTIVACION_COLS)
    act["_clave"] = act["documento"].map(televentas_data.clave_documento)

    comp = (df_compras.copy() if df_compras is not None and not df_compras.empty
            else pd.DataFrame(columns=["documento", "fecha", "monto"]))
    comp["_clave"] = comp["documento"].map(televentas_data.clave_documento)
    comp["_dia"] = pd.to_datetime(comp["fecha"], errors="coerce").dt.date
    compras_por = {k: g for k, g in comp.groupby("_clave")}

    filas = []
    for clave, g in act.groupby("_clave"):
        primero = g["_ts"].iloc[0].date()
        fin = primero + timedelta(days=int(dias_ventana))
        en_ventana = g[g["_ts"].dt.date <= fin]
        efectivos = [i for i, r in enumerate(g["resultado"]) if es_contacto_efectivo(r)]
        c = compras_por.get(clave)
        posteriores = (c[(c["_dia"] > primero) & (c["_dia"] <= fin)]
                       if c is not None else pd.DataFrame())
        if not posteriores.empty:
            estado = "Compró"
            f_compra = min(posteriores["_dia"])
        elif fin >= hoy:
            estado, f_compra = "En ventana", None
        else:
            estado, f_compra = "No compró", None
        inicio_prev = primero - timedelta(days=int(dias_inactivo))
        if datos_desde is not None and inicio_prev < datos_desde:
            sin_prev = None
        else:
            sin_prev = bool(c is None or c[(c["_dia"] >= inicio_prev)
                                           & (c["_dia"] < primero)].empty)
        filas.append({
            "clave_documento": clave,
            "documento": str(g["documento"].iloc[0]),
            "primer_contacto": primero.isoformat(),
            "gestiones": int(len(g)),
            "contacto_efectivo": bool(efectivos),
            "intentos_hasta_contacto": (efectivos[0] + 1) if efectivos else None,
            "pedido_televentas": bool((en_ventana["resultado"] == "Pedido cargado").any()),
            "estado": estado,
            "fecha_compra": f_compra.isoformat() if f_compra else "",
            "dias_hasta_compra": (f_compra - primero).days if f_compra else None,
            "monto_ventana": round(float(posteriores["monto"].sum()), 2)
                             if not posteriores.empty else 0.0,
            "sin_compras_previas": sin_prev,
        })
    return pd.DataFrame(filas)[REACTIVACION_COLS]


def gestiones_de(df_actividad: pd.DataFrame, documento) -> pd.DataFrame:
    """Las gestiones de un cliente, cruzando por clave (tolera el cero inicial)."""
    if df_actividad is None or df_actividad.empty:
        return pd.DataFrame(columns=ACTIVIDAD_COLS)
    clave = televentas_data.clave_documento(documento)
    return df_actividad[
        df_actividad["documento"].map(televentas_data.clave_documento) == clave]


def registrar_actividad(gsheets_section: dict, fila: dict, timestamp: str) -> None:
    """Agrega una fila de actividad (append-only) al Sheet.

    `fila` debe traer al menos `documento`. El resto de las columnas se
    completan con "" / 0 si faltan. `timestamp` lo pasa el caller (la app
    usa datetime.now()) para mantener esta función testeable.
    """
    doc = str(fila.get("documento") or "").strip()
    if not doc:
        raise ValueError("registrar_actividad: falta `documento`.")

    ws = _preparar_tab(gsheets_section, TAB_ACTIVIDAD, ACTIVIDAD_COLS)

    fila_out = {**fila, "timestamp": timestamp}
    valores = [
        str(fila_out.get("timestamp") or ""),
        doc,
        str(fila_out.get("razon_social") or ""),
        str(fila_out.get("agente") or ""),
        str(fila_out.get("canal") or ""),
        str(fila_out.get("resultado") or ""),
        str(fila_out.get("nota") or ""),
        str(fila_out.get("proximo_seguimiento") or ""),
        float(fila_out.get("monto_pedido") or 0.0),
        str(fila_out.get("nro_orden") or ""),
    ]
    ws.append_row(valores, value_input_option=_ESCRITURA)


def _preparar_tab(gsheets_section: dict, tab: str, columnas: list[str]):
    """Abre el Sheet y devuelve la worksheet lista para escribirle.

    Todo lo que hace es LEER (abrir + buscar la tab + mirar el encabezado)
    más, si hace falta, escribir el encabezado —que es idempotente porque
    siempre son los mismos valores en el mismo rango—. Por eso se puede
    reintentar entero ante un 429.

    El append que viene DESPUÉS queda afuera del reintento a propósito: un
    append repetido duplica filas. Separarlos es lo que permite absorber el
    429 de cuota de lectura, que es donde fallaban los guardados.
    """
    def _prep():
        sh = gsheets._open_sheet(gsheets_section)
        ws = gsheets._ensure_worksheet(sh, tab, cols=len(columnas))
        header = ws.row_values(1)
        if not header or header[: len(columnas)] != columnas:
            ws.update("A1", [columnas], value_input_option=_ESCRITURA)
        return ws
    return gsheets.reintentar_lectura(_prep)


def _tabla_desde_valores(filas, columnas, tab, columnas_previas=None):
    """Arma el DataFrame del schema `columnas` desde lo que devolvió Sheets.

    Las filas pueden venir cortas: `values_batch_get` omite las celdas
    vacías del final, así que cada una se rellena hasta el ancho del schema.

    Devuelve None si la tab está vacía o sin encabezado — el caller decide
    si la inicializa. Levanta si el encabezado es otro (schema desconocido),
    salvo que coincida con `columnas_previas` (schema viejo tolerado).
    """
    if not filas:
        return None
    header = filas[0]
    if header[: len(columnas)] != columnas:
        if all(not c for c in header):
            return None
        if not (columnas_previas
                and header[: len(columnas_previas)] == columnas_previas):
            raise GsheetsError(f"Encabezados inesperados en '{tab}': {header}")
    if len(filas) < 2:
        return pd.DataFrame(columns=columnas)
    ancho = len(columnas)
    datos = [(list(f) + [""] * ancho)[:ancho] for f in filas[1:]]
    return pd.DataFrame(datos, columns=columnas)


def _normalizar_actividad(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["documento"] = df["documento"].astype(str).str.strip()
    df["monto_pedido"] = pd.to_numeric(df["monto_pedido"], errors="coerce").fillna(0.0)
    return df


def _normalizar_importaciones(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["documento"] = df["documento"].astype(str).str.strip()
    return df


def leer_actividad(gsheets_section: dict) -> pd.DataFrame:
    """Lee todo el historial de actividad. DataFrame vacío con el schema
    correcto si la tab no existe o está vacía. Reintenta ante 429."""
    return gsheets.reintentar_lectura(
        lambda: _leer_actividad(gsheets._open_sheet(gsheets_section)))


def _leer_actividad(sh) -> pd.DataFrame:
    """Camino lento: una lectura de metadata + una de valores. Crea la tab
    y le pone el encabezado si hace falta."""
    ws = gsheets._ensure_worksheet(sh, TAB_ACTIVIDAD, cols=len(ACTIVIDAD_COLS))
    df = _tabla_desde_valores(ws.get_all_values(), ACTIVIDAD_COLS, TAB_ACTIVIDAD)
    if df is None:
        ws.update("A1", [ACTIVIDAD_COLS], value_input_option=_ESCRITURA)
        df = pd.DataFrame(columns=ACTIVIDAD_COLS)
    return _normalizar_actividad(df)


def guardar_importacion(
    gsheets_section: dict, nombre: str, filas: list[dict],
    agente: str, timestamp: str,
) -> int:
    """Guarda una lista importada (append). `filas` = list de
    {documento, codigo, razon_social, comentario, problema_pago}.
    Devuelve cuántas filas escribió.

    Append-only a propósito: volver a subir una lista con el mismo nombre no
    pisa nada, y los lectores se quedan con el dato más reciente de cada
    cliente. Así re-importar la planilla (por ejemplo para sumarle los
    comentarios) es seguro y queda el rastro de qué se subió cuándo.
    """
    nombre = (nombre or "").strip()
    if not nombre:
        raise ValueError("La importación necesita un nombre.")
    filas_validas = [f for f in filas if str(f.get("documento") or "").strip()]
    if not filas_validas:
        return 0

    # Migra solo el encabezado viejo de 6 columnas al de 8. Reintenta ante
    # 429; el append de abajo NO (duplicaría filas).
    ws = _preparar_tab(gsheets_section, TAB_IMPORTACIONES, IMPORTACIONES_COLS)

    rows = [[nombre, str(f.get("documento") or "").strip(),
             str(f.get("codigo") or ""), str(f.get("razon_social") or ""),
             timestamp, agente or "",
             str(f.get("comentario") or "").strip(),
             "1" if f.get("problema_pago") else ""] for f in filas_validas]
    ws.append_rows(rows, value_input_option=_ESCRITURA)
    return len(rows)


def leer_importaciones(gsheets_section: dict) -> pd.DataFrame:
    """Lee todas las listas importadas. DataFrame vacío con schema si no
    hay. Reintenta ante 429."""
    return gsheets.reintentar_lectura(
        lambda: _leer_importaciones(gsheets._open_sheet(gsheets_section)))


def leer_crm(gsheets_section: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Lee actividad + importaciones en DOS llamadas a la API en total.

    Existe por cuota: Sheets da 60 lecturas por minuto al Service Account
    que comparten TODAS las apps GSU. El camino obvio gastaba 5 lecturas
    por corrida (abrir + metadata de cada tab + valores de cada tab),
    porque `sh.worksheet()` de gspread se trae la metadata entera cada vez
    que se lo llama. Con `values_batch_get` las dos tabs vienen en una sola
    llamada: abrir (1) + valores (1) = 2.

    Si alguna tab todavía no existe o está sin encabezado, cae al camino
    lento —que las crea— y ahí sí gasta más. Pasa una sola vez.

    Devuelve (df_actividad, df_importaciones).
    """
    def _leer():
        sh = gsheets._open_sheet(gsheets_section)
        try:
            resp = sh.values_batch_get([TAB_ACTIVIDAD, TAB_IMPORTACIONES])
        except gspread.exceptions.APIError as e:
            if gsheets.es_rate_limit(e):
                raise            # que lo reintente el wrapper
            return _leer_actividad(sh), _leer_importaciones(sh)
        rangos = resp.get("valueRanges") or []
        crudo_act = (rangos[0].get("values") if len(rangos) > 0 else None) or []
        crudo_imp = (rangos[1].get("values") if len(rangos) > 1 else None) or []

        df_act = _tabla_desde_valores(crudo_act, ACTIVIDAD_COLS, TAB_ACTIVIDAD)
        df_imp = _tabla_desde_valores(crudo_imp, IMPORTACIONES_COLS,
                                      TAB_IMPORTACIONES, _IMPORTACIONES_COLS_V1)
        # None = tab vacía o sin encabezado → hay que inicializarla, y eso
        # necesita la worksheet.
        if df_act is None:
            df_act = _leer_actividad(sh)
        else:
            df_act = _normalizar_actividad(df_act)
        if df_imp is None:
            df_imp = _leer_importaciones(sh)
        else:
            df_imp = _normalizar_importaciones(df_imp)
        return df_act, df_imp

    return gsheets.reintentar_lectura(_leer)


def _leer_importaciones(sh) -> pd.DataFrame:
    """Camino lento: metadata + valores. Crea la tab si no existe."""
    ws = gsheets._ensure_worksheet(sh, TAB_IMPORTACIONES, cols=len(IMPORTACIONES_COLS))
    df = _tabla_desde_valores(ws.get_all_values(), IMPORTACIONES_COLS,
                              TAB_IMPORTACIONES, _IMPORTACIONES_COLS_V1)
    if df is None:
        ws.update("A1", [IMPORTACIONES_COLS], value_input_option=_ESCRITURA)
        df = pd.DataFrame(columns=IMPORTACIONES_COLS)
    return _normalizar_importaciones(df)


def reparar_documentos(
    gsheets_section: dict, documentos_validos, *, dry_run: bool = False,
) -> dict[str, dict[str, int]]:
    """Reescribe los documentos que Sheets mutiló al guardarlos como número.

    Las filas escritas con `USER_ENTERED` (antes del fix) perdieron el cero
    inicial del RUT. Esta función recorre las dos tabs del CRM y, para cada
    documento cuya CLAVE coincide con la de un cliente real, vuelve a
    escribir el documento completo — con RAW, así queda como texto.

    `documentos_validos`: los documentos reales, tal como vienen de
    Contabilium (típicamente `leads["documento"]`).

    Solo toca la columna `documento`: ninguna otra celda se reescribe. Es
    idempotente (una segunda corrida repara 0) y conservadora: si un
    documento del Sheet no matchea ningún cliente actual, se deja como está.

    Devuelve {tab: {"filas": n, "reparadas": n}}. Con `dry_run=True`
    cuenta lo que haría sin escribir nada.
    """
    por_clave: dict[str, str] = {}
    for d in documentos_validos:
        s = str(d or "").strip()
        if s:
            # setdefault: si dos clientes colapsaran en la misma clave, no
            # inventamos un ganador nuevo en cada corrida.
            por_clave.setdefault(televentas_data.clave_documento(s), s)

    sh = gsheets._open_sheet(gsheets_section)
    reporte: dict[str, dict[str, int]] = {}

    for tab, columnas in ((TAB_IMPORTACIONES, IMPORTACIONES_COLS),
                          (TAB_ACTIVIDAD, ACTIVIDAD_COLS)):
        ws = gsheets._ensure_worksheet(sh, tab, cols=len(columnas))
        filas = ws.get_all_values()
        if len(filas) < 2:
            reporte[tab] = {"filas": 0, "reparadas": 0}
            continue

        idx = columnas.index("documento")
        actuales = [(f[idx] if len(f) > idx else "") for f in filas[1:]]
        nuevos, reparadas = [], 0
        for valor in actuales:
            s = str(valor).strip()
            real = por_clave.get(televentas_data.clave_documento(s), s)
            if real != s:
                reparadas += 1
            nuevos.append(real)

        if reparadas and not dry_run:
            rango = "{}:{}".format(
                gspread.utils.rowcol_to_a1(2, idx + 1),
                gspread.utils.rowcol_to_a1(len(nuevos) + 1, idx + 1),
            )
            ws.update(rango, [[v] for v in nuevos], value_input_option=_ESCRITURA)

        reporte[tab] = {"filas": len(actuales), "reparadas": reparadas}

    return reporte


def nombres_importaciones(df_imp: pd.DataFrame) -> list[str]:
    """Nombres de listas importadas, más recientes primero."""
    if df_imp is None or df_imp.empty:
        return []
    orden = df_imp.drop_duplicates("nombre", keep="last")["nombre"].tolist()
    return list(reversed(orden))


def documentos_de_importacion(df_imp: pd.DataFrame, nombre: str) -> set[str]:
    """Set de CLAVES de documento que pertenecen a la lista `nombre`.

    Devuelve claves normalizadas y no los strings crudos por dos razones:
    así el set cruza contra los leads aunque la fila vieja haya perdido el
    cero inicial, y así un mismo cliente que quedó escrito de las dos
    formas (mutilado y entero) cuenta una sola vez — si no, el total de la
    lista aparecería inflado.
    """
    if df_imp is None or df_imp.empty:
        return set()
    docs = df_imp.loc[df_imp["nombre"] == nombre, "documento"].astype(str)
    return {televentas_data.clave_documento(d) for d in docs if str(d).strip()}


NOTAS_COLS = ["comentario", "problema_pago", "nota_lista", "nota_fecha"]


def notas_por_lead(df_imp: pd.DataFrame) -> pd.DataFrame:
    """Consolida, por cliente, lo que anotó el vendedor de calle.

    Junta el `comentario` y el flag `problema_pago` de TODAS las listas
    importadas —no solo la que se está trabajando—, porque son propiedades
    del cliente, no de la lista: si Ernesto marcó a alguien como problema de
    pago, la operadora tiene que verlo aunque esté llamando desde otra lista.

    Pura. Devuelve un DataFrame indexado por `clave_documento` con columnas
    NOTAS_COLS. `nota_lista`/`nota_fecha` dicen de dónde salió el dato, para
    que se pueda juzgar si está viejo.

    Criterio: gana el último valor NO VACÍO de cada campo. Es decir, una
    lista posterior que no traiga la columna no borra lo que ya se sabía —
    para una alerta, quedarse con un aviso viejo es más barato que perderlo.
    La contra: no se puede des-marcar a alguien re-importándolo sin la marca;
    hay que editar la Sheet.
    """
    if df_imp is None or df_imp.empty:
        return pd.DataFrame(columns=NOTAS_COLS)

    df = df_imp.copy()
    for c in ("comentario", "problema_pago", "nombre", "fecha_carga"):
        if c not in df.columns:
            df[c] = ""
    df["_clave"] = df["documento"].map(televentas_data.clave_documento)
    # Orden cronológico: "YYYY-MM-DD HH:MM" ordena bien como texto.
    df = df.sort_values("fecha_carga", kind="stable")

    acum: dict[str, dict] = {}
    for _, r in df.iterrows():
        clave = r["_clave"]
        if not clave:
            continue
        info = acum.setdefault(
            clave, {"comentario": "", "problema_pago": False,
                    "nota_lista": "", "nota_fecha": ""})
        comentario = str(r.get("comentario") or "").strip()
        problema = televentas_data.es_marca_positiva(r.get("problema_pago"))
        if comentario:
            info["comentario"] = comentario
        if problema:
            info["problema_pago"] = True
        if comentario or problema:
            info["nota_lista"] = str(r.get("nombre") or "")
            info["nota_fecha"] = str(r.get("fecha_carga") or "")

    filas = [{"clave_documento": k, **v} for k, v in acum.items()
             if v["comentario"] or v["problema_pago"]]
    if not filas:
        return pd.DataFrame(columns=NOTAS_COLS)
    return pd.DataFrame(filas).set_index("clave_documento")[NOTAS_COLS]


def estado_actual_por_lead(df_actividad: pd.DataFrame,
                           hoy: date | None = None) -> pd.DataFrame:
    """Deriva el estado actual de cada lead desde su ÚLTIMA gestión.

    Pura: recibe el DataFrame de actividad y devuelve uno indexado por la
    CLAVE del documento (`televentas_data.clave_documento`, no el documento
    crudo) con columnas:
      estado, ultima_gestion, ultimo_resultado, proximo_seguimiento,
      num_contactos, ultima_nota, pedidos_generados, monto_generado,
      proxima_accion, proxima_fecha (ver `siguiente_paso`).

    Con `hoy`, un cliente pausado cuya pausa ya terminó vuelve a "Pendiente"
    (sin `hoy` queda "Pausado": la función no mira el reloj por su cuenta).

    El índice es la clave y no el documento porque las filas viejas de la
    Sheet perdieron el cero inicial: si se cruzara por el string crudo, las
    gestiones de esos clientes no volverían nunca. Quien mergee contra los
    leads tiene que mapear su columna `documento` con la misma función.
    """
    cols = [
        "estado", "ultima_gestion", "ultimo_resultado", "proximo_seguimiento",
        "num_contactos", "ultima_nota", "pedidos_generados", "monto_generado",
        "proxima_accion", "proxima_fecha",
    ]
    if df_actividad is None or df_actividad.empty:
        return pd.DataFrame(columns=cols)

    df = df_actividad.copy()
    df["_ts"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.sort_values("_ts")
    df["_clave"] = df["documento"].map(televentas_data.clave_documento)

    filas = []
    for clave, g in df.groupby("_clave"):
        ult = g.iloc[-1]
        resultado = str(ult.get("resultado") or "")
        pedidos = int((g["resultado"] == "Pedido cargado").sum())
        monto = float(pd.to_numeric(g["monto_pedido"], errors="coerce").fillna(0).sum())
        paso = siguiente_paso(g)
        estado = _ESTADO_POR_RESULTADO.get(resultado, "En seguimiento")
        if (estado == "Pausado" and hoy is not None
                and (_a_fecha(paso["fecha"]) or hoy) <= hoy):
            estado = "Pendiente"
        filas.append({
            "clave_documento": clave,
            "estado": estado,
            "ultima_gestion": ult.get("_ts"),
            "ultimo_resultado": resultado,
            "proximo_seguimiento": str(ult.get("proximo_seguimiento") or ""),
            "num_contactos": int(len(g)),
            "ultima_nota": str(ult.get("nota") or ""),
            "pedidos_generados": pedidos,
            "monto_generado": round(monto, 2),
            "proxima_accion": paso["accion"],
            "proxima_fecha": paso["fecha"],
        })
    return pd.DataFrame(filas).set_index("clave_documento")
