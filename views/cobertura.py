"""
views/cobertura.py — Métricas de cobertura de clientes.

Tres bloques:
  1. Cobertura general por vendedor (cuántos de sus clientes asignados
     recibieron al menos una venta FAC en el período).
  2. Cobertura desagregada por sub-rubro (de la cartera de cada vendedor,
     a cuántos les vendió algo de cada sub-rubro).
  3. Cobertura para un SKU específico (selector).

Recordar la regla del manual: las NCF NUNCA cuentan para cobertura,
ni siquiera las que son devoluciones reales. Esa lógica vive en
metrics.py — esta vista solo muestra los resultados.
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
        df_sem, df_mes, df_clientes: ver views/resumen.py.
        health_sem, health_mes: por consistencia de firma con resumen.py.
            Esta vista no los usa actualmente.
    """
    del health_sem, health_mes  # actualmente no se usan acá

    st.subheader("Cobertura de clientes")

    # ----- Selector de timeframe -----
    timeframe = st.radio(
        "Período",
        options=["Semana", "Mes"],
        horizontal=True,
        key="cobertura_tf",
    )
    df = df_sem if timeframe == "Semana" else df_mes

    if df.empty:
        st.info("No hay datos para el período seleccionado.")
        return

    # ----- Bloque 1: cobertura general -----
    st.markdown("### Cobertura general por vendedor")
    st.caption(
        "De los clientes asignados a cada vendedor, cuántos recibieron "
        "al menos una venta FAC en el período. Las NCF no cuentan para "
        "esta métrica."
    )
    cob = metrics.cobertura_por_vendedor(df, df_clientes)
    if cob.empty:
        st.info("Sin datos de cobertura general.")
    else:
        st.dataframe(
            cob.style.format({"cobertura_pct": "{:.1f}%"}),
            use_container_width=True,
            hide_index=True,
        )

    # ----- Bloque 2: cobertura por sub-rubro -----
    st.divider()
    st.markdown("### Cobertura por sub-rubro")
    st.caption(
        "Para cada (vendedor, sub-rubro), cuántos clientes asignados al "
        "vendedor compraron al menos un producto de ese sub-rubro."
    )
    cob_sr = metrics.cobertura_por_sub_rubro(df, df_clientes)
    if cob_sr.empty:
        st.info("Sin datos de cobertura por sub-rubro.")
    else:
        st.dataframe(
            cob_sr.style.format({"cobertura_pct": "{:.1f}%"}),
            use_container_width=True,
            hide_index=True,
        )

    # ----- Bloque 3: cobertura por SKU específico -----
    st.divider()
    st.markdown("### Cobertura por SKU específico")
    st.caption(
        "Para un SKU dado: cuántos de los clientes asignados a cada "
        "vendedor lo compraron en el período (con FAC)."
    )

    skus = sorted(df["sku"].dropna().astype(str).unique().tolist())
    if not skus:
        st.info("Sin SKUs para mostrar en el período seleccionado.")
        return

    sku_sel = st.selectbox(
        "Seleccionar SKU",
        options=skus,
        key="cobertura_sku_sel",
    )
    cob_sku = metrics.cobertura_por_sku(df, df_clientes, sku_sel)
    if cob_sku.empty:
        st.info("Sin datos de cobertura para el SKU seleccionado.")
    else:
        st.dataframe(
            cob_sku.style.format({"cobertura_pct": "{:.1f}%"}),
            use_container_width=True,
            hide_index=True,
        )
