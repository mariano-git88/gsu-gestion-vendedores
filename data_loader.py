"""
data_loader.py — Carga y validación de las 5 planillas de Contabilium.

Este módulo es la frontera entre los archivos .xlsx que sube el usuario y
los DataFrames que consume el resto del app. Funciones puras: reciben un
file path o un file-like object (UploadedFile de Streamlit) y devuelven
un DataFrame ya validado y normalizado a nombres internos snake_case.

NO importa streamlit. El cache de UI vive en app.py envolviendo estas
funciones con @st.cache_data.

Reglas implementadas (ver claude.md.txt, sección "Reglas de carga y tipos
de dato"):

  - `Documento` siempre como string (dtype={'Documento': str}).
  - Lectura con hoja explícita por planilla — no depender del orden.
  - Validación estricta de schemas: si falta alguna columna requerida,
    levanta MissingColumnsError con detalle de qué falta.
  - Si la hoja específica no existe en el archivo, SheetNotFoundError.
  - Después de validar, rename a nombres internos snake_case.
  - El loader de combos deduplica por SKU Combo (la planilla original es
    una lista de materiales con varias filas por combo).
"""

import pandas as pd

# =====================================================================
# Constantes — nombres de hojas, columnas requeridas, mapping de rename
# =====================================================================

SHEET_FC = "Comprobantes"
SHEET_CLIENTES = "Clientes"
SHEET_PRODUCTOS = "Productos"
SHEET_COMBOS = "Combos"

# Columnas que TIENEN que existir en la planilla original (nombres del ERP).
# Si falta alguna, el loader levanta error y aborta.
REQUIRED_FC = [
    "Documento",
    "Razon Social",
    "Vendedor",
    "Fecha",
    "Tipo",
    "Moneda",
    "Codigo",
    "Producto",
    "Cantidad",
    "Subo Total Bonif",
]
REQUIRED_CLIENTES = [
    "Documento",
    "Razon Social",
    "Vendedor Asignado",
]
REQUIRED_PRODUCTOS = [
    "SKU",
    "Nombre",
    "Sub Rubro",
    "Rubro",
]
REQUIRED_COMBOS = [
    "SKU Combo",
    "Nombre",
]

# Mapping ERP → nombre interno snake_case. Después del rename, todo el
# resto del código (transforms, metrics, views) trabaja con estos nombres.
RENAME_FC = {
    "Documento": "documento",
    "Razon Social": "razon_social",
    "Vendedor": "vendedor",
    "Fecha": "fecha",
    "Tipo": "tipo",
    "Moneda": "moneda",
    "Codigo": "sku",
    "Producto": "producto",
    "Cantidad": "unidades",
    "Subo Total Bonif": "monto",
}
RENAME_CLIENTES = {
    "Documento": "documento",
    "Razon Social": "razon_social",
    "Vendedor Asignado": "vendedor",
}
RENAME_PRODUCTOS = {
    "SKU": "sku",
    "Nombre": "nombre",
    "Sub Rubro": "sub_rubro",
    "Rubro": "rubro",
}
RENAME_COMBOS = {
    "SKU Combo": "sku",
    "Nombre": "nombre",
}


# =====================================================================
# Excepciones específicas
# =====================================================================

class SheetNotFoundError(ValueError):
    """Se levanta cuando el archivo .xlsx no contiene la hoja esperada."""

    def __init__(self, sheet_name: str):
        self.sheet_name = sheet_name
        super().__init__(
            f"No se encontró la hoja '{sheet_name}' en el archivo. "
            f"Verificá que la planilla tenga una hoja con ese nombre exacto."
        )


class MissingColumnsError(ValueError):
    """Se levanta cuando una hoja no tiene todas las columnas requeridas."""

    def __init__(self, sheet_name: str, missing: set, found: set):
        self.sheet_name = sheet_name
        self.missing = sorted(missing)
        self.found = sorted(found)
        super().__init__(
            f"En la hoja '{sheet_name}' faltan columnas requeridas: "
            f"{self.missing}. Columnas encontradas en la planilla: {self.found}."
        )


# =====================================================================
# Helpers internos
# =====================================================================

