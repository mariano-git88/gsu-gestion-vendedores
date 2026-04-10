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
(Semana o Mes). Default: Mes (la penetración semanal tiende a ser baja
para todos y no aporta señal).

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
) -> None:
    """Args ver views/resumen.py. health_sem y health_mes no se usan acá."""
    del health_sem, health_mes  # firma uniforme con las otras vistas

    st.subheader("Análisis profundo")
    st.caption(
        "Tres exploraciones estratégicas para identificar oportunidades "
        "concretas de cross-sell y proteger el core del negocio."
    )

    # Selector de período común a las 3 secciones — default Mes
    timeframe = st.radio(
        "Período",
        options=["Mes", "Semana"],
        horizontal=True,
        key="analisis_tf",
    )
    df = df_mes if timeframe == "Mes" else df_sem

    if df.empty:
        st.info("No hay datos para el período seleccionado.")
        return

    # Bloque 1
    _seccion_penetracion(df, df_clientes)

    # Bloque 2
    st.divider()
    _seccion_heatmap(df, df_clientes)

    # Bloque 3
    st.divider()
    _seccion_pareto(df, df_clientes)


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

    # Color celda por celda con helper local (sin matplotlib)
    styled = pivot.style.applymap(_color_for_pct).format("{:.0f}%")

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
    styled = heat.style.applymap(
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
