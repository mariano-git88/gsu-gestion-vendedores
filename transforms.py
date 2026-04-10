"""
transforms.py — Lógica de negocio del pipeline de facturación.

Este módulo recibe los DataFrames "crudos" que produce data_loader.py
(ya validados estructuralmente y con columnas en snake_case) y aplica
las reglas críticas del proyecto:

  1. Filtrado de notas de crédito (descartar descuentos comerciales,
     conservar devoluciones reales).
  2. Validación de moneda (solo UYU, sin conversión automática).
  3. Join de facturación contra clientes (por `documento`, llave canónica).
  4. Reemplazo de razón social por la canónica de clientes.
  5. Clasificación de SKU: productos → combos → SIN ASIGNAR.
  6. Validación cross-tabla: vendedores con ventas pero sin cartera.

Funciones puras: reciben DataFrames, devuelven DataFrames + info para
el panel de salud. NO importa streamlit. NO escribe nada a disco.

Todas las reglas implementadas acá vienen del manual operativo
(claude.md.txt, secciones "Reglas de filtrado y transformación" y
"Panel de salud de datos"). Si una regla cambia, hay que actualizar el
manual Y este módulo en el mismo commit.
"""

from typing import Tuple

import pandas as pd

# Status posibles de una fila de facturación tras el join contra clientes
STATUS_OK = "OK"
STATUS_DOC_FALTANTE = "DOCUMENTO FALTANTE"
STATUS_CLIENTE_NO_ENCONTRADO = "CLIENTE NO ENCONTRADO"

# Categoría de sub_rubro asignada cuando un SKU matchea como combo
SUB_RUBRO_COMBO = "COMBO"
SUB_RUBRO_SIN_ASIGNAR = "SIN ASIGNAR"

# Origen de la clasificación (para auditoría y panel de salud)
ORIGEN_PRODUCTO = "producto"
ORIGEN_COMBO = "combo"
ORIGEN_SIN_ASIGNAR = "sin_asignar"

# Vendedores operativos / administrativos que NO cuentan para ninguna
# métrica del dashboard (decisión 2026-04-10, ver _learning/decisions.md).
# Match exacto por string. Si el ERP cambia la capitalización o el dominio,
# actualizar esta lista.
VENDEDORES_OP_EXCLUIDOS = frozenset({
    "OPJESICA@SUPRABOND.COM.UY",
    "OPVALERIA@SUPRABOND.COM.UY",
})


# =====================================================================
# Helpers
# =====================================================================

def _is_blank(val) -> bool:
    """
    Devuelve True si `val` es 'vacío' en sentido amplio:
      - NaN / None / NaT
      - String vacío o solo whitespace
    """
    if pd.isna(val):
        return True
    if isinstance(val, str) and val.strip() == "":
        return True
    return False


# =====================================================================
# 0. Exclusión de vendedores operativos (OPJESICA, OPVALERIA)
# =====================================================================

