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
TAB_LOG_FACTURACION = "log_facturacion"
TAB_LOG_CARGA_PEDIDOS = "log_carga_pedidos"
TAB_EQUIVALENCIAS_LISTAS = "equivalencias_uy_ar"

# Tabla de equivalencias entre SKUs UY (Contabilium) y SKUs AR
# (export Lista_Marketing). Una fila por equivalencia confirmada
# manualmente desde listas_app.py.
EQUIVALENCIAS_LISTAS_COLUMNS = [
    "sku_uy",         # str — SKU canónico (Contabilium UY)
    "sku_ar",         # str — SKU del export AR que mapea al UY
    "fecha",          # ISO YYYY-MM-DD HH:MM cuando se creó la equivalencia
    "nota",           # str opcional, contexto sobre por qué son equivalentes
]

# Schema del log de facturación masiva. Una fila por orden procesada
# (sea exitosa o fallida). Append-only: nunca se borran ni reescriben filas.
LOG_FACTURACION_COLUMNS = [
    "timestamp",          # ISO YYYY-MM-DD HH:MM:SS
    "id_orden",           # int, ID interno de Contabilium
    "numero_orden",       # str, ej "00010445"
    "comprador",          # str, razón social del cliente
    "total_uyu",          # float
    "status",             # "OK" | "ERROR"
    "id_comprobante",     # int (0 si falló antes de crear borrador)
    "numero_factura",     # str, ej "FAC A-00033662" (vacío si falló)
    "cae",                # str (vacío si falló)
    "fiscal_url",         # str URL del QR DGI (vacío si falló)
    "orden_cancelada",    # bool: si la orden de venta se canceló post-emisión (libera reserva)
    "orden_cancel_error", # str: mensaje de error si falló la cancelación (factura sigue válida)
    "error",              # str con mensaje de error (vacío si OK)
]

# Schema del audit log de Carga de Pedidos (Fase 2). Una fila por
# pedido procesado. Append-only. Registra quién aprobó qué.
LOG_CARGA_PEDIDOS_COLUMNS = [
    "timestamp",          # ISO YYYY-MM-DD HH:MM:SS
    "usuario",            # str — quien operó (libre, lo setea la app)
    "pedido",             # str, hoja ej "Pedido 4"
    "nro_cliente",        # str, el del Excel ej "4060"
    "cliente",            # str, razón social Contabilium
    "rut",                # str
    "id_cliente",         # int Contabilium
    "id_vendedor",        # int (vendedor del cliente)
    "n_items",            # int
    "total_sin_iva",      # float
    "aprobado_deuda",     # "SI" | "NO" | "N/A"
    "aprobado_precio",    # "SI" | "NO" | "N/A"
    "descuentos",         # str, ej "fila10:32%; fila57:10%" | "—"
    "status",             # "OK" | "ERROR"
    "numero_orden",       # str (vacío si falló)
    "id_orden",           # str/int (vacío si falló)
    "error",              # str (vacío si OK)
]

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


# =====================================================================
# Log de facturación masiva
# =====================================================================

def append_log_facturacion(
    gsheets_section: dict,
    filas: list[dict],
) -> int:
    """Apenda filas al log de facturación masiva. Append-only: nunca
    borra ni reescribe filas existentes.

    Schema esperado en cada fila: ver `LOG_FACTURACION_COLUMNS`.
    Las claves no presentes se llenan con string vacío.

    Devuelve la cantidad de filas escritas. Si `filas` es vacío,
    no toca el Sheet y devuelve 0.

    Diseño:
      - El caller (facturador_app.py) decide si tratar la excepción de
        este método como warning (no detener el flujo) o como error.
        El módulo de facturación NO debe romperse por un fallo del log
        — la verdad fiscal vive en el comprobante emitido, no acá.
      - Tab dedicada `log_facturacion`. Si no existe, se crea con header.
    """
    if not filas:
        return 0

    sh = _open_sheet(gsheets_section)
    ws = _ensure_worksheet(
        sh, TAB_LOG_FACTURACION,
        rows=10000, cols=len(LOG_FACTURACION_COLUMNS),
    )

    # Asegurar header en row 1. Si el header existe pero tiene menos
    # columnas que LOG_FACTURACION_COLUMNS (Sheet creado con schema
    # viejo), lo reemplazamos in-place — append_rows no se rompe porque
    # las filas viejas mantienen sus valores en las primeras N columnas
    # y las nuevas quedan vacías.
    existing_header = ws.row_values(1)
    if not existing_header or len(existing_header) < len(LOG_FACTURACION_COLUMNS):
        ws.update("A1", [LOG_FACTURACION_COLUMNS], value_input_option="RAW")

    # Construir filas en el orden de LOG_FACTURACION_COLUMNS, todo string.
    rows_data = []
    for f in filas:
        rows_data.append([str(f.get(c, "") or "") for c in LOG_FACTURACION_COLUMNS])

    ws.append_rows(rows_data, value_input_option="RAW")
    return len(rows_data)


