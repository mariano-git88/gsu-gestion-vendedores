"""
views/analisis.py — Análisis profundo: penetración, heatmap, pareto.

Esta vista contiene tres bloques de exploración estratégica que NO son
del uso diario, sino de la conversación profunda en la reunión semanal
sobre dónde están las oportunidades:

  1. **Penetración por sub-rubro** — matriz vendedor × sub-rubro con %
     de cobertura. Identifica huecos de cross-sell a nivel agregado.

  2. **Heatmap cliente × sub-rubro** — para un vendedor específico,
     los top N clientes en filas y los sub-rubros en columnas. Ver el
     mix de productos que cada cliente compra.

  3. **Pareto de clientes** — distribución 80/20. Identifica el "core
     vital" del negocio (los clientes que hay que blindar) y el resto.

Las tres visualizaciones operan sobre el período seleccionado arriba
(Semana, Mes o Trimestre). Default: Mes (la penetración semanal tiende
a ser baja para todos y no aporta señal). "Trimestre" aparece sólo si
el modo API sincronizó el rango trimestral (ventana móvil de 3 meses).

Funciones puras de cálculo viven en `metrics.py`. Esta vista solo
orquesta + estiliza.
"""

import pandas as pd
import streamlit as st

import metrics


# =====================================================================
# Helpers de color (sin matplotlib)
# =====================================================================
# IMPORTANTE: NO usar `Styler.background_gradient()`. Esa función requiere
# matplotlib como dependencia oculta, y si matplotlib no está instalado
# en el entorno de deploy (Streamlit Cloud), TODA la vista rompe en
# runtime. Para evitarlo, calculamos los colores manualmente con dos
# helpers locales que solo usan stdlib + pandas.
# Ver _learning/errors.md, entrada 2026-04-10 sobre este incidente.

def _color_for_pct(value) -> str:
    """
    Devuelve el CSS de background para un valor 0-100 (porcentaje).
    Escala rojo claro → amarillo claro → verde claro, suave para que
    encaje con el theme Dieter Rams.

    NaN o no numéricos → sin color (string vacío).
    """
    if pd.isna(value):
        return ""
    try:
        pct = max(0.0, min(100.0, float(value))) / 100.0
    except (ValueError, TypeError):
        return ""

    if pct <= 0.5:
        # Rojo claro (0%) → Amarillo claro (50%)
        t = pct * 2
        r = int(248 + (250 - 248) * t)
        g = int(184 + (240 - 184) * t)
        b = int(184 + (176 - 184) * t)
    else:
        # Amarillo claro (50%) → Verde claro (100%)
        t = (pct - 0.5) * 2
        r = int(250 + (191 - 250) * t)
        g = int(240 + (229 - 240) * t)
        b = int(176 + (191 - 176) * t)

    return f"background-color: rgb({r}, {g}, {b}); color: #1A1A1A"


def _make_grey_scale(vmax: float):
    """
    Devuelve una función que mapea valores 0..vmax a un fondo de gris.
    Las celdas con 0 o NaN no llevan color. La escala va de gris muy
    claro (valor bajo) a gris oscuro (valor alto), con texto blanco
    cuando el fondo se vuelve muy oscuro.
    """
    def _color(value) -> str:
        if pd.isna(value):
            return ""
        try:
            v = float(value)
        except (ValueError, TypeError):
            return ""
        if v <= 0:
            return ""
        ratio = min(1.0, v / vmax) if vmax > 0 else 0.0
        # Gris: 245 (casi blanco) → 80 (gris oscuro)
        intensity = int(245 - (245 - 80) * ratio)
        text_color = "#1A1A1A" if intensity > 140 else "#FFFFFF"
        return (
            f"background-color: rgb({intensity}, {intensity}, {intensity}); "
            f"color: {text_color}"
        )

    return _color


