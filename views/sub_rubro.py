"""
views/sub_rubro.py — Desglose de ventas por sub-rubro.

Permite explorar el mix de productos vendidos por cada vendedor,
con filtros por sub-rubro y por SKU específico, y un pivot final
vendedor × sub-rubro para tener la matriz completa de un vistazo.
"""

import pandas as pd
import streamlit as st

import metrics


def render(
    df_sem: pd.DataFrame,
    df_mes: pd.DataFrame,
    df_clientes: pd.DataFrame,
    health_sem: dict | None = None,
    health_mes: dict | None = None,
) -> None:
    """
    Args:
        df_sem: facturación semanal preparada.
        df_mes: facturación mensual preparada.
        df_clientes: maestro de clientes (no se usa directamente acá,
            pero se recibe por consistencia con las otras vistas).
        health_sem, health_mes: dicts de salud de cada timeframe. Se
            reciben por consistencia de firma con resumen.py — esta vista
            no los usa actualmente.
    """
    del health_sem, health_mes  # actualmente no se usan acá
    st.subheader("Desglose por sub-rubro")

    # ----- Selector de timeframe -----
    timeframe = st.radio(
        "Período",
        options=["Semana", "Mes"],
        horizontal=True,
        key="sub_rubro_tf",
    )
    df = df_sem if timeframe == "Semana" else df_mes

    if df.empty:
        st.info("No hay datos para el período seleccionado.")
        return

    # ----- Filtros opcionales -----
    col_sr, col_sku = st.columns(2)
    with col_sr:
        sub_rubros = sorted(
            df["sub_rubro"].dropna().astype(str).unique().tolist()
        )
        sub_rubro_filter = st.selectbox(
            "Filtrar por sub-rubro",
            options=["(todos)"] + sub_rubros,
            key="sub_rubro_filter",
        )
    with col_sku:
        skus = sorted(df["sku"].dropna().astype(str).unique().tolist())
        sku_filter = st.selectbox(
            "Filtrar por SKU específico",
            options=["(todos)"] + skus,
            key="sku_filter",
        )

    df_filtered = df.copy()
    if sub_rubro_filter != "(todos)":
        df_filtered = df_filtered[df_filtered["sub_rubro"] == sub_rubro_filter]
    if sku_filter != "(todos)":
        df_filtered = df_filtered[df_filtered["sku"] == sku_filter]

    # ----- Tabla por (vendedor, sub_rubro) -----
    desglose = metrics.ventas_por_vendedor_y_sub_rubro(df_filtered)

    if desglose.empty:
        st.info("No hay datos con los filtros aplicados.")
        return

    st.markdown("### Ventas por (vendedor, sub-rubro)")
    st.dataframe(
        desglose.style.format({"monto": "{:,.0f}", "unidades": "{:,.0f}"}),
        use_container_width=True,
        hide_index=True,
    )

    # ----- Pivot vendedor × sub_rubro -----
    st.markdown("### Pivot vendedor × sub-rubro (monto)")
    st.caption(
        "Cada celda es la suma de monto del vendedor para ese sub-rubro "
        "en el período (con NCF neteadas)."
    )
    try:
        pivot = (
            desglose.pivot(index="vendedor", columns="sub_rubro", values="monto")
            .fillna(0)
            .astype(int)
        )
        st.dataframe(
            pivot.style.format("{:,}"),
            use_container_width=True,
        )
    except Exception as e:
        # Por si algún caso edge rompe el pivot, no fallar la vista entera
        st.warning(f"No se pudo generar el pivot: {e}")
