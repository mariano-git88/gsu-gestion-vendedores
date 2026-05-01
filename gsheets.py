"""
gsheets.py — Persistencia del histórico de comisiones en Google Sheets.

Encapsula la integración con `gspread` para que `comisiones_app.py`
no tenga que conocer detalles del API de Google. Funciones puras:
no importan streamlit, reciben los secrets como dict.

Estructura del Sheet:
  - Tab "historico": tabla con una fila por (vendedor, período).
  - Tab "pivot_vendedor": vista pivoteada vendedores × períodos
    con la comisión neta — se REGENERA en cada escritura.

Schema de "historico":
  vendedor | periodo | ventas | cobranzas | comision_neta | fecha_calculo

  - periodo: "AAAA-MM" (ordena bien lexicográficamente).
  - ventas / cobranzas: float, sin formato (se formatean al display).
  - comision_neta: int (ya redondeado al peso por commissions.py).
  - fecha_calculo: ISO "YYYY-MM-DD HH:MM".

Soporta dos formas de proveer credenciales del Service Account
(igual que el smoke_gsheets.py):
  A. Local: `service_account_json_path` apuntando a un .json en disco.
  B. Producción (Streamlit Cloud): `service_account` como dict embebido.

Si ambas están, gana B (la del dict).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import gspread
import pandas as pd


# =====================================================================
# Excepciones
# =====================================================================

class GsheetsError(Exception):
    """Error genérico al integrar con Google Sheets."""


class CredencialesError(GsheetsError):
    """Faltan credenciales o están mal configuradas."""


class PeriodoYaExisteError(GsheetsError):
    """El período ya tiene filas en el histórico — requiere flag de
    sobrescritura explícito para evitar duplicados accidentales."""


# =====================================================================
# Constantes
# =====================================================================

TAB_HISTORICO = "historico"
TAB_PIVOT = "pivot_vendedor"
TAB_COBRANZAS_PAGADAS = "cobranzas_pagadas"

HISTORICO_COLUMNS = [
    "vendedor",
    "periodo",
    "ventas",
    "cobranzas",
    "comision_neta",
    "fecha_calculo",
]

# Ledger de cobranzas individuales — se usa para detectar cobranzas
# tardías del mes anterior comparando contra la API. Una fila por
# (numero de cobranza, periodo). Cuando se sobreescribe un período,
# se borran sus filas y se reemplazan.
COBRANZAS_PAGADAS_COLUMNS = [
    "numero",
    "periodo_cobranza",
    "vendedor",
    "rut_cliente",
    "razon_social",
    "fecha_cobranza",
    "importe",
]


# =====================================================================
# Auth & cliente
# =====================================================================

def _resolver_credenciales(gsheets_section: dict, repo_root: Path | None = None) -> dict:
    """Resuelve el dict de credenciales del Service Account.

    Prioriza `service_account` embebido. Si no, lee del path
    `service_account_json_path` (relativo al repo si no es absoluto).
    """
    sa = gsheets_section.get("service_account")
    if sa:
        # Si vino como dict de Streamlit secrets, convertir a dict puro
        return dict(sa)

    path_str = gsheets_section.get("service_account_json_path")
    if not path_str:
        raise CredencialesError(
            "Faltan credenciales del Service Account. Configurá una de:\n"
            "  - gsheets.service_account_json_path = '.gsheets/sa.json'\n"
            "  - [gsheets.service_account] con el contenido del JSON."
        )

    p = Path(path_str)
    if not p.is_absolute():
        if repo_root is None:
            repo_root = Path(__file__).resolve().parent
        p = repo_root / p

    if not p.exists():
        raise CredencialesError(
            f"No existe el archivo de credenciales: {p}\n"
            f"Verificá la ruta en gsheets.service_account_json_path."
        )

    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise CredencialesError(
            f"El archivo {p} no es JSON válido: {e}"
        ) from e


def _get_client(gsheets_section: dict, repo_root: Path | None = None) -> gspread.Client:
    """Devuelve un cliente autenticado de gspread."""
    sa_dict = _resolver_credenciales(gsheets_section, repo_root=repo_root)
    return gspread.service_account_from_dict(sa_dict)


def _open_sheet(gsheets_section: dict, repo_root: Path | None = None):
    """Abre el spreadsheet configurado en `gsheets.spreadsheet_id`."""
    spreadsheet_id = gsheets_section.get("spreadsheet_id")
    if not spreadsheet_id:
        raise CredencialesError("Falta gsheets.spreadsheet_id en secrets.")
    client = _get_client(gsheets_section, repo_root=repo_root)
    try:
        return client.open_by_key(spreadsheet_id)
    except gspread.exceptions.SpreadsheetNotFound as e:
        raise GsheetsError(
            f"Sheet no encontrado (id={spreadsheet_id}). Verificá el ID."
        ) from e
    except PermissionError as e:
        raise GsheetsError(
            f"Sin permisos para abrir el Sheet. Compartilo con el "
            f"client_email del Service Account como Editor."
        ) from e


def _ensure_worksheet(sh, title: str, rows: int = 1000, cols: int = 10):
    """Devuelve la worksheet con `title`. Si no existe, la crea."""
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


# =====================================================================
# Lectura del histórico
# =====================================================================

def read_historico(gsheets_section: dict) -> pd.DataFrame:
    """Lee la tab `historico` y devuelve un DataFrame.

    Si la tab está vacía o solo tiene encabezados, devuelve DataFrame
    vacío con las columnas correctas. Tolerante a tabs sin headers
    (caso de Sheet recién creado): los crea silenciosamente.
    """
    sh = _open_sheet(gsheets_section)
    ws = _ensure_worksheet(sh, TAB_HISTORICO)
    rows = ws.get_all_values()

    if not rows:
        # Sheet vacío — escribir headers y devolver DF vacío.
        ws.update("A1", [HISTORICO_COLUMNS])
        return pd.DataFrame(columns=HISTORICO_COLUMNS)

    headers = rows[0]
    if headers != HISTORICO_COLUMNS:
        # Inicializar headers si no coinciden (primera corrida).
        if all(not c for c in headers):  # tab vacía
            ws.update("A1", [HISTORICO_COLUMNS])
            return pd.DataFrame(columns=HISTORICO_COLUMNS)
        raise GsheetsError(
            f"Encabezados inesperados en tab '{TAB_HISTORICO}'. "
            f"Esperaba {HISTORICO_COLUMNS}, encontró {headers}."
        )

    if len(rows) < 2:
        return pd.DataFrame(columns=HISTORICO_COLUMNS)

    df = pd.DataFrame(rows[1:], columns=HISTORICO_COLUMNS)
    # Tipado básico: ventas/cobranzas/comision_neta son numéricos.
    for c in ("ventas", "cobranzas"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df["comision_neta"] = pd.to_numeric(df["comision_neta"], errors="coerce").fillna(0).astype(int)
    return df


# =====================================================================
# Escritura: append idempotente del período
# =====================================================================

def write_historico_periodo(
    gsheets_section: dict,
    periodo: str,
    resumen: list[dict],
    *,
    sobreescribir: bool = False,
) -> dict:
    """Agrega las filas del período al histórico.

    Args:
        periodo: 'AAAA-MM'.
        resumen: lista de dicts como los devuelve
            `commissions.compute_commissions()`. Cada dict tiene al
            menos: vendedor, ventas_brutas, cobranzas, comision_neta.
        sobreescribir: si False (default) y el período ya está en el
            histórico, raise PeriodoYaExisteError (gate anti-duplicado).
            Si True, borra las filas viejas del período y reescribe.

    Returns:
        Dict con stats: {filas_agregadas, filas_eliminadas, periodos_total}.

    Después de actualizar 'historico', regenera la tab 'pivot_vendedor'.
    """
    if not periodo or len(periodo) != 7 or periodo[4] != "-":
        raise GsheetsError(
            f"`periodo` debe tener formato 'AAAA-MM'. Recibido: {periodo!r}"
        )

    sh = _open_sheet(gsheets_section)
    ws = _ensure_worksheet(sh, TAB_HISTORICO)

    rows_existentes = ws.get_all_values()
    headers_ok = bool(rows_existentes) and rows_existentes[0] == HISTORICO_COLUMNS
    data_rows = rows_existentes[1:] if headers_ok else []

    # Separar filas por período actual vs otros
    rows_otros = [r for r in data_rows if len(r) >= 2 and r[1] != periodo]
    rows_periodo_existente = [r for r in data_rows if len(r) >= 2 and r[1] == periodo]
    periodos_existentes = {r[1] for r in data_rows if len(r) >= 2}

    if rows_periodo_existente and not sobreescribir:
        raise PeriodoYaExisteError(
            f"El período {periodo} ya tiene {len(rows_periodo_existente)} "
            f"fila(s) en el histórico. Para reescribir, llamá esta "
            f"función con `sobreescribir=True`."
        )

    # Construir filas nuevas
    fecha_calculo = datetime.now().strftime("%Y-%m-%d %H:%M")
    nuevas_filas = []
    for r in resumen:
        nuevas_filas.append([
            r.get("vendedor", ""),
            periodo,
            float(r.get("ventas_brutas", 0.0)),
            float(r.get("cobranzas", 0.0)),
            int(r.get("comision_neta", 0)),
            fecha_calculo,
        ])

    # Reescribir tab entera de un solo golpe (1 clear + 1 update,
    # en lugar de N delete_rows + 1 append). Evita el rate-limit 429
    # de Google Sheets (60 escrituras/min).
    grid = [HISTORICO_COLUMNS] + rows_otros + nuevas_filas
    ws.clear()
    ws.update("A1", grid, value_input_option="USER_ENTERED")

    # Regenerar el pivot
    _rebuild_pivot(sh)

    periodos_finales = (periodos_existentes - {periodo}) | {periodo}

    return {
        "filas_agregadas": len(nuevas_filas),
        "filas_eliminadas": len(rows_periodo_existente),
        "periodos_total": len(periodos_finales),
    }


def _rebuild_pivot(sh) -> None:
    """Regenera la tab `pivot_vendedor` con vendedores × períodos
    (valor: comisión_neta).

    Lee la tab `historico` directamente desde el sheet abierto (ya
    autenticado) para no duplicar conexión.
    """
    ws_hist = _ensure_worksheet(sh, TAB_HISTORICO)
    rows = ws_hist.get_all_values()
    if len(rows) < 2:
        return

    headers = rows[0]
    if headers != HISTORICO_COLUMNS:
        return

    df = pd.DataFrame(rows[1:], columns=HISTORICO_COLUMNS)
    df["comision_neta"] = pd.to_numeric(df["comision_neta"], errors="coerce").fillna(0).astype(int)

    pivot = df.pivot_table(
        index="vendedor",
        columns="periodo",
        values="comision_neta",
        aggfunc="sum",
        fill_value=0,
    )
    # Ordenar columnas por período ascendente
    pivot = pivot[sorted(pivot.columns)]
    # Ordenar filas alfabéticamente
    pivot = pivot.sort_index()
    # Agregar fila de TOTAL al final
    pivot.loc["TOTAL"] = pivot.sum()

    ws_pivot = _ensure_worksheet(sh, TAB_PIVOT)
    ws_pivot.clear()

    # Construir el grid: encabezado + filas
    header_row = ["vendedor"] + list(pivot.columns)
    data_rows = []
    for vend, fila in pivot.iterrows():
        data_rows.append([vend] + [int(v) for v in fila.values])

    ws_pivot.update("A1", [header_row] + data_rows, value_input_option="USER_ENTERED")


# =====================================================================
# Cobranzas pagadas (ledger para detectar tardías del mes anterior)
# =====================================================================

def read_cobranzas_periodo(
    gsheets_section: dict, periodo: str
) -> pd.DataFrame:
    """Lee las cobranzas registradas en el Sheet para un período.

    Devuelve DataFrame con `COBRANZAS_PAGADAS_COLUMNS`. Si la tab no
    existe o el período no tiene filas, devuelve DataFrame vacío con
    las columnas correctas.
    """
    sh = _open_sheet(gsheets_section)
    ws = _ensure_worksheet(sh, TAB_COBRANZAS_PAGADAS)
    rows = ws.get_all_values()

    if not rows:
        ws.update("A1", [COBRANZAS_PAGADAS_COLUMNS])
        return pd.DataFrame(columns=COBRANZAS_PAGADAS_COLUMNS)

    headers = rows[0]
    if headers != COBRANZAS_PAGADAS_COLUMNS:
        if all(not c for c in headers):
            ws.update("A1", [COBRANZAS_PAGADAS_COLUMNS])
            return pd.DataFrame(columns=COBRANZAS_PAGADAS_COLUMNS)
        raise GsheetsError(
            f"Encabezados inesperados en tab '{TAB_COBRANZAS_PAGADAS}'. "
            f"Esperaba {COBRANZAS_PAGADAS_COLUMNS}, encontró {headers}."
        )

    if len(rows) < 2:
        return pd.DataFrame(columns=COBRANZAS_PAGADAS_COLUMNS)

    df = pd.DataFrame(rows[1:], columns=COBRANZAS_PAGADAS_COLUMNS)
    df["importe"] = pd.to_numeric(df["importe"], errors="coerce").fillna(0.0)
    return df[df["periodo_cobranza"] == periodo].reset_index(drop=True)


def write_cobranzas_periodo(
    gsheets_section: dict,
    periodo: str,
    cobranzas: list[dict],
) -> dict:
    """Escribe (con sobreescritura) las cobranzas de un período.

    Args:
        periodo: 'AAAA-MM'.
        cobranzas: lista de dicts con keys
            numero, vendedor, rut_cliente, razon_social,
            fecha_cobranza, importe.

    Si el período ya tiene filas en el Sheet, se reemplazan
    completamente. Esta es operación de "snapshot" del período —
    siempre escribe lo que viene como verdad.

    Returns: {filas_agregadas, filas_eliminadas, total_filas_periodo}.
    """
    sh = _open_sheet(gsheets_section)
    ws = _ensure_worksheet(sh, TAB_COBRANZAS_PAGADAS)

    rows_existentes = ws.get_all_values()
    headers_ok = (
        bool(rows_existentes) and rows_existentes[0] == COBRANZAS_PAGADAS_COLUMNS
    )
    data_rows = rows_existentes[1:] if headers_ok else []

    rows_otros = [r for r in data_rows if len(r) >= 2 and r[1] != periodo]
    rows_periodo_existente = [r for r in data_rows if len(r) >= 2 and r[1] == periodo]

    # Construir filas nuevas
    nuevas_filas = []
    for c in cobranzas:
        nuevas_filas.append([
            str(c.get("numero", "")),
            periodo,
            str(c.get("vendedor", "")),
            str(c.get("rut_cliente", "")),
            str(c.get("razon_social", "")),
            str(c.get("fecha_cobranza", "")),
            float(c.get("importe", 0.0)),
        ])

    # Reescribir tab entera de un solo golpe (evita rate-limit 429).
    grid = [COBRANZAS_PAGADAS_COLUMNS] + rows_otros + nuevas_filas
    ws.clear()
    ws.update("A1", grid, value_input_option="USER_ENTERED")

    return {
        "filas_agregadas": len(nuevas_filas),
        "filas_eliminadas": len(rows_periodo_existente),
        "total_filas_periodo": len(nuevas_filas),
    }


def periodo_existe_en_historico(
    gsheets_section: dict, periodo: str
) -> bool:
    """True si el período ya tiene filas en la tab `historico`. Útil
    para decidir si calcular ajuste retroactivo sobre M-1."""
    df = read_historico(gsheets_section)
    if df.empty:
        return False
    return periodo in set(df["periodo"].astype(str))
