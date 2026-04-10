"""
metrics.py — Cálculo de ventas y cobertura para el dashboard.

Recibe los DataFrames preparados por `transforms.py` (con todas las
clasificaciones aplicadas y columnas en snake_case) y devuelve
DataFrames listos para mostrar en las vistas Streamlit.

Funciones puras: no importa streamlit, no escribe a disco. El cache de
UI se aplica en `app.py` envolviendo estas funciones con
`@st.cache_data`.

Reglas críticas implementadas (claude.md.txt, sección "Métricas del
dashboard"):

  - **Monto/unidades**: se calcula sobre TODAS las filas (FAC + NCF con
    sku). Las NCF tienen `monto` y `unidades` negativos y netean
    correctamente con las FAC. Eso es el comportamiento deseado.

  - **Cobertura de clientes**: SOLO cuenta filas con `tipo == 'FAC'`.
    Las NCF nunca cuentan para cobertura, ni siquiera las devoluciones
    reales. Es regla explícita del manual.

  - **Match cliente ↔ vendedor para cobertura**: el vendedor de la
    OPERACIÓN tiene que ser el mismo que el vendedor ASIGNADO al
    cliente en cartera. Si V1 tiene a C1 en cartera pero V2 le facturó
    a C1, ese cliente no cuenta como "con venta" para V1 (V1 no le
    vendió) ni para V2 (no está en su cartera). Es matching estricto
    via merge por `(vendedor, documento)`.
"""

from __future__ import annotations

import pandas as pd

TIPO_FAC = "FAC"


# =====================================================================
# Helpers internos
# =====================================================================

def _cartera_unica(df_clientes: pd.DataFrame) -> pd.DataFrame:
    """
    Devuelve los pares únicos `(vendedor, documento)` del maestro de
    clientes, sin nulos. Es el universo contra el que se evalúan
    todas las métricas de cobertura.
    """
    return (
        df_clientes[["vendedor", "documento"]]
        .dropna()
        .drop_duplicates()
        .reset_index(drop=True)
    )


def _clientes_asignados_por_vendedor(df_clientes: pd.DataFrame) -> pd.DataFrame:
    """
    Para cada vendedor, count de clientes (documentos únicos) en cartera.
    Devuelve DataFrame con columnas: vendedor, clientes_asignados.
    """
    return (
        df_clientes.dropna(subset=["vendedor", "documento"])
        .groupby("vendedor")["documento"]
        .nunique()
        .reset_index(name="clientes_asignados")
    )


def _fac_en_cartera_propia(
    df_fc: pd.DataFrame, df_clientes: pd.DataFrame
) -> pd.DataFrame:
    """
    Subset de facturación que cumple AMBAS condiciones:
      1. tipo == 'FAC' (regla del manual: NCF no cuentan para cobertura).
      2. el (vendedor de la operación, documento del cliente) está en la
         cartera del MISMO vendedor en clientes.xlsx.

    Es el insumo común de las tres funciones de cobertura.
    """
    df_fac = df_fc[df_fc["tipo"] == TIPO_FAC]
    cartera = _cartera_unica(df_clientes)
    return df_fac.merge(cartera, on=["vendedor", "documento"], how="inner")


def _calcular_pct_cobertura(con_venta: int, asignados: int) -> float:
    """Cobertura = con_venta / asignados, en % redondeado a 2 decimales."""
    if asignados == 0:
        return 0.0
    return round(con_venta / asignados * 100, 2)


# =====================================================================
# VENTAS
# =====================================================================

def ventas_por_vendedor(df_fc: pd.DataFrame) -> pd.DataFrame:
    """
    Totales de venta por vendedor.

    Suma `monto` y `unidades` agrupando por `vendedor`. Las NCF
    (devoluciones reales) se incluyen en la suma con sus valores
    negativos, neteando con las FAC.

    Devuelve DataFrame con columnas:
      vendedor, monto_total, unidades_totales

    Ordenado por monto_total descendente.
    """
    if df_fc.empty:
        return pd.DataFrame(columns=["vendedor", "monto_total", "unidades_totales"])

    return (
        df_fc.groupby("vendedor", dropna=False)
        .agg(monto_total=("monto", "sum"), unidades_totales=("unidades", "sum"))
        .reset_index()
        .sort_values("monto_total", ascending=False)
        .reset_index(drop=True)
    )


def ventas_por_vendedor_y_sub_rubro(df_fc: pd.DataFrame) -> pd.DataFrame:
    """
    Desglose de ventas por (vendedor, sub_rubro).

    Útil para pivotar a una matriz vendedor × sub_rubro en la vista.
    Las NCF netean en su sub_rubro correspondiente (mismo SKU).

    Devuelve DataFrame con columnas:
      vendedor, sub_rubro, monto, unidades
    """
    if df_fc.empty:
        return pd.DataFrame(columns=["vendedor", "sub_rubro", "monto", "unidades"])

    return (
        df_fc.groupby(["vendedor", "sub_rubro"], dropna=False)
        .agg(monto=("monto", "sum"), unidades=("unidades", "sum"))
        .reset_index()
        .sort_values(["vendedor", "monto"], ascending=[True, False])
        .reset_index(drop=True)
    )


def filtrar_por_sku(df_fc: pd.DataFrame, sku: str) -> pd.DataFrame:
    """
    Filtra el DataFrame de facturación a las filas de un SKU exacto.

    Útil para componer otras métricas: por ejemplo, llamar a
    `ventas_por_vendedor(filtrar_por_sku(df, 'PROD123'))` da la venta
    de ese SKU por vendedor.
    """
    return df_fc[df_fc["sku"] == sku].copy().reset_index(drop=True)


