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
    health_sem: dict | None = None,
    health_mes: dict | None = None,
) -> None:
    """
    Renderiza la vista de resumen.

    Args:
        df_sem: facturación semanal (post `transforms.prepare_facturacion`).
        df_mes: facturación mensual (post `transforms.prepare_facturacion`).
        df_clientes: maestro de clientes (post `data_loader.load_clientes`).
        health_sem: dict de salud devuelto por prepare_facturacion para
            la planilla semanal. Si se pasa, se muestra debajo del total
            el monto de NCF descuentos no descontadas (transparencia para
            cuadrar contra Excel).
        health_mes: idem para la planilla mensual.
    """
    health_sem = health_sem or {}
    health_mes = health_mes or {}

    st.subheader("Resumen del período")

    # ----- Big numbers: total + ticket promedio semana vs mes -----
    col_sem, col_mes = st.columns(2)
    with col_sem:
        total_sem = float(df_sem["monto"].sum()) if not df_sem.empty else 0.0
        tickets_sem = _count_tickets(df_sem)
        ticket_prom_sem = (total_sem / tickets_sem) if tickets_sem > 0 else 0.0
        st.metric("Total semana (UYU)", f"{total_sem:,.0f}")
        st.caption(
            f"{tickets_sem:,} ticket(s) · promedio "
            f"${ticket_prom_sem:,.0f}"
        )
        _render_caption_ncf_descartadas(health_sem)
    with col_mes:
        total_mes = float(df_mes["monto"].sum()) if not df_mes.empty else 0.0
        tickets_mes = _count_tickets(df_mes)
        ticket_prom_mes = (total_mes / tickets_mes) if tickets_mes > 0 else 0.0

        # Comparativa temporal si los DFs de mes anterior / YoY están
        # disponibles (solo en modo API).
        df_prev = st.session_state.get("df_prev")
        df_yoy = st.session_state.get("df_yoy")
        comp = metrics.comparativa_temporal(df_mes, df_prev, df_yoy)

        _delta_mom = _format_delta(comp["delta_mom_pct"])
        st.metric(
            "Total mes (UYU)",
            f"{total_mes:,.0f}",
            delta=_delta_mom,
            help="Variación vs mes anterior (mismo día de corte)",
        )
        st.caption(
            f"{tickets_mes:,} ticket(s) · promedio "
            f"${ticket_prom_mes:,.0f}"
        )

        # Segunda línea con el delta YoY — como st.metric solo soporta
        # un delta, mostramos el YoY como caption explícita debajo.
        if comp["delta_yoy_pct"] is not None:
            _yoy_signo = "▲" if comp["delta_yoy_pct"] >= 0 else "▼"
            st.caption(
                f"{_yoy_signo} {abs(comp['delta_yoy_pct']):.1f}% vs "
                f"mismo mes año pasado "
                f"(${comp['monto_yoy']:,.0f})"
            )
        elif df_yoy is None:
            st.caption("Sin comparación YoY disponible (sync manual).")

        _render_caption_ncf_descartadas(health_mes)

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
            "tickets": "tickets_semana",
            "ticket_promedio": "ticket_prom_semana",
        }
    )
    ventas_mes = metrics.ventas_por_vendedor(df_mes).rename(
        columns={
            "monto_total": "monto_mes",
            "unidades_totales": "unidades_mes",
            "tickets": "tickets_mes",
            "ticket_promedio": "ticket_prom_mes",
        }
    )

    # Outer join para conservar vendedores que solo aparecen en uno de los dos
    combined = ventas_sem.merge(ventas_mes, on="vendedor", how="outer").fillna(0)
    for col in [
        "monto_semana", "unidades_semana", "tickets_semana",
        "monto_mes", "unidades_mes", "tickets_mes",
    ]:
        combined[col] = combined[col].astype(int)
    combined = combined.sort_values("monto_mes", ascending=False).reset_index(drop=True)

    # Orden visual: intercalar las columnas del mes y la semana agrupadas
    col_order = [
        "vendedor",
        "monto_semana", "unidades_semana", "tickets_semana", "ticket_prom_semana",
        "monto_mes", "unidades_mes", "tickets_mes", "ticket_prom_mes",
    ]
    combined = combined[col_order]

    st.dataframe(
        combined.style.format(
            {
                "monto_semana": "{:,}",
                "unidades_semana": "{:,}",
                "tickets_semana": "{:,}",
                "ticket_prom_semana": "{:,.0f}",
                "monto_mes": "{:,}",
                "unidades_mes": "{:,}",
                "tickets_mes": "{:,}",
                "ticket_prom_mes": "{:,.0f}",
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
            column_config={
                "concentracion_80": st.column_config.NumberColumn(
                    "Conc. 80%",
                    help="N clientes que concentran el 80% de la venta del vendedor",
                ),
                "mix_top3": st.column_config.TextColumn(
                    "Mix top-3",
                    help="Los 3 sub-rubros con mayor % de venta",
                ),
            },
        )


def _count_tickets(df: pd.DataFrame) -> int:
    """Count de comprobantes distintos en el DF (vía id_comprobante).
    Robusto a inputs que no traigan la columna."""
    if df.empty or "id_comprobante" not in df.columns:
        return 0
    return int(df["id_comprobante"].nunique())


def _format_delta(pct: float | None) -> str | None:
    """Formatea un delta % como '+12.3%' / '-5.4%'. Devuelve None si
    `pct` es None (st.metric lo trata como 'sin delta')."""
    if pct is None:
        return None
    signo = "+" if pct >= 0 else ""
    return f"{signo}{pct:.1f}%"


def _render_caption_ncf_descartadas(health: dict) -> None:
    """
    Pinta una caption debajo de un metric con el monto total de descuentos
    comerciales aplicados en el período (NCF tipo descuento, sin SKU).

    Importante: estos descuentos SÍ se aplicaron al cliente en la
    facturación, pero NO están restados del total mostrado en el dashboard
    (regla de negocio: NCF sin sku no se contabilizan). Mostrar el monto
    explícito permite al usuario cuadrar contra el cálculo manual de
    Excel, donde sí se restan.

    Se muestra el valor absoluto (los descuentos vienen como números
    negativos en la planilla; el label habla de monto otorgado).

    Si no hay NCF descartadas, no se renderiza nada.
    """
    n = health.get("ncf_descartadas_descuento", 0)
    if n <= 0:
        return
    monto = abs(float(health.get("monto_ncf_descartado", 0.0)))
    st.caption(
        f"Los siguientes descuentos comerciales no fueron descontados "
        f"del monto indicado arriba: ${monto:,.2f} ({n} NCF sin SKU)"
    )