def _validate_columns(df: pd.DataFrame, required: list, sheet_name: str) -> None:
    """Verifica que df contenga TODAS las columnas requeridas."""
    found = set(df.columns)
    missing = set(required) - found
    if missing:
        raise MissingColumnsError(sheet_name, missing, found)


def _read_and_validate(
    file_or_path,
    sheet_name: str,
    required: list,
    rename: dict,
    *,
    dtype: dict | None = None,
) -> pd.DataFrame:
    """
    Pipeline de lectura común:
      1. Leer la hoja específica del .xlsx (engine openpyxl, dtype opcional).
      2. Validar que existan todas las columnas requeridas.
      3. Quedarse SOLO con las columnas requeridas (descarta el resto).
      4. Renombrar al esquema interno snake_case.

    Devuelve un DataFrame limpio listo para transforms.
    """
    try:
        df = pd.read_excel(
            file_or_path,
            sheet_name=sheet_name,
            dtype=dtype or {},
            engine="openpyxl",
        )
    except ValueError as e:
        # pandas/openpyxl tira ValueError "Worksheet named 'X' not found" cuando
        # la hoja no existe. Lo traducimos a un error más amigable.
        if "not found" in str(e).lower() or "no sheet" in str(e).lower():
            raise SheetNotFoundError(sheet_name) from e
        raise

    _validate_columns(df, required, sheet_name)
    df = df[required].rename(columns=rename)
    return df


# =====================================================================
# Loaders públicos — uno por planilla
# =====================================================================

def load_fc(file_or_path) -> pd.DataFrame:
    """
    Carga una planilla de facturación (semanal o mensual).

    Misma estructura para `fc_semanal.xlsx` y `fc_mensual.xlsx`: hoja
    `Comprobantes`, mismas columnas. Documento se lee como string para
    preservar formato (no convertir a int, no quitar ceros).

    Devuelve DataFrame con columnas:
      documento, razon_social, vendedor, fecha, tipo, moneda,
      sku, producto, unidades, monto
    """
    return _read_and_validate(
        file_or_path,
        sheet_name=SHEET_FC,
        required=REQUIRED_FC,
        rename=RENAME_FC,
        dtype={"Documento": str},
    )


def load_clientes(file_or_path) -> pd.DataFrame:
    """
    Carga el maestro de clientes (`clientes.xlsx`, hoja `Clientes`).

    Es el universo canónico: la `razon_social` que se muestra en la UI sale
    de acá (no de facturación), y la asignación cliente→vendedor también.

    Devuelve DataFrame con columnas:
      documento, razon_social, vendedor
    """
    return _read_and_validate(
        file_or_path,
        sheet_name=SHEET_CLIENTES,
        required=REQUIRED_CLIENTES,
        rename=RENAME_CLIENTES,
        dtype={"Documento": str},
    )


def load_productos(file_or_path) -> pd.DataFrame:
    """
    Carga el maestro de productos (`productos.xlsx`, hoja `Productos`).

    Devuelve DataFrame con columnas:
      sku, nombre, sub_rubro, rubro
    """
    return _read_and_validate(
        file_or_path,
        sheet_name=SHEET_PRODUCTOS,
        required=REQUIRED_PRODUCTOS,
        rename=RENAME_PRODUCTOS,
    )


def load_combos(file_or_path) -> pd.DataFrame:
    """
    Carga el maestro de combos (`combos.xlsx`, hoja `Combos`).

    OJO: la planilla original NO es una tabla `sku → nombre`. Es una lista
    de materiales: cada fila es un item que compone un combo, y un mismo
    combo aparece en múltiples filas (una por cada item). Para usarla como
    tabla de lookup en transforms.py, deduplicamos por `sku` (el SKU del
    combo, no el del item) y nos quedamos con la primera ocurrencia de
    cada combo.

    Devuelve DataFrame con columnas:
      sku, nombre

    El número de filas final = cantidad de combos distintos en la planilla.
    """
    df = _read_and_validate(
        file_or_path,
        sheet_name=SHEET_COMBOS,
        required=REQUIRED_COMBOS,
        rename=RENAME_COMBOS,
    )
    df = df.drop_duplicates(subset="sku", keep="first").reset_index(drop=True)
    return df
