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

import pandas as pd

import gsheets  # reutiliza _open_sheet / _ensure_worksheet / errores

GsheetsError = gsheets.GsheetsError

TAB_ACTIVIDAD = "actividad_televentas"

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
    ws.append_row(valores, value_input_option="USER_ENTERED")


def leer_actividad(gsheets_section: dict) -> pd.DataFrame:
    """Lee todo el historial de actividad. DataFrame vacío con el schema
    correcto si la tab no existe o está vacía."""
    sh = gsheets._open_sheet(gsheets_section)
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


def estado_actual_por_lead(df_actividad: pd.DataFrame) -> pd.DataFrame:
    """Deriva el estado actual de cada lead desde su ÚLTIMA gestión.

    Pura: recibe el DataFrame de actividad y devuelve uno indexado por
    `documento` con columnas:
      estado, ultima_gestion, ultimo_resultado, proximo_seguimiento,
      num_contactos, ultima_nota, pedidos_generados, monto_generado.
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

    filas = []
    for doc, g in df.groupby("documento"):
        ult = g.iloc[-1]
        resultado = str(ult.get("resultado") or "")
        pedidos = int((g["resultado"] == "Pedido cargado").sum())
        monto = float(pd.to_numeric(g["monto_pedido"], errors="coerce").fillna(0).sum())
        filas.append({
            "documento": doc,
            "estado": _ESTADO_POR_RESULTADO.get(resultado, "En seguimiento"),
            "ultima_gestion": ult.get("_ts"),
            "ultimo_resultado": resultado,
            "proximo_seguimiento": str(ult.get("proximo_seguimiento") or ""),
            "num_contactos": int(len(g)),
            "ultima_nota": str(ult.get("nota") or ""),
            "pedidos_generados": pedidos,
            "monto_generado": round(monto, 2),
        })
    return pd.DataFrame(filas).set_index("documento")