def render(
    df_sem: pd.DataFrame,
    df_mes: pd.DataFrame,
    df_clientes: pd.DataFrame,
    health_sem: dict | None = None,
    health_mes: dict | None = None,
    df_clientes_act: pd.DataFrame | None = None,
) -> None:
    """Args ver views/resumen.py. health_sem y health_mes no se usan acá.

    `df_clientes_act` se usa en penetración y heatmap; los bloques de
    retención/frecuencia siguen usando `df_clientes` (cartera completa)."""
    del health_sem, health_mes  # firma uniforme con las otras vistas

    if df_clientes_act is None:
        df_clientes_act = df_clientes

    st.subheader("Análisis profundo")
    st.caption(
        "Tres exploraciones estratégicas para identificar oportunidades "
        "concretas de cross-sell y proteger el core del negocio."
    )

    # Selector de período común a las 3 secciones — default Mes.
    # "Trimestre" aparece sólo si el modo API sincronizó un trimestre
    # (session_state.df_tri). En Modo Manual el trimestre no existe.
    df_tri = st.session_state.get("df_tri")
    opciones = ["Mes", "Semana"]
    if df_tri is not None and not df_tri.empty:
        opciones.append("Trimestre")

    timeframe = st.radio(
        "Período",
        options=opciones,
        horizontal=True,
        key="analisis_tf",
    )
    if timeframe == "Mes":
        df = df_mes
    elif timeframe == "Semana":
        df = df_sem
    else:
        df = df_tri

    if df.empty:
        st.info("No hay datos para el período seleccionado.")
        return

    # Bloque 1 — penetración usa cartera depurada
    _seccion_penetracion(df, df_clientes_act)

    # Bloque 2 — heatmap usa cartera depurada
    st.divider()
    _seccion_heatmap(df, df_clientes_act)

    # Bloque 3
    st.divider()
    _seccion_pareto(df, df_clientes)

    # Bloque 4
    st.divider()
    _seccion_patrones_temporales(df, df_clientes)

    # Bloques 5 y 6: requieren histórico 12m. Si no está cargado,
    # cada sub-bloque muestra un aviso y no rompe.
    st.divider()
    _seccion_retencion(df_clientes)

    st.divider()
    _seccion_frecuencia(df_clientes)


# =====================================================================
# Bloque 1: Penetración por sub-rubro
# =====================================================================

def _seccion_penetracion(df: pd.DataFrame, df_clientes: pd.DataFrame) -> None:
    st.markdown("### Penetración por sub-rubro")
    st.caption(
        "Para cada vendedor, qué % de su cartera asignada recibió al "
        "menos una venta de cada sub-rubro en el período. Las celdas "
        "**rojas** son los huecos: clientes que tu vendedor tiene pero "
        "a los que todavía no les vendió esa categoría."
    )

    pivot = metrics.penetracion_por_sub_rubro_pivot(df, df_clientes)
    if pivot.empty:
        st.info("Sin datos de penetración para mostrar en este período.")
        return

    # Color celda por celda con helper local (sin matplotlib).
    # Usamos Styler.map (NO applymap, que fue removido en pandas 3.x).
    styled = pivot.style.map(_color_for_pct).format("{:.0f}%")

    st.dataframe(styled, use_container_width=True)


# =====================================================================
# Bloque 2: Heatmap cliente × sub-rubro
# =====================================================================

def _seccion_heatmap(df: pd.DataFrame, df_clientes: pd.DataFrame) -> None:
    st.markdown("### Heatmap cliente × sub-rubro")
    st.caption(
        "Para el vendedor seleccionado: los top N clientes (por monto "
        "del período) en filas, sub-rubros en columnas. Cada celda es "
        "el monto vendido. Las celdas en blanco son los **huecos por "
        "cliente** — productos que ese cliente no te compró pero podría."
    )

    vendedores_cartera = sorted(
        df_clientes["vendedor"].dropna().astype(str).unique().tolist()
    )
    if not vendedores_cartera:
        st.info("No hay vendedores con cartera asignada.")
        return

    col_v, col_n = st.columns([2, 1])
    with col_v:
        vendedor_sel = st.selectbox(
            "Vendedor",
            options=vendedores_cartera,
            key="heatmap_vendedor",
        )
    with col_n:
        top_n = st.number_input(
            "Top N clientes",
            min_value=5,
            max_value=100,
            value=30,
            step=5,
            key="heatmap_top_n",
        )

    heat = metrics.heatmap_cliente_sub_rubro(
        df, df_clientes, vendedor=vendedor_sel, top_n=int(top_n)
    )

    if heat.empty:
        st.info(
            f"El vendedor `{vendedor_sel}` no tiene clientes con compras "
            "en el período seleccionado."
        )
        return

    # Identificar columnas numéricas (sub-rubros) vs no-numéricas
    no_numeric_cols = {"razon_social", "documento"}
    sub_rubro_cols = [c for c in heat.columns if c not in no_numeric_cols]

    # Heatmap monocromático en grises (helper local, sin matplotlib).
    # vmax es el monto máximo en cualquier celda del subset, así la
    # escala usa todo el rango disponible para esta tabla.
    vmax = (
        float(heat[sub_rubro_cols].to_numpy().max())
        if sub_rubro_cols and not heat[sub_rubro_cols].empty
        else 0.0
    )
    grey_color = _make_grey_scale(vmax)
    # Usamos Styler.map (NO applymap, removido en pandas 3.x)
    styled = heat.style.map(
        grey_color, subset=sub_rubro_cols
    ).format("{:,.0f}", subset=sub_rubro_cols)

    st.dataframe(styled, use_container_width=True, hide_index=True)
    st.caption(
        f"Mostrando {len(heat)} clientes (top por monto del período) "
        f"sobre {len(sub_rubro_cols)} sub-rubros."
    )


