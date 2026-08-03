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
RESULTADOS = [
    "No atendió",
    "Volver a llamar",
    "Contactado - interesado",
    "Contactado - no interesado",
    "Pedido cargado",
    "Datos actualizados",
    "Número equivocado / no existe",
    "No molestar / baja",
]

# Mapeo de la última disposición → estado del lead (para el pipeline).
_ESTADO_POR_RESULTADO = {
    "No atendió": "Pendiente",
    "Volver a llamar": "En seguimiento",
    "Contactado - interesado": "En seguimiento",
    "Contactado - no interesado": "Descartado",
    "Pedido cargado": "Compró",
    "Datos actualizados": "En seguimiento",
    "Número equivocado / no existe": "Datos inválidos",
    "No molestar / baja": "No contactar",
}


def registrar_actividad(gsheets_section: dict, fila: dict, timestamp: str) -> None:
    """Agrega una fila de actividad (append-only) al Sheet.

    `fila` debe traer al menos `documento`. El resto de las columnas se
    completan con "" / 0 si faltan. `timestamp` lo pasa el caller (la app
    usa datetime.now()) para mantener esta función testeable.
    """
    doc = str(fila.get("documento") or "").strip()
    if not doc:
        raise ValueError("registrar_actividad: falta `documento`.")

    sh = gsheets._open_sheet(gsheets_section)
    ws = gsheets._ensure_worksheet(sh, TAB_ACTIVIDAD, cols=len(ACTIVIDAD_COLS))

    header = ws.row_values(1)
    if not header or header[: len(ACTIVIDAD_COLS)] != ACTIVIDAD_COLS:
        ws.update("A1", [ACTIVIDAD_COLS], value_input_option="RAW")

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


def leer_actividad(gsheets_section: dict) -> pd.DataFrame:
    """Lee todo el historial de actividad. DataFrame vacío con el schema
    correcto si la tab no existe o está vacía. Reintenta ante 429."""
    return gsheets.reintentar_lectura(
        lambda: _leer_actividad(gsheets._open_sheet(gsheets_section)))


def _leer_actividad(sh) -> pd.DataFrame:
    ws = gsheets._ensure_worksheet(sh, TAB_ACTIVIDAD, cols=len(ACTIVIDAD_COLS))
    filas = ws.get_all_values()
    if not filas:
        ws.update("A1", [ACTIVIDAD_COLS], value_input_option="RAW")
        return pd.DataFrame(columns=ACTIVIDAD_COLS)
    header = filas[0]
    if header[: len(ACTIVIDAD_COLS)] != ACTIVIDAD_COLS:
        if all(not c for c in header):
            ws.update("A1", [ACTIVIDAD_COLS], value_input_option="RAW")
            return pd.DataFrame(columns=ACTIVIDAD_COLS)
        raise GsheetsError(
            f"Encabezados inesperados en '{TAB_ACTIVIDAD}': {header}"
        )
    if len(filas) < 2:
        return pd.DataFrame(columns=ACTIVIDAD_COLS)
    df = pd.DataFrame(filas[1:], columns=ACTIVIDAD_COLS[: len(filas[0])])
    df["documento"] = df["documento"].astype(str).str.strip()
    df["monto_pedido"] = pd.to_numeric(df["monto_pedido"], errors="coerce").fillna(0.0)
    return df


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

    sh = gsheets._open_sheet(gsheets_section)
    ws = gsheets._ensure_worksheet(sh, TAB_IMPORTACIONES, cols=len(IMPORTACIONES_COLS))
    header = ws.row_values(1)
    # Migra solo el encabezado viejo de 6 columnas al de 8.
    if not header or header[: len(IMPORTACIONES_COLS)] != IMPORTACIONES_COLS:
        ws.update("A1", [IMPORTACIONES_COLS], value_input_option="RAW")

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
    """Lee actividad + importaciones abriendo el Sheet UNA sola vez.

    Existe por cuota: cada apertura del spreadsheet gasta una lectura, y
    Sheets permite 60 por minuto para el Service Account que comparten
    todas las apps GSU. Leer las dos tabs juntas baja de ~6 lecturas por
    corrida a ~4, y hace que el reintento cubra a las dos de una.

    Devuelve (df_actividad, df_importaciones).
    """
    def _leer():
        sh = gsheets._open_sheet(gsheets_section)
        return _leer_actividad(sh), _leer_importaciones(sh)
    return gsheets.reintentar_lectura(_leer)


def _leer_importaciones(sh) -> pd.DataFrame:
    ws = gsheets._ensure_worksheet(sh, TAB_IMPORTACIONES, cols=len(IMPORTACIONES_COLS))
    filas = ws.get_all_values()
    if not filas:
        ws.update("A1", [IMPORTACIONES_COLS], value_input_option="RAW")
        return pd.DataFrame(columns=IMPORTACIONES_COLS)
    header = filas[0]
    if header[: len(IMPORTACIONES_COLS)] != IMPORTACIONES_COLS:
        if all(not c for c in header):
            ws.update("A1", [IMPORTACIONES_COLS], value_input_option="RAW")
            return pd.DataFrame(columns=IMPORTACIONES_COLS)
        # Las listas subidas antes de agosto 2026 tienen el schema de 6
        # columnas: se leen igual y las dos nuevas quedan vacías.
        if header[: len(_IMPORTACIONES_COLS_V1)] != _IMPORTACIONES_COLS_V1:
            raise GsheetsError(f"Encabezados inesperados en '{TAB_IMPORTACIONES}'.")
    if len(filas) < 2:
        return pd.DataFrame(columns=IMPORTACIONES_COLS)
    # Normalizar el ancho: las filas viejas traen 6 valores y las nuevas 8.
    ancho = len(IMPORTACIONES_COLS)
    datos = [(f + [""] * ancho)[:ancho] for f in filas[1:]]
    df = pd.DataFrame(datos, columns=IMPORTACIONES_COLS)
    df["documento"] = df["documento"].astype(str).str.strip()
    return df


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


def estado_actual_por_lead(df_actividad: pd.DataFrame) -> pd.DataFrame:
    """Deriva el estado actual de cada lead desde su ÚLTIMA gestión.

    Pura: recibe el DataFrame de actividad y devuelve uno indexado por la
    CLAVE del documento (`televentas_data.clave_documento`, no el documento
    crudo) con columnas:
      estado, ultima_gestion, ultimo_resultado, proximo_seguimiento,
      num_contactos, ultima_nota, pedidos_generados, monto_generado.

    El índice es la clave y no el documento porque las filas viejas de la
    Sheet perdieron el cero inicial: si se cruzara por el string crudo, las
    gestiones de esos clientes no volverían nunca. Quien mergee contra los
    leads tiene que mapear su columna `documento` con la misma función.
    """
    cols = [
        "estado", "ultima_gestion", "ultimo_resultado", "proximo_seguimiento",
        "num_contactos", "ultima_nota", "pedidos_generados", "monto_generado",
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
        filas.append({
            "clave_documento": clave,
            "estado": _ESTADO_POR_RESULTADO.get(resultado, "En seguimiento"),
            "ultima_gestion": ult.get("_ts"),
            "ultimo_resultado": resultado,
            "proximo_seguimiento": str(ult.get("proximo_seguimiento") or ""),
            "num_contactos": int(len(g)),
            "ultima_nota": str(ult.get("nota") or ""),
            "pedidos_generados": pedidos,
            "monto_generado": round(monto, 2),
        })
    return pd.DataFrame(filas).set_index("clave_documento")
