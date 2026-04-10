"""
views/resumen.py — Vista principal del dashboard.

Métricas globales por vendedor en dos timeframes paralelos (semana | mes).
Es la primera vista que ve el Jefe de Ventas al entrar al app: tiene que
dar la foto rápida de cómo viene el período.

Esta vista NO calcula nada por sí sola. Llama a las funciones puras de
metrics.py con los DataFrames ya preparados que le pasa app.py.
"""

import pandas as pd
import streamlit as st

import metrics


def render(
    df_sem: pd.DataFrame,
    df_mes: pd.DataFrame,
    df_clientes: pd.DataFrame,
) -> None:
    """
    Renderiza la vista de resumen.

    Args:
        df_sem: facturación semanal (post `transforms.prepare_facturacion`).
        df_mes: facturación mensual (post `transforms.prepare_facturacion`).
        df_clientes: maestro de clientes (post `data_loader.load_clientes`).
    """
    st.subheader("Resumen del período")

    # ----- Big numbers: total semana vs mes -----
    col_sem, col_mes = st.columns(2)
    with col_sem:
        total_sem = float(df_sem["monto"].sum()) if not df_sem.empty else 0.0
        st.metric("Total semana (UYU)", f"{total_sem:,.0f}")
    with col_mes:
        total_mes = float(df_mes["monto"].sum()) if not df_mes.empty else 0.0
        st.metric("Total mes (UYU)", f"{total_mes:,.0f}")

    st.divider()

    # ----- Ventas por vendedor: tabla con ambos timeframes lado a lado -----
    st.markdown("### Ventas por vendedor")
    st.caption(
        "Monto y unidades del período. Las NCF (devoluciones reales) "
        "netean con las FAC."
    )

    ventas_sem = metrics.ventas_por_vendedor(df_sem).rename(
        columns={
            "monto_total": "monto_semana",
            "unidades_totales": "unidades_semana",
        }
    )
    ventas_mes = metrics.ventas_por_vendedor(df_mes).rename(
        columns={
            "monto_total": "monto_mes",
            "unidades_totales": "unidades_mes",
        }
    )

    # Outer join para conservar vendedores que solo aparecen en uno de los dos
    combined = ventas_sem.merge(ventas_mes, on="vendedor", how="outer").fillna(0)
    for col in ["monto_semana", "unidades_semana", "monto_mes", "unidades_mes"]:
        combined[col] = combined[col].astype(int)
    combined = combined.sort_values("monto_mes", ascending=False).reset_index(drop=True)

    st.dataframe(
        combined.style.format(
            {
                "monto_semana": "{:,}",
                "unidades_semana": "{:,}",
                "monto_mes": "{:,}",
                "unidades_mes": "{:,}",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

    # ----- Cobertura general (mes) -----
    st.divider()
    st.markdown("### Cobertura general por vendedor (mes)")
    st.caption(
        "De los clientes asignados a cada vendedor, cuántos recibieron "
        "al menos una venta FAC en el mes."
    )

    cob = metrics.cobertura_por_vendedor(df_mes, df_clientes)
    if cob.empty:
        st.info("Sin datos de cobertura para mostrar.")
    else:
        st.dataframe(
            cob.style.format({"cobertura_pct": "{:.1f}%"}),
            use_container_width=True,
            hide_index=True,
        )
