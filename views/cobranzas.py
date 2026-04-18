"""
views/cobranzas.py — Tab de Cobranzas.

Dashboard de deuda viva por cliente y vendedor, construido sobre los
campos `saldo`, `fecha_vencimiento` y `condicion_venta` que
`api_loader.load_fc_api` agrega al DataFrame de facturación
(discovery 2026-04-18: el detalle del comprobante ya trae saldo
pendiente, no hay que usar `/api/cobranzas/search`).

Esta tab SOLO funciona en modo API. El Modo Manual Secundario no
tiene esos campos (los xlsx de Contabilium no los exportan por
defecto). Si se entra en Modo Manual, se muestra un aviso.

Estructura:
  1. KPIs arriba (deuda total, % vencida, días promedio de deuda).
  2. Aging por cliente (matriz con buckets).
  3. Top deudores.
  4. Días promedio de deuda por vendedor.

Las métricas se calculan siempre sobre `df_mes` — la deuda "viva" es
estado actual, no un concepto de período. `df_mes` es simplemente
el DataFrame más reciente que tiene los campos de cobranzas.
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
    """Firma uniforme con las otras vistas. `df_mes` es el input primario
    (contiene los campos de cobranzas); el resto no se usa."""
    del df_sem, df_clientes, health_sem, health_mes

    st.subheader("Cobranzas")
    st.caption(
        "Estado actual de la deuda viva: comprobantes con saldo pendiente, "
        "aging por cliente, top deudores. Las métricas se calculan sobre "
        "los comprobantes del mes sincronizado (ese es el rango más "
        "reciente con datos de cobranzas). Solo disponible en modo API."
    )

    # Degradación elegante en Modo Manual
    if st.session_state.get("fuente_activa") != "api":
        st.info(
            "La tab de Cobranzas solo funciona en **modo API**. "
            "El Modo Manual Secundario no incluye los campos de saldo, "
            "vencimiento y pagos que vienen con el detalle del "
            "comprobante de Contabilium."
        )
        return

    if df_mes is None or df_mes.empty or "saldo" not in df_mes.columns:
        st.info(
            "No hay datos de cobranzas para mostrar. Tocá **Sincronizar** "
            "en la sidebar para pullear facturación con los campos de "
            "saldo y vencimiento."
        )
        return

    # ===== Bloque 1: KPIs =====
    resumen = metrics.deuda_vencida_vs_corriente(df_mes)
    dpd_vendedor = metrics.dias_promedio_deuda_por_vendedor(df_mes)

    col_a, col_b, col_c, col_d = st.columns(4)
    col_a.metric(
        "Deuda total viva (UYU)",
        f"{resumen['total']:,.0f}",
        help="Suma del saldo de todos los comprobantes FAC con saldo > 0.",
    )
    col_b.metric(
        "Deuda vencida (UYU)",
        f"{resumen['vencida']:,.0f}",
        delta=f"{resumen['pct_vencida']:.1f}% del total",
        delta_color="inverse" if resumen["pct_vencida"] > 30 else "off",
        help="Comprobantes con fecha_vencimiento anterior a hoy.",
    )
    col_c.metric(
        "Deuda corriente (UYU)",
        f"{resumen['corriente']:,.0f}",
        help="Comprobantes cuyo vencimiento aún no llegó (o no tiene fecha).",
    )
    # Días promedio de deuda global (ponderado por comprobante)
    dpd_global = (
        float(dpd_vendedor["dias_promedio_deuda"].mean())
        if not dpd_vendedor.empty
        else 0.0
    )
    col_d.metric(
        "Días promedio de deuda",
        f"{dpd_global:.0f} días",
        help=(
            "Promedio simple entre vendedores de los días que tienen sus "
            "facturas pendientes (hoy - fecha de emisión)."
        ),
    )

    # ===== Bloque 2: Aging por cliente =====
    st.divider()
    st.markdown("### Aging por cliente")
    st.caption(
        "Para cada cliente con deuda viva, cuánto debe en cada bucket "
        "según los días desde su **fecha de vencimiento**. **Al día** = "
        "aún no vencido. **Sin vencimiento** = el ERP no registró plazo "
        "(normalmente son contado)."
    )

    aging = metrics.aging_por_cliente(df_mes)
    if aging.empty:
        st.success("No hay deuda viva registrada — todo cobrado.")
    else:
        # Filtro opcional por vendedor
        _vendedores = sorted(
            aging["vendedor"].dropna().astype(str).unique().tolist()
        )
        _sel = st.selectbox(
            "Filtrar por vendedor",
            options=["(Todos)"] + _vendedores,
            key="aging_vendedor_sel",
        )
        vista = aging if _sel == "(Todos)" else aging[aging["vendedor"] == _sel]

        st.dataframe(
            vista.style.format(
                {
                    "al_dia": "{:,.0f}",
                    "b_0_30": "{:,.0f}",
                    "b_31_60": "{:,.0f}",
                    "b_61_90": "{:,.0f}",
                    "b_90_mas": "{:,.0f}",
                    "sin_vencimiento": "{:,.0f}",
                    "deuda_total": "{:,.0f}",
                }
            ),
            use_container_width=True,
            hide_index=True,
            column_config={
                "al_dia": st.column_config.NumberColumn("Al día"),
                "b_0_30": st.column_config.NumberColumn("0-30 días"),
                "b_31_60": st.column_config.NumberColumn("31-60 días"),
                "b_61_90": st.column_config.NumberColumn("61-90 días"),
                "b_90_mas": st.column_config.NumberColumn("90+ días"),
                "sin_vencimiento": st.column_config.NumberColumn("Sin venc."),
                "deuda_total": st.column_config.NumberColumn("Total"),
            },
        )
        st.caption(f"Total: {len(vista)} cliente(s) con deuda viva.")

    # ===== Bloque 3: Top deudores =====
    st.divider()
    st.markdown("### Top deudores")
    st.caption(
        "Los clientes con mayor saldo pendiente. **Comprobante más viejo**: "
        "fecha del comprobante con saldo > 0 más antiguo — útil para "
        "detectar deuda crónica."
    )

    top_n = st.slider(
        "Cantidad a mostrar",
        min_value=5,
        max_value=50,
        value=20,
        step=5,
        key="top_deudores_n",
    )
    top = metrics.top_deudores(df_mes, n=top_n)
    if top.empty:
        st.info("Sin deudores para mostrar.")
    else:
        vista_top = top.copy()
        vista_top["comprobante_mas_viejo"] = vista_top[
            "comprobante_mas_viejo"
        ].apply(
            lambda d: "—" if pd.isna(d) else pd.Timestamp(d).strftime("%Y-%m-%d")
        )
        st.dataframe(
            vista_top.style.format(
                {
                    "deuda_total": "{:,.0f}",
                    "comprobantes_pendientes": "{:,}",
                }
            ),
            use_container_width=True,
            hide_index=True,
        )

    # ===== Bloque 4: Días promedio de deuda por vendedor =====
    st.divider()
    st.markdown("### Días promedio de deuda por vendedor")
    st.caption(
        "Para cada vendedor, promedio simple de días que tiene abiertas "
        "sus facturas con saldo > 0 (hoy - fecha de emisión). Valores "
        "altos sugieren que el vendedor deja envejecer la cartera; "
        "valores bajos indican rotación rápida o baja cartera vencida."
    )
    if dpd_vendedor.empty:
        st.info("Sin datos para calcular días promedio por vendedor.")
    else:
        st.dataframe(
            dpd_vendedor.style.format(
                {
                    "comprobantes_pendientes": "{:,}",
                    "deuda_total": "{:,.0f}",
                    "dias_promedio_deuda": "{:.1f}",
                }
            ),
            use_container_width=True,
            hide_index=True,
        )