def append_log_carga_pedidos(
    gsheets_section: dict,
    filas: list[dict],
) -> int:
    """Apenda filas al audit log de Carga de Pedidos (Fase 2).

    Append-only, mismo diseño que `append_log_facturacion`: tab dedicada
    `log_carga_pedidos` (se crea con header si no existe), best-effort
    desde el caller (un fallo del log no debe romper la carga; la verdad
    vive en la orden creada en Contabilium). Reusa el mismo Sheet/Service
    Account del facturador (`[gsheets_facturacion]`).
    """
    if not filas:
        return 0

    sh = _open_sheet(gsheets_section)
    ws = _ensure_worksheet(
        sh, TAB_LOG_CARGA_PEDIDOS,
        rows=10000, cols=len(LOG_CARGA_PEDIDOS_COLUMNS),
    )

    existing_header = ws.row_values(1)
    if not existing_header or len(existing_header) < len(LOG_CARGA_PEDIDOS_COLUMNS):
        ws.update("A1", [LOG_CARGA_PEDIDOS_COLUMNS], value_input_option="RAW")

    rows_data = [
        [str(f.get(c, "") or "") for c in LOG_CARGA_PEDIDOS_COLUMNS]
        for f in filas
    ]
    ws.append_rows(rows_data, value_input_option="RAW")
    return len(rows_data)


# =====================================================================
# Equivalencias UY ↔ AR (listas de precios)
# =====================================================================

def read_equivalencias_listas(gsheets_section: dict) -> pd.DataFrame:
    """Lee la tab de equivalencias SKU UY ↔ SKU AR.

    Si la tab no existe, la crea con headers y devuelve DataFrame vacío.
    """
    sh = _open_sheet(gsheets_section)
    ws = _ensure_worksheet(
        sh, TAB_EQUIVALENCIAS_LISTAS, cols=len(EQUIVALENCIAS_LISTAS_COLUMNS)
    )
    rows = ws.get_all_values()

    if not rows:
        ws.update("A1", [EQUIVALENCIAS_LISTAS_COLUMNS])
        return pd.DataFrame(columns=EQUIVALENCIAS_LISTAS_COLUMNS)

    headers = rows[0]
    if headers != EQUIVALENCIAS_LISTAS_COLUMNS:
        if all(not c for c in headers):
            ws.update("A1", [EQUIVALENCIAS_LISTAS_COLUMNS])
            return pd.DataFrame(columns=EQUIVALENCIAS_LISTAS_COLUMNS)
        raise GsheetsError(
            f"Encabezados inesperados en tab '{TAB_EQUIVALENCIAS_LISTAS}'. "
            f"Esperaba {EQUIVALENCIAS_LISTAS_COLUMNS}, encontró {headers}."
        )

    if len(rows) < 2:
        return pd.DataFrame(columns=EQUIVALENCIAS_LISTAS_COLUMNS)

    return pd.DataFrame(rows[1:], columns=EQUIVALENCIAS_LISTAS_COLUMNS)


