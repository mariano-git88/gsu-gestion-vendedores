"""
views/inventario.py — Tab de Inventario.

Muestra para cada SKU (producto o combo) su stock actual y las semanas
de stock resultantes bajo 3 cortes de venta semanal promedio:

  - Último mes (últimos 30 días)
  - Últimos 3 meses (default para criticidad)
  - Mejor mes calendario de los últimos 12

Destaca en rojo los SKUs con <4 semanas según el corte de 3 meses
(umbral confirmado por Mariano 2026-04-18).

Fuentes:
  - df_productos (API modo API) con columna `stock` del endpoint
    `/api/conceptos/search`.
  - df_combos (API) con columna `stock` calculada derivando desde
    componentes (discovery + decisión 2026-04-18).
  - df_hist (session_state.df_hist12) con el histórico 12 meses
    cargado por el botón opt-in de la sidebar.

Si falta el histórico, muestra un aviso y no rompe.
Si la fuente es Manual (xlsx sin stock), degrada con aviso.
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
    """Firma uniforme con las otras vistas. Los args no se usan acá —
    inventario va contra session_state (df_productos, df_combos,
    df_hist12)."""
    del df_sem, df_mes, df_clientes, health_sem, health_mes

    st.subheader("Inventario")
    st.caption(
        "Stock actual por SKU (productos y combos consolidado) y **semanas "
        "de stock** bajo 3 cortes de venta semanal promedio. El stock de "
        "combos se calcula derivando de sus componentes — lo que permite "
        "armar efectivamente. Los SKUs con **menos de 4 semanas** de stock "
        "según el corte de 3 meses se marcan **críticos** (en rojo)."
    )

    # Degradación — Modo Manual no tiene stock
    if st.session_state.get("fuente_activa") != "api":
        st.info(
            "La tab de Inventario solo funciona en **modo API**. El Modo "
            "Manual Secundario no incluye el campo `Stock` del maestro "
            "de conceptos de Contabilium."
        )
        return

    df_productos = st.session_state.get("df_productos")
    df_combos = st.session_state.get("df_combos")
    df_hist12 = st.session_state.get("df_hist12")

    if df_productos is None or df_productos.empty:
        st.info(
            "No hay maestro de productos cargado. Tocá **Sincronizar** "
            "en la sidebar."
        )
        return

    if "stock" not in df_productos.columns:
        st.warning(
            "**El maestro de productos cacheado no tiene la columna `stock`.** "
            "Esto pasa si sincronizaste con una versión anterior del código "
            "y el caché sigue vigente.\n\n"
            "Solución: tocá **'Resync forzado (bypass caché)'** en la "
            "sidebar y volvé a **Sincronizar**."
        )
        return

    if df_hist12 is None or df_hist12.empty:
        st.info(
            "Para calcular semanas de stock hace falta el **histórico de "
            "12 meses**. Cargalo desde la sidebar (botón 'Cargar histórico')."
        )
        return

    # ===== Calcular tabla consolidada =====
    inv = metrics.inventario_semanas_stock(
        df_productos, df_combos, df_hist12,
    )

    if inv.empty:
        st.info("No hay datos para mostrar.")
        return

    # ===== KPIs arriba =====
    n_total = len(inv)
    n_criticos = int(inv["critico"].sum())
    stock_total_unidades = float(inv["stock"].sum())
    col_a, col_b, col_c = st.columns(3)
    col_a.metric(
        "SKUs totales",
        f"{n_total:,}",
        help="Productos + combos con stock registrado en Contabilium.",
    )
    col_b.metric(
        "SKUs críticos",
        f"{n_criticos:,}",
        delta=f"{n_criticos / n_total * 100:.1f}% del total" if n_total else None,
        delta_color="inverse" if n_criticos > 0 else "off",
        help=(
            "SKUs con menos de 4 semanas de stock según el corte de "
            "últimos 3 meses. Los SKUs sin venta en 3 meses NO se "
            "cuentan como críticos (no hay demanda)."
        ),
    )
    col_c.metric(
        "Unidades en stock (total)",
        f"{stock_total_unidades:,.0f}",
        help="Suma de unidades en stock de todos los SKUs.",
    )

    # ===== Filtros =====
    st.divider()
    col_f1, col_f2, col_f3 = st.columns(3)

    # Filtro por tipo (Producto / Combo / Todos)
    tipos_opts = ["(Todos)"] + sorted(inv["tipo"].dropna().unique().tolist())
    _tipo = col_f1.selectbox("Tipo", options=tipos_opts, key="inv_tipo")

    # Filtro por sub-rubro
    sr_opts = ["(Todos)"] + sorted(
        inv["sub_rubro"].dropna().astype(str).unique().tolist()
    )
    _sub_rubro = col_f2.selectbox("Sub-rubro", options=sr_opts, key="inv_sr")

    # Filtro de solo críticos
    _solo_criticos = col_f3.checkbox(
        "Solo críticos",
        value=False,
        key="inv_solo_criticos",
        help="Ocultar SKUs que no están en nivel crítico.",
    )

    vista = inv.copy()
    if _tipo != "(Todos)":
        vista = vista[vista["tipo"] == _tipo]
    if _sub_rubro != "(Todos)":
        vista = vista[vista["sub_rubro"].astype(str) == _sub_rubro]
    if _solo_criticos:
        vista = vista[vista["critico"]]

    # ===== Tabla principal =====
    st.divider()
    st.markdown("### SKUs y semanas de stock")
    st.caption(
        "Columnas `sem_...` muestran las semanas de stock estimadas bajo "
        "cada corte: **sem_ult_mes** (ventana corta, volátil), "
        "**sem_3m** (default para criticidad) y **sem_mejor_12m** "
        "(conservador — asume que la demanda pico se repite). Valores "
        "`<NA>` significan que no hubo ventas en ese corte."
    )

    if vista.empty:
        st.info("No hay SKUs que coincidan con los filtros.")
        return

    # Formato y orden de columnas para display
    display_cols = [
        "sku", "nombre", "tipo", "sub_rubro", "stock",
        "venta_sem_ultimo_mes", "venta_sem_ultimos_3m", "venta_sem_mejor_mes",
        "semanas_ultimo_mes", "semanas_ultimos_3m", "semanas_mejor_mes",
        "critico",
    ]

    # Styling: filas críticas con fondo rojo muy suave
    def _highlight_critico(row: pd.Series) -> list[str]:
        if row.get("critico", False):
            return ["background-color: #F8E4E0"] * len(row)
        return [""] * len(row)

    styled = (
        vista[display_cols]
        .style
        .apply(_highlight_critico, axis=1)
        .format(
            {
                "stock": "{:,.0f}",
                "venta_sem_ultimo_mes": "{:,.1f}",
                "venta_sem_ultimos_3m": "{:,.1f}",
                "venta_sem_mejor_mes": "{:,.1f}",
                "semanas_ultimo_mes": "{:.1f}",
                "semanas_ultimos_3m": "{:.1f}",
                "semanas_mejor_mes": "{:.1f}",
            }
        )
    )

    st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
        column_config={
            "sku": st.column_config.TextColumn("SKU", width="small"),
            "nombre": st.column_config.TextColumn("Nombre"),
            "tipo": st.column_config.TextColumn("Tipo", width="small"),
            "sub_rubro": st.column_config.TextColumn("Sub-rubro", width="small"),
            "stock": st.column_config.NumberColumn("Stock"),
            "venta_sem_ultimo_mes": st.column_config.NumberColumn(
                "Vta/sem ú.mes",
                help="Venta semanal promedio últimos 30 días.",
            ),
            "venta_sem_ultimos_3m": st.column_config.NumberColumn(
                "Vta/sem 3m",
                help="Venta semanal promedio últimos 90 días (default para criticidad).",
            ),
            "venta_sem_mejor_mes": st.column_config.NumberColumn(
                "Vta/sem mejor-12m",
                help="Venta semanal promedio del mejor mes calendario de los últimos 12.",
            ),
            "semanas_ultimo_mes": st.column_config.NumberColumn(
                "sem ú.mes",
                help="Stock / venta semanal últimos 30 días.",
            ),
            "semanas_ultimos_3m": st.column_config.NumberColumn(
                "sem 3m",
                help="Stock / venta semanal últimos 90 días. El corte usado para marcar crítico.",
            ),
            "semanas_mejor_mes": st.column_config.NumberColumn(
                "sem mejor-12m",
                help="Stock / venta semanal del mejor mes (escenario más demandante).",
            ),
            "critico": st.column_config.CheckboxColumn(
                "Crítico",
                help="True si sem 3m < 4 semanas.",
            ),
        },
    )

    st.caption(
        f"Mostrando {len(vista):,} de {n_total:,} SKU(s). "
        f"Críticos visibles: {int(vista['critico'].sum()):,}."
    )
