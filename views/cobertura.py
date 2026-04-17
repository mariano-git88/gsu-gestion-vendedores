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
    # "Trimestre" aparece sólo si el modo API sincronizó un trimestre
    # (session_state.df_tri). En Modo Manual el trimestre no existe.
    df_tri = st.session_state.get("df_tri")
    opciones = ["Semana", "Mes"]
    if df_tri is not None and not df_tri.empty:
        opciones.append("Trimestre")

    timeframe = st.radio(
        "Período",
        options=opciones,
        horizontal=True,
        key="cobertura_tf",
    )
    if timeframe == "Semana":
        df = df_sem
    elif timeframe == "Mes":
        df = df_mes
    else:
        df = df_tri

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

    # ----- Bloque 4: clientes que no compraron este SKU en el mes -----
    # Esta sección SIEMPRE usa df_mes (lo que va del mes), independiente
    # del selector de Semana/Mes de arriba. Reutiliza el sku_sel del bloque
    # anterior para evitar duplicar UI.
    st.divider()
    st.markdown("### Clientes que NO compraron este SKU en el mes")
    st.caption(
        "De la cartera asignada a cada vendedor, los clientes que NO "
        "recibieron una venta tipo FAC del SKU seleccionado en lo que va "
        "del mes. **Siempre sobre el mes**, independiente del selector "
        "de período de arriba. El criterio es estricto: si el cliente le "
        "compró el SKU a otro vendedor distinto al asignado, igual aparece."
    )

    # Edge case: el SKU seleccionado puede no existir en df_mes
    # (por ejemplo, si arriba está seleccionada Semana y se eligió un
    # SKU que solo facturó esta semana). Avisar antes de calcular.
    if sku_sel not in set(df_mes["sku"].dropna().astype(str).unique()):
        st.info(
            f"El SKU `{sku_sel}` no aparece en lo que va del mes. "
            "No se puede calcular la lista de no-compradores sobre el mes."
        )
    else:
        no_compradores = metrics.clientes_sin_compra_sku(df_mes, df_clientes, sku_sel)
        if no_compradores.empty:
            st.success(
                "Todos los clientes en cartera ya compraron este SKU "
                "este mes (de su vendedor asignado)."
            )
        else:
            st.dataframe(
                no_compradores,
                use_container_width=True,
                hide_index=True,
            )
            st.caption(f"Total: {len(no_compradores)} clientes sin compra del SKU.")