def add_equivalencia_lista(
    gsheets_section: dict,
    sku_uy: str,
    sku_ar: str,
    nota: str = "",
) -> dict:
    """Agrega una equivalencia SKU UY ↔ SKU AR. Idempotente: si la
    misma combinación ya existe, no se duplica.

    Valida que ni `sku_uy` ni `sku_ar` aparezcan ya en otras
    equivalencias (caso ambiguo): si alguno está, raise GsheetsError
    con el conflicto explícito.
    """
    sku_uy = (sku_uy or "").strip().upper()
    sku_ar = (sku_ar or "").strip().upper()
    if not sku_uy or not sku_ar:
        raise GsheetsError("sku_uy y sku_ar no pueden estar vacíos.")

    df = read_equivalencias_listas(gsheets_section)

    if not df.empty:
        df["sku_uy"] = df["sku_uy"].astype(str).str.strip().str.upper()
        df["sku_ar"] = df["sku_ar"].astype(str).str.strip().str.upper()

        # Idempotencia: ya existe exactamente esta combinación
        ya_esta = ((df["sku_uy"] == sku_uy) & (df["sku_ar"] == sku_ar)).any()
        if ya_esta:
            return {"agregada": False, "motivo": "ya_existia"}

        # Conflictos
        conflicto_uy = df[df["sku_uy"] == sku_uy]
        if not conflicto_uy.empty:
            otro_ar = conflicto_uy.iloc[0]["sku_ar"]
            raise GsheetsError(
                f"El SKU UY {sku_uy!r} ya está vinculado al SKU AR "
                f"{otro_ar!r}. Borrá esa equivalencia antes de crear una nueva."
            )
        conflicto_ar = df[df["sku_ar"] == sku_ar]
        if not conflicto_ar.empty:
            otro_uy = conflicto_ar.iloc[0]["sku_uy"]
            raise GsheetsError(
                f"El SKU AR {sku_ar!r} ya está vinculado al SKU UY "
                f"{otro_uy!r}. Borrá esa equivalencia antes de crear una nueva."
            )

    sh = _open_sheet(gsheets_section)
    ws = _ensure_worksheet(
        sh, TAB_EQUIVALENCIAS_LISTAS, cols=len(EQUIVALENCIAS_LISTAS_COLUMNS)
    )
    # Asegurar header
    existing_header = ws.row_values(1)
    if not existing_header or existing_header[: len(EQUIVALENCIAS_LISTAS_COLUMNS)] != EQUIVALENCIAS_LISTAS_COLUMNS:
        ws.update("A1", [EQUIVALENCIAS_LISTAS_COLUMNS], value_input_option="RAW")

    fila = [
        sku_uy,
        sku_ar,
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        str(nota or ""),
    ]
    ws.append_row(fila, value_input_option="RAW")
    return {"agregada": True}


