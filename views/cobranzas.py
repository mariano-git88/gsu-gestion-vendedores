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

    if df_mes is None or df_mes.empty:
        st.info(
            "No hay datos de cobranzas para mostrar. Tocá **Sincronizar** "
            "en la sidebar para pullear facturación con los campos de "
            "saldo y vencimiento."
        )
        return

    if "saldo" not in df_mes.columns:
        # Caso típico: el sync usó un caché previo a que agregáramos los
        # campos de cobranzas al parser. El cache de `_api_sync_fc` se
        # indexa por (fecha_desde, fecha_hasta) — si los rangos no
        # cambiaron desde el sync anterior, Streamlit devuelve el DF
        # viejo aunque el código nuevo ya exponga saldo/vencimiento.
        st.warning(
            "**El sync cacheado no tiene los campos de cobranzas.** "
            "Esto pasa si ya habías sincronizado con una versión anterior "
            "del código y el caché de 1 h todavía está vigente.\n\n"
            "Solución: tocá **'Resync forzado (bypass caché)'** en la "
            "sidebar (debajo del timestamp del último sync) y después "
            "volvé a tocar **Sincronizar**. Eso pullea fresco y los "
            "campos de saldo/vencimiento aparecen acá."
        )
        return

    # ===== Bloque 1: KPIs =====
    resumen = metrics.deuda_vencida_vs_corriente(df_mes)
    dpd_vendedor = metrics.dias_promedio_deuda_por_vendedor(df_mes)

    # 2 filas de 2 columnas para que los montos completos de 7-8 dígitos
    # entren sin truncarse (UYU puede dar montos grandes).
    row1_a, row1_b = st.columns(2)
    row1_a.metric(
        "Deuda total viva (UYU)",
        f"{resumen['total']:,.0f}",
        help="Suma del saldo de todos los comprobantes FAC con saldo > 0.",
    )
    row1_b.metric(
        "Deuda vencida (UYU)",
        f"{resumen['vencida']:,.0f}",
        delta=f"{resumen['pct_vencida']:.1f}% del total",
        delta_color="inverse" if resumen["pct_vencida"] > 30 else "off",
        help="Comprobantes con fecha_vencimiento anterior a hoy.",
    )

    row2_a, row2_b = st.columns(2)
    row2_a.metric(
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
    row2_b.metric(
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

    # ===== Bloque 5: Venta reciente + deuda vieja =====
    st.divider()
    st.markdown("### Clientes con venta reciente y deuda vieja")
    st.caption(
        "Clientes a los que se les facturó en los **últimos 30 días** y "
        "que al mismo tiempo tienen comprobantes vencidos hace **más de "
        "90 días**. Señal de riesgo crediticio: se les sigue vendiendo "
        "pese a que la cobranza está retrasada. **Vendedor** = el que "
        "hizo la venta más reciente."
    )

    # Para cubrir bien la deuda vieja >90d necesitamos un rango amplio.
    # Preferimos histórico 12m, después trimestre, sino df_mes.
    df_hist12 = st.session_state.get("df_hist12")
    df_tri = st.session_state.get("df_tri")
    if df_hist12 is not None and not df_hist12.empty:
        fuente_cruce = df_hist12
        fuente_label = "histórico de 12 meses"
    elif df_tri is not None and not df_tri.empty:
        fuente_cruce = df_tri
        fuente_label = "trimestre (3 meses)"
    else:
        fuente_cruce = df_mes
        fuente_label = "mes en curso"

    cruce = metrics.clientes_venta_reciente_con_deuda_vieja(fuente_cruce)
    st.caption(
        f"Fuente: **{fuente_label}**. "
        + (
            "Cargá el histórico 12m desde la sidebar para máxima cobertura."
            if fuente_label != "histórico de 12 meses"
            else "Máxima cobertura."
        )
    )

    if cruce.empty:
        st.success(
            "No hay clientes con venta reciente y deuda vieja en la "
            "fuente disponible."
        )
    else:
        # Filtro opcional por vendedor (vendedor de la venta reciente)
        _vend_cruce = sorted(
            cruce["vendedor"].dropna().astype(str).unique().tolist()
        )
        _sel_cruce = st.selectbox(
            "Filtrar por vendedor",
            options=["(Todos)"] + _vend_cruce,
            key="cruce_venta_deuda_vendedor",
        )
        vista_cruce = (
            cruce
            if _sel_cruce == "(Todos)"
            else cruce[cruce["vendedor"] == _sel_cruce]
        )

        vista_cruce = vista_cruce.copy()
        vista_cruce["fecha_ultima_venta"] = vista_cruce[
            "fecha_ultima_venta"
        ].apply(
            lambda d: "—" if pd.isna(d) else pd.Timestamp(d).strftime("%Y-%m-%d")
        )
        vista_cruce["fecha_venc_mas_vieja"] = vista_cruce[
            "fecha_venc_mas_vieja"
        ].apply(
            lambda d: "—" if pd.isna(d) else pd.Timestamp(d).strftime("%Y-%m-%d")
        )
        st.dataframe(
            vista_cruce.style.format(
                {
                    "monto_venta_reciente": "{:,.0f}",
                    "deuda_vieja": "{:,.0f}",
                }
            ),
            use_container_width=True,
            hide_index=True,
            column_config={
                "monto_venta_reciente": st.column_config.NumberColumn(
                    "Venta 30d (UYU)",
                    help="Suma de FAC al cliente en los últimos 30 días (neto).",
                ),
                "deuda_vieja": st.column_config.NumberColumn(
                    "Deuda >90d (UYU)",
                    help="Saldo bruto con IVA de comprobantes vencidos hace >90 días.",
                ),
                "fecha_ultima_venta": st.column_config.TextColumn(
                    "Última venta",
                ),
                "fecha_venc_mas_vieja": st.column_config.TextColumn(
                    "Venc. más viejo",
                ),
            },
        )
        st.caption(f"Total: {len(vista_cruce)} cliente(s) en el cruce.")