def exclude_op_vendedores(df_fc: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Excluye filas cuyo `vendedor` está en la lista de vendedores operativos
    que no se contabilizan en el dashboard.

    OPJESICA y OPVALERIA son cuentas operativas/administrativas, no
    representantes comerciales. Sus operaciones aparecen en facturación
    pero no deben contar para venta, cobertura ni ninguna métrica de
    performance del equipo comercial.

    Es el primer paso del pipeline (`prepare_facturacion`), antes incluso
    del filtrado de NCF, así el resto del pipeline no procesa filas que
    igual van a descartarse.

    Match exacto por string (case-sensitive). Ver `VENDEDORES_OP_EXCLUIDOS`
    arriba en este módulo.

    Devuelve `(df_conservadas, df_excluidas)`. La segunda es para reportar
    en el panel de salud cuántas líneas se excluyeron y de qué cuentas.
    """
    mask = df_fc["vendedor"].isin(VENDEDORES_OP_EXCLUIDOS)
    df_excl = df_fc[mask].copy().reset_index(drop=True)
    df_keep = df_fc[~mask].copy().reset_index(drop=True)
    return df_keep, df_excl


# =====================================================================
# 1. Filtrado de notas de crédito
# =====================================================================

def filter_notas_credito(df_fc: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Aplica la regla del manual sobre NCF:

      - Conservar TODAS las filas con `tipo == 'FAC'`.
      - Conservar las filas con `tipo == 'NCF'` SOLO si tienen `sku` no
        vacío (devoluciones reales — tienen producto, cantidad negativa,
        monto negativo). Estas netean correctamente con las FAC.
      - DESCARTAR las filas con `tipo == 'NCF'` y `sku` vacío. Estas son
        descuentos comerciales (ej: "10% DTO COMERCIAL") sin producto
        asociado, y no deben afectar las métricas por SKU/sub_rubro.

    Devuelve una tupla `(df_conservadas, df_descartadas)`. La segunda es
    útil para reportar en el panel de salud cuántas líneas se filtraron
    y por qué.
    """
    es_fac = df_fc["tipo"] == "FAC"
    es_ncf = df_fc["tipo"] == "NCF"
    sku_vacio = df_fc["sku"].apply(_is_blank)

    # Conservar: todas las FAC + NCF con sku
    mask_conservar = es_fac | (es_ncf & ~sku_vacio)
    # Descartar: NCF sin sku
    mask_descartar = es_ncf & sku_vacio

    df_conservadas = df_fc[mask_conservar].copy().reset_index(drop=True)
    df_descartadas = df_fc[mask_descartar].copy().reset_index(drop=True)
    return df_conservadas, df_descartadas


# =====================================================================
# 2. Validación de moneda
# =====================================================================

def validate_moneda(df_fc: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Separa el DataFrame en filas con `moneda == 'UYU'` y filas con
    cualquier otra cosa. Las filas no-UYU NO se intentan convertir; se
    excluyen del cálculo y se reportan al usuario.

    Devuelve `(df_uyu, df_no_uyu)`. El llamador suele descartar la
    segunda y reportar su largo en el panel de salud.
    """
    mask_uyu = df_fc["moneda"] == "UYU"
    df_uyu = df_fc[mask_uyu].copy().reset_index(drop=True)
    df_no_uyu = df_fc[~mask_uyu].copy().reset_index(drop=True)
    return df_uyu, df_no_uyu


# =====================================================================
# 3. Join facturación ↔ clientes
# =====================================================================

def join_facturacion_clientes(
    df_fc: pd.DataFrame,
    df_clientes: pd.DataFrame,
) -> pd.DataFrame:
    """
    Left join por `documento` entre facturación y clientes.

    Reglas del manual implementadas:
      - El join SIEMPRE es por `documento`, nunca por razón social.
      - La `razon_social` que se muestra en la UI viene de clientes
        (canónica). Se descarta la que viene en facturación.
      - Si una fila de fc tiene `documento` vacío → status_cliente
        = 'DOCUMENTO FALTANTE'.
      - Si una fila tiene `documento` que no matchea en clientes →
        status_cliente = 'CLIENTE NO ENCONTRADO'.
      - Las filas no se descartan en ningún caso, solo se marcan.

    IMPORTANTE: del DataFrame de clientes solo se trae `razon_social`,
    NO el `vendedor` asignado al cliente. El `vendedor` de cada fila
    sigue siendo el que hizo la venta (vendedor de la operación), no
    el que tiene asignado el cliente en cartera. La asignación
    cliente→vendedor se usa después en metrics.py para calcular
    cobertura, pero NO contamina cada fila de facturación.

    Asume que `df_clientes` está deduplicado por `documento`. Si hay
    duplicados, el merge va a duplicar filas de fc — usar
    `check_clientes_duplicados()` antes para detectarlos.
    """
    # Borrar la razón social que viene de fc (vamos a usar la canónica)
    df = df_fc.drop(columns=["razon_social"]).copy()

    # Subset de clientes solo con la info que vamos a traer
    df_clientes_subset = df_clientes[["documento", "razon_social"]]

    df_joined = df.merge(
        df_clientes_subset,
        on="documento",
        how="left",
        indicator=True,
    )

    # Asignar status_cliente fila por fila
    doc_blank = df_joined["documento"].apply(_is_blank)
    no_match = df_joined["_merge"] == "left_only"

    status = pd.Series(STATUS_OK, index=df_joined.index)
    status[no_match] = STATUS_CLIENTE_NO_ENCONTRADO
    # DOCUMENTO FALTANTE pisa CLIENTE NO ENCONTRADO porque es más específico
    status[doc_blank] = STATUS_DOC_FALTANTE

    df_joined["status_cliente"] = status
    df_joined = df_joined.drop(columns=["_merge"]).reset_index(drop=True)
    return df_joined


def check_clientes_duplicados(df_clientes: pd.DataFrame) -> list:
    """
    Devuelve la lista de `documento`s que aparecen más de una vez en el
    maestro de clientes. Si la lista no está vacía, hay un problema de
    integridad en el maestro que hay que reportar al usuario antes de
    correr el join (sino el merge va a duplicar filas de fc).
    """
    counts = df_clientes["documento"].value_counts()
    return counts[counts > 1].index.tolist()


# =====================================================================
# 4. Clasificación de SKU
# =====================================================================

def classify_skus(
    df_fc: pd.DataFrame,
    df_productos: pd.DataFrame,
    df_combos: pd.DataFrame,
) -> pd.DataFrame:
    """
    Clasifica cada fila de facturación según su `sku`, en este orden
    estricto (el orden importa):

      1. ¿El sku está en productos? → sub_rubro y nombre del producto.
         origen = 'producto'.
      2. Si no, ¿está en combos (deduplicados)? → sub_rubro = 'COMBO',
         nombre del combo.
         origen = 'combo'.
      3. Si no está en ninguno → sub_rubro = 'SIN ASIGNAR', nombre =
         el `producto` original de fc (fallback de display).
         origen = 'sin_asignar'.

    Agrega tres columnas nuevas al DataFrame:
      - `sub_rubro`: del producto, o 'COMBO', o 'SIN ASIGNAR'
      - `nombre`: nombre canónico para mostrar en la UI
      - `origen_clasificacion`: 'producto' / 'combo' / 'sin_asignar'

    La columna `producto` original de fc se conserva (auditoría),
    pero las vistas usan `nombre`.

    Asume que `df_combos` ya viene deduplicado por sku (eso lo hace
    `data_loader.load_combos()`).
    """
    df = df_fc.copy()

    productos_lookup = df_productos.set_index("sku")[["sub_rubro", "nombre"]]
    combos_lookup = df_combos.set_index("sku")["nombre"]

    # Inicializar las tres columnas nuevas con NA
    df["sub_rubro"] = pd.NA
    df["nombre"] = pd.NA
    df["origen_clasificacion"] = pd.NA

    # Step 1: matchear contra productos
    in_productos = df["sku"].isin(productos_lookup.index)
    if in_productos.any():
        skus_match = df.loc[in_productos, "sku"]
        df.loc[in_productos, "sub_rubro"] = skus_match.map(productos_lookup["sub_rubro"]).values
        df.loc[in_productos, "nombre"] = skus_match.map(productos_lookup["nombre"]).values
        df.loc[in_productos, "origen_clasificacion"] = ORIGEN_PRODUCTO

    # Step 2: de los que no matchearon en productos, matchear contra combos
    pendientes = ~in_productos
    in_combos = pendientes & df["sku"].isin(combos_lookup.index)
    if in_combos.any():
        skus_match = df.loc[in_combos, "sku"]
        df.loc[in_combos, "sub_rubro"] = SUB_RUBRO_COMBO
        df.loc[in_combos, "nombre"] = skus_match.map(combos_lookup).values
        df.loc[in_combos, "origen_clasificacion"] = ORIGEN_COMBO

    # Step 3: el resto cae en SIN ASIGNAR
    sin_asignar = pendientes & ~in_combos
    if sin_asignar.any():
        df.loc[sin_asignar, "sub_rubro"] = SUB_RUBRO_SIN_ASIGNAR
        df.loc[sin_asignar, "nombre"] = df.loc[sin_asignar, "producto"]
        df.loc[sin_asignar, "origen_clasificacion"] = ORIGEN_SIN_ASIGNAR

    return df


# =====================================================================
# 5. Validación cross-tabla: vendedores sin cartera
# =====================================================================

def check_vendedores_sin_cartera(
    df_fc: pd.DataFrame,
    df_clientes: pd.DataFrame,
) -> list:
    """
    Devuelve la lista ordenada de vendedores que aparecen haciendo ventas
    en la planilla de facturación pero NO tienen ningún cliente asignado
    en clientes.xlsx.

    Esto es un error estructural: un vendedor sin cartera no debería
    poder facturar contra clientes. Si aparece, suele indicar que el
    maestro de clientes está desactualizado, o que hubo un cambio de
    asignación reciente que todavía no se cargó. Va al panel de salud
    en rojo (no en amarillo).
    """
    vendedores_fc = set(df_fc["vendedor"].dropna().unique())
    vendedores_con_cartera = set(df_clientes["vendedor"].dropna().unique())
    return sorted(vendedores_fc - vendedores_con_cartera)


# =====================================================================
# 6. Orquestador del pipeline
# =====================================================================

def prepare_facturacion(
    df_fc_raw: pd.DataFrame,
    df_clientes: pd.DataFrame,
    df_productos: pd.DataFrame,
    df_combos: pd.DataFrame,
) -> Tuple[pd.DataFrame, dict]:
    """
    Aplica el pipeline completo a una planilla de facturación cargada.

    Orden de operaciones:
      1. Filtrar NCF descuentos (las que no tienen sku).
      2. Validar moneda — separa UYU del resto, descarta no-UYU.
      3. Join contra clientes — marca status, reemplaza razón social.
      4. Clasificar SKUs — agrega sub_rubro, nombre, origen.
      5. Calcular validaciones cross-tabla.

    Devuelve `(df_final, health)` donde:
      - df_final: DataFrame listo para metrics.py. Tiene TODAS las filas
        que pasaron el filtrado de NCF y la validación de moneda
        (incluyendo las de cliente no encontrado o SKU sin asignar —
        no se descartan acá).
      - health: dict con contadores y listas para el panel de salud.
        Las llaves son estables; las usa el panel de salud en app.py.

    Esta función NO recibe argumentos opcionales y NO tiene side effects.
    Es la frontera entre data_loader y metrics.
    """
    health = {
        "filas_iniciales": len(df_fc_raw),
        # OP excluidos (vendedores operativos)
        "filas_op_excluidas": 0,
        "vendedores_op_excluidos": [],
        # NCF
        "ncf_descartadas_descuento": 0,
        # Monto sumado por las NCF de descuentos comerciales que se
        # descartaron. Es el "delta de transparencia" entre el total
        # mostrado en el dashboard y la suma cruda de la planilla. Se
        # mantiene como número original (signo natural; los descuentos
        # son negativos).
        "monto_ncf_descartado": 0.0,
        # Moneda
        "filas_no_uyu": 0,
        "monedas_no_uyu": [],
        # Join clientes
        "filas_doc_faltante": 0,
        "filas_cliente_no_encontrado": 0,
        "clientes_duplicados": [],
        # SKU
        "skus_sin_asignar": [],
        # Cross-tabla
        "vendedores_sin_cartera": [],
        # Final
        "filas_finales": 0,
    }

    # Pre-check: clientes duplicados (no aborta, pero reporta)
    health["clientes_duplicados"] = check_clientes_duplicados(df_clientes)

    # 0. Excluir vendedores operativos (OPJESICA, OPVALERIA).
    # Es el primer step, antes de cualquier filtrado de negocio, para que el
    # resto del pipeline no procese estas filas. Ver decisions.md 2026-04-10.
    df, df_excluidas_op = exclude_op_vendedores(df_fc_raw)
    health["filas_op_excluidas"] = len(df_excluidas_op)
    health["vendedores_op_excluidos"] = sorted(
        df_excluidas_op["vendedor"].dropna().astype(str).unique().tolist()
    )
    # Snapshot del df post-OP-exclude para usar en el cross-tabla check más
    # abajo (queremos detectar vendedores sin cartera SOBRE las filas que
    # sí cuentan, no incluyendo OPJESICA/OPVALERIA).
    df_post_op = df.copy()

    # 1. NCF
    df, ncf_descartadas = filter_notas_credito(df)
    health["ncf_descartadas_descuento"] = len(ncf_descartadas)
    health["monto_ncf_descartado"] = (
        float(ncf_descartadas["monto"].sum()) if not ncf_descartadas.empty else 0.0
    )

    # 2. Moneda
    df, df_no_uyu = validate_moneda(df)
    health["filas_no_uyu"] = len(df_no_uyu)
    health["monedas_no_uyu"] = sorted(
        df_no_uyu["moneda"].dropna().astype(str).unique().tolist()
    )

    # 3. Join clientes
    df = join_facturacion_clientes(df, df_clientes)
    health["filas_doc_faltante"] = int((df["status_cliente"] == STATUS_DOC_FALTANTE).sum())
    health["filas_cliente_no_encontrado"] = int(
        (df["status_cliente"] == STATUS_CLIENTE_NO_ENCONTRADO).sum()
    )

    # 4. Clasificación de SKU
    df = classify_skus(df, df_productos, df_combos)
    sin_asignar_mask = df["origen_clasificacion"] == ORIGEN_SIN_ASIGNAR
    health["skus_sin_asignar"] = sorted(
        df.loc[sin_asignar_mask, "sku"].dropna().astype(str).unique().tolist()
    )

    # 5. Cross-tabla: vendedores sin cartera.
    # Se evalúa contra `df_post_op` (post-OP-exclude pero pre-NCF-filter)
    # por dos razones combinadas:
    #   - Excluir OPJESICA/OPVALERIA: no queremos verlos flagueados como
    #     huérfanos porque por definición no tienen cartera (decisión 2026-04-10).
    #   - Pre-NCF-filter: si un vendedor solo factura NCF descuento, igual
    #     queremos detectar que existe y no tiene cartera asignada.
    health["vendedores_sin_cartera"] = check_vendedores_sin_cartera(df_post_op, df_clientes)

    health["filas_finales"] = len(df)
    return df, health