def filtrar_por_combo(
    df_fc: pd.DataFrame, sku_combo: str | None = None
) -> pd.DataFrame:
    """
    Filtra a filas de combos.

    - Sin argumento: todas las filas con `sub_rubro == 'COMBO'`.
    - Con `sku_combo`: solo el combo específico.

    Notar que después de `transforms.classify_skus()`, los combos
    quedan marcados con `sub_rubro = 'COMBO'`. No depende de listar
    SKUs uno por uno.
    """
    df = df_fc[df_fc["sub_rubro"] == "COMBO"]
    if sku_combo is not None:
        df = df[df["sku"] == sku_combo]
    return df.copy().reset_index(drop=True)


# =====================================================================
# COBERTURA
# =====================================================================

def cobertura_por_vendedor(
    df_fc: pd.DataFrame, df_clientes: pd.DataFrame
) -> pd.DataFrame:
    """
    Cobertura general por vendedor.

    Para cada vendedor en cartera, calcula:
      - clientes_asignados: cuántos clientes distintos tiene en cartera.
      - clientes_con_venta: cuántos de esos clientes recibieron al
        menos una venta tipo FAC del MISMO vendedor en el período.
      - cobertura_pct: clientes_con_venta / clientes_asignados (%).

    Solo se reportan vendedores que aparecen en `clientes.xlsx` (los
    que tienen cartera). Vendedores que facturan pero no tienen
    cartera (los huérfanos que detecta `check_vendedores_sin_cartera`)
    NO aparecen acá — se reportan en el panel de salud.

    NCF nunca cuentan para esta métrica.
    """
    asignados = _clientes_asignados_por_vendedor(df_clientes)
    fac_en_cartera = _fac_en_cartera_propia(df_fc, df_clientes)

    con_venta = (
        fac_en_cartera.groupby("vendedor")["documento"]
        .nunique()
        .reset_index(name="clientes_con_venta")
    )

    result = asignados.merge(con_venta, on="vendedor", how="left")
    result["clientes_con_venta"] = (
        result["clientes_con_venta"].fillna(0).astype(int)
    )
    result["cobertura_pct"] = result.apply(
        lambda r: _calcular_pct_cobertura(
            r["clientes_con_venta"], r["clientes_asignados"]
        ),
        axis=1,
    )
    return result.sort_values("cobertura_pct", ascending=False).reset_index(drop=True)


def cobertura_por_sub_rubro(
    df_fc: pd.DataFrame, df_clientes: pd.DataFrame
) -> pd.DataFrame:
    """
    Cobertura desagregada por sub_rubro.

    Para cada (vendedor, sub_rubro), cuántos clientes asignados al
    vendedor recibieron al menos una venta FAC de un producto de ese
    sub_rubro (del mismo vendedor).

    El denominador (`clientes_asignados`) es el TOTAL de clientes en
    cartera del vendedor, NO los que tienen venta de ese sub_rubro.
    Esto es a propósito: la métrica responde "del total de mi cartera,
    a cuántos les vendí algo de este sub_rubro".

    Devuelve DataFrame con columnas:
      vendedor, sub_rubro, clientes_asignados,
      clientes_con_venta_sub_rubro, cobertura_pct
    """
    asignados = _clientes_asignados_por_vendedor(df_clientes)
    fac_en_cartera = _fac_en_cartera_propia(df_fc, df_clientes)

    con_venta_sr = (
        fac_en_cartera.groupby(["vendedor", "sub_rubro"])["documento"]
        .nunique()
        .reset_index(name="clientes_con_venta_sub_rubro")
    )

    result = con_venta_sr.merge(asignados, on="vendedor", how="left")
    result["cobertura_pct"] = result.apply(
        lambda r: _calcular_pct_cobertura(
            r["clientes_con_venta_sub_rubro"], r["clientes_asignados"]
        ),
        axis=1,
    )
    result = result[
        [
            "vendedor",
            "sub_rubro",
            "clientes_asignados",
            "clientes_con_venta_sub_rubro",
            "cobertura_pct",
        ]
    ]
    return result.sort_values(
        ["vendedor", "cobertura_pct"], ascending=[True, False]
    ).reset_index(drop=True)


def cobertura_por_sku(
    df_fc: pd.DataFrame, df_clientes: pd.DataFrame, sku: str
) -> pd.DataFrame:
    """
    Cobertura para un SKU específico.

    Para cada vendedor con cartera, cuántos de sus clientes asignados
    compraron el SKU (con FAC) en el período.

    Devuelve DataFrame con columnas:
      vendedor, clientes_asignados, clientes_con_venta_sku, cobertura_pct
    """
    asignados = _clientes_asignados_por_vendedor(df_clientes)

    # Filtrar fc al SKU específico, en FAC, en cartera del propio vendedor
    df_fac_sku = df_fc[(df_fc["tipo"] == TIPO_FAC) & (df_fc["sku"] == sku)]
    cartera = _cartera_unica(df_clientes)
    fac_sku_cartera = df_fac_sku.merge(
        cartera, on=["vendedor", "documento"], how="inner"
    )

    con_venta_sku = (
        fac_sku_cartera.groupby("vendedor")["documento"]
        .nunique()
        .reset_index(name="clientes_con_venta_sku")
    )

    result = asignados.merge(con_venta_sku, on="vendedor", how="left")
    result["clientes_con_venta_sku"] = (
        result["clientes_con_venta_sku"].fillna(0).astype(int)
    )
    result["cobertura_pct"] = result.apply(
        lambda r: _calcular_pct_cobertura(
            r["clientes_con_venta_sku"], r["clientes_asignados"]
        ),
        axis=1,
    )
    return result.sort_values("cobertura_pct", ascending=False).reset_index(drop=True)