def bulk_add_equivalencias_listas(
    gsheets_section: dict,
    filas: list[dict],
) -> dict:
    """Inserción masiva de equivalencias. Append-only.

    Cada elemento de `filas` debe tener keys: sku_uy, sku_ar, y
    opcionalmente nota.

    Comportamiento por fila:
      - Pares (sku_uy, sku_ar) ya existentes exactos → conteo en
        `duplicadas`, no se reescriben.
      - sku_uy o sku_ar ya vinculados a OTRA equivalencia (en el Sheet
        o en el mismo batch) → registrado en `conflictos` con motivo.
      - Resto → agregadas en un solo `append_rows` (1 escritura, evita
        rate-limit 429 de Google Sheets).

    Devuelve dict:
        {agregadas: int, duplicadas: int, conflictos: list[dict]}
    Cada conflicto es {sku_uy, sku_ar, motivo}.
    """
    if not filas:
        return {"agregadas": 0, "duplicadas": 0, "conflictos": []}

    df_existente = read_equivalencias_listas(gsheets_section)

    pares_existentes: set[tuple[str, str]] = set()
    sku_uy_tomados: dict[str, str] = {}
    sku_ar_tomados: dict[str, str] = {}
    if not df_existente.empty:
        for _, r in df_existente.iterrows():
            uy = str(r["sku_uy"]).strip().upper()
            ar = str(r["sku_ar"]).strip().upper()
            if uy and ar:
                pares_existentes.add((uy, ar))
                sku_uy_tomados[uy] = ar
                sku_ar_tomados[ar] = uy

    agregadas_rows: list[list[str]] = []
    duplicadas = 0
    conflictos: list[dict] = []
    fecha_now = datetime.now().strftime("%Y-%m-%d %H:%M")

    for f in filas:
        uy = (f.get("sku_uy") or "").strip().upper()
        ar = (f.get("sku_ar") or "").strip().upper()
        nota = str(f.get("nota") or "")
        if not uy or not ar:
            conflictos.append({
                "sku_uy": uy, "sku_ar": ar, "motivo": "sku vacío"
            })
            continue
        if (uy, ar) in pares_existentes:
            duplicadas += 1
            continue
        if uy in sku_uy_tomados:
            conflictos.append({
                "sku_uy": uy, "sku_ar": ar,
                "motivo": f"SKU UY ya vinculado a {sku_uy_tomados[uy]}",
            })
            continue
        if ar in sku_ar_tomados:
            conflictos.append({
                "sku_uy": uy, "sku_ar": ar,
                "motivo": f"SKU AR ya vinculado a {sku_ar_tomados[ar]}",
            })
            continue
        # Reservar para detectar conflictos internos del mismo batch
        pares_existentes.add((uy, ar))
        sku_uy_tomados[uy] = ar
        sku_ar_tomados[ar] = uy
        agregadas_rows.append([uy, ar, fecha_now, nota])

    if agregadas_rows:
        sh = _open_sheet(gsheets_section)
        ws = _ensure_worksheet(
            sh, TAB_EQUIVALENCIAS_LISTAS, cols=len(EQUIVALENCIAS_LISTAS_COLUMNS)
        )
        existing_header = ws.row_values(1)
        if not existing_header or existing_header[: len(EQUIVALENCIAS_LISTAS_COLUMNS)] != EQUIVALENCIAS_LISTAS_COLUMNS:
            ws.update("A1", [EQUIVALENCIAS_LISTAS_COLUMNS], value_input_option="RAW")
        ws.append_rows(agregadas_rows, value_input_option="RAW")

    return {
        "agregadas": len(agregadas_rows),
        "duplicadas": duplicadas,
        "conflictos": conflictos,
    }


def delete_equivalencia_lista(
    gsheets_section: dict,
    sku_uy: str,
    sku_ar: str,
) -> dict:
    """Elimina la equivalencia (sku_uy, sku_ar) del Sheet. Si no existe,
    devuelve {"eliminada": False, "motivo": "no_existia"} sin error.
    """
    sku_uy = (sku_uy or "").strip().upper()
    sku_ar = (sku_ar or "").strip().upper()

    sh = _open_sheet(gsheets_section)
    ws = _ensure_worksheet(
        sh, TAB_EQUIVALENCIAS_LISTAS, cols=len(EQUIVALENCIAS_LISTAS_COLUMNS)
    )
    rows = ws.get_all_values()
    if len(rows) < 2:
        return {"eliminada": False, "motivo": "no_existia"}

    headers = rows[0]
    if headers != EQUIVALENCIAS_LISTAS_COLUMNS:
        raise GsheetsError(
            f"Encabezados inesperados en tab '{TAB_EQUIVALENCIAS_LISTAS}'."
        )

    data = rows[1:]
    nuevas = [
        r for r in data
        if not (
            len(r) >= 2
            and r[0].strip().upper() == sku_uy
            and r[1].strip().upper() == sku_ar
        )
    ]
    if len(nuevas) == len(data):
        return {"eliminada": False, "motivo": "no_existia"}

    grid = [EQUIVALENCIAS_LISTAS_COLUMNS] + nuevas
    ws.clear()
    ws.update("A1", grid, value_input_option="RAW")
    return {"eliminada": True, "filas_restantes": len(nuevas)}