# =====================================================================
# Bloque 3: Pareto de clientes
# =====================================================================

def _seccion_pareto(df: pd.DataFrame, df_clientes: pd.DataFrame) -> None:
    st.markdown("### Pareto de clientes")
    st.caption(
        "Distribución 80/20: los clientes ordenados por monto descendente "
        "y el porcentaje acumulado. Los marcados como **CORE 80%** son "
        "los que generan la mayoría de la venta — son los que hay que "
        "blindar antes que salir a buscar clientes nuevos."
    )

    vendedores_cartera = sorted(
        df_clientes["vendedor"].dropna().astype(str).unique().tolist()
    )
    options = ["(Todos los vendedores)"] + vendedores_cartera

    sel = st.selectbox(
        "Vista",
        options=options,
        key="pareto_vendedor",
    )
    vendedor_filter = None if sel == "(Todos los vendedores)" else sel

    par = metrics.pareto_clientes(df, df_clientes, vendedor=vendedor_filter)

    if par.empty:
        st.info("Sin datos de Pareto para mostrar.")
        return

    # Clasificar cada cliente según pct_acumulado
    par = par.copy()
    par["tipo"] = par["pct_acumulado"].apply(
        lambda x: "CORE 80%" if x <= 80.0 else "RESTO"
    )

    # Reordenar para que `tipo` quede al inicio
    cols_order = ["tipo"] + [c for c in par.columns if c != "tipo"]
    par = par[cols_order]

    # Insights numéricos arriba de la tabla
    n_total = len(par)
    n_core = int((par["tipo"] == "CORE 80%").sum())
    monto_total = float(par["monto"].sum())
    monto_core = float(par[par["tipo"] == "CORE 80%"]["monto"].sum())
    pct_clientes_core = (n_core / n_total * 100) if n_total else 0
    pct_monto_core = (monto_core / monto_total * 100) if monto_total else 0

    col_a, col_b, col_c = st.columns(3)
    col_a.metric("Total clientes", f"{n_total:,}")
    col_b.metric(
        "Clientes en CORE 80%",
        f"{n_core:,}",
        delta=f"{pct_clientes_core:.0f}% del total",
        delta_color="off",
    )
    col_c.metric(
        "Monto del CORE 80%",
        f"${monto_core:,.0f}",
        delta=f"{pct_monto_core:.0f}% del total",
        delta_color="off",
    )

    # Styling: fondo crema sutil para las filas CORE 80%
    def _highlight_core(row: pd.Series) -> list[str]:
        if row["tipo"] == "CORE 80%":
            return ["background-color: #FAF6E8"] * len(row)
        return [""] * len(row)

    fmt = {
        "monto": "{:,.0f}",
        "pct_individual": "{:.2f}%",
        "pct_acumulado": "{:.2f}%",
    }
    styled = par.style.apply(_highlight_core, axis=1).format(fmt)

    st.dataframe(styled, use_container_width=True, hide_index=True)


# =====================================================================
# Bloque 4: Patrones temporales (día de semana / quincena)
# =====================================================================

# =====================================================================
# Bloque 5: Tasa de retención (requiere histórico 12m)
# =====================================================================

def _seccion_retencion(df_clientes: pd.DataFrame) -> None:
    st.markdown("### Tasa de retención por vendedor")
    st.caption(
        "De los clientes que compraron **hace 6 meses calendario** (A), "
        "cuántos también compraron **en los últimos 3 meses** (B ∩ A). "
        "Retención = |B ∩ A| / |A|. Cuanto más alto, más vendedor "
        "conserva su base. Match estricto (vendedor operación = "
        "asignado). Requiere el histórico de 12 meses cargado."
    )
    df_hist12 = st.session_state.get("df_hist12")
    if df_hist12 is None or df_hist12.empty:
        st.info(
            "Para ver esta sección, cargá el **histórico de 12 meses** "
            "desde la sidebar."
        )
        return

    ret = metrics.tasa_retencion(df_hist12, df_clientes)
    if ret.empty:
        st.info(
            "No hay datos suficientes para calcular la retención "
            "(ningún cliente con FAC hace 6 meses)."
        )
        return

    st.dataframe(
        ret.style.map(_color_for_pct, subset=["retencion_pct"])
        .format({"retencion_pct": "{:.1f}%"}),
        use_container_width=True,
        hide_index=True,
    )


# =====================================================================
# Bloque 6: Frecuencia de compra por cliente (requiere histórico 12m)
# =====================================================================

def _seccion_frecuencia(df_clientes: pd.DataFrame) -> None:
    st.markdown("### Frecuencia de compra por cliente")
    st.caption(
        "Para cada cliente con al menos 2 FAC en el histórico de 12 "
        "meses, el promedio de días entre compras consecutivas. "
        "Valores bajos = clientes muy frecuentes. Útil para detectar "
        "clientes que están estirando su frecuencia habitual (señal "
        "temprana de cliente en fuga)."
    )
    df_hist12 = st.session_state.get("df_hist12")
    if df_hist12 is None or df_hist12.empty:
        st.info(
            "Para ver esta sección, cargá el **histórico de 12 meses** "
            "desde la sidebar."
        )
        return

    frec = metrics.frecuencia_compra_por_cliente(df_hist12, df_clientes)
    if frec.empty:
        st.info(
            "No hay clientes con al menos 2 compras en los últimos 12 meses."
        )
        return

    _vendedores = sorted(
        frec["vendedor"].dropna().astype(str).unique().tolist()
    )
    _opts = ["(Todos los vendedores)"] + _vendedores
    _sel = st.selectbox(
        "Filtrar por vendedor",
        options=_opts,
        key="frecuencia_vendedor_sel",
    )
    vista = (
        frec
        if _sel == "(Todos los vendedores)"
        else frec[frec["vendedor"] == _sel]
    )
    vista = vista.copy()
    vista["ultima_compra"] = vista["ultima_compra"].apply(
        lambda d: "—" if pd.isna(d) else pd.Timestamp(d).strftime("%Y-%m-%d")
    )
    st.dataframe(
        vista.style.format(
            {
                "n_compras": "{:,}",
                "dias_promedio_entre_compras": "{:.1f}",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(f"Total: {len(vista)} cliente(s) con historial analizable.")


def _seccion_patrones_temporales(
    df: pd.DataFrame, df_clientes: pd.DataFrame
) -> None:
    st.markdown("### Patrones temporales")
    st.caption(
        "Cuándo vende cada vendedor dentro del período. Útil para "
        "detectar concentración indebida (ej: empujones de cierre en la "
        "última quincena) y patrones de día de la semana."
    )

    vendedores_cartera = sorted(
        df_clientes["vendedor"].dropna().astype(str).unique().tolist()
    )
    options = ["(Todos los vendedores)"] + vendedores_cartera
    sel = st.selectbox(
        "Vendedor",
        options=options,
        key="patrones_vendedor",
    )
    vendedor_filter = None if sel == "(Todos los vendedores)" else sel

    col_dia, col_q = st.columns(2)

    # --- Día de la semana ---
    with col_dia:
        st.markdown("**Ventas por día de la semana**")
        por_dia = metrics.ventas_por_dia_semana(df, vendedor=vendedor_filter)
        if por_dia.empty or float(por_dia["monto"].sum()) == 0:
            st.info("Sin datos en el período.")
        else:
            chart_dia = por_dia.set_index("dia")[["monto"]]
            st.bar_chart(chart_dia, height=240)
            st.dataframe(
                por_dia[["dia", "monto", "tickets"]].style.format(
                    {"monto": "{:,.0f}", "tickets": "{:,}"}
                ),
                use_container_width=True,
                hide_index=True,
            )

    # --- Quincena ---
    with col_q:
        st.markdown("**Ventas por quincena del mes**")
        st.caption(
            "Día 1-15 vs 16-fin. Patrón muy segundo-tercio "
            "sugiere empujón artificial al cierre."
        )
        por_q = metrics.ventas_por_quincena(df, vendedor=vendedor_filter)
        if por_q.empty or float(por_q["monto"].sum()) == 0:
            st.info("Sin datos en el período.")
        else:
            chart_q = por_q.set_index("quincena")[["monto"]]
            st.bar_chart(chart_q, height=240)
            st.dataframe(
                por_q.style.format(
                    {"monto": "{:,.0f}", "tickets": "{:,}"}
                ),
                use_container_width=True,
                hide_index=True,
            )
