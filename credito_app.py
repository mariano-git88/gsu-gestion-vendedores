"""
credito_app.py — App de Streamlit para el scoring crediticio de la cartera.

Entry point independiente del dashboard principal (`app.py`), de Comisiones,
Listas, Facturador y Rendición. Se deploya en Streamlit Cloud como un app más
del mismo repo: misma codebase, URL distinta, password propia
(`credito_password`). Reusa `contabilium_client_id` / `contabilium_client_secret`.

Público: finanzas (Mariano), no la reunión comercial. Por eso vive aparte del
dashboard del Jefe de Ventas.

Toda la lógica vive en `credito.py` (puro) y `credito_api.py` (bajada de
datos). Este archivo es solo UI.

Decisiones de gráficos: las bandas A→E están ORDENADAS, así que no llevan
colores categóricos — barras de un solo tono con la letra en el eje. El
semáforo va como ícono + letra en las tablas, nunca color solo. (Una paleta
de 5 estados no pasa el piso de separación: el ámbar de "C" y el naranja de
"D" quedan a ΔE 13,6 y no se distinguen ni con visión normal.)
"""

from __future__ import annotations

import hmac
from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st

import api_loader
import cambios_credito
import credito as cr
import credito_api as ca
import gsheets
import theme
import tutorial_credito

st.set_page_config(
    page_title="Scoring Crediticio — GSU",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="expanded",
)

ACCENT = "#C8552F"
ACCENT_DARK = "#A8451F"
INK = "#1A1A1A"
SOFT = "#767676"

# El logo va en el menú lateral fijo, que es donde lo pone `st.logo()`. En el
# encabezado NO va: ahí quedan solo el título, los datos y los botones.
theme.apply_theme()

# Botones en naranja y métricas más chicas, como en el resto de las apps. Sin
# achicar la métrica, un importe de 8 cifras se corta en la mitad de las
# columnas.
st.markdown(
    """
    <style>
    [data-testid="stMain"] .stButton > button,
    [data-testid="stMain"] .stDownloadButton > button,
    [data-testid="stMain"] [data-testid="stFormSubmitButton"] > button {
        background-color: #C8552F !important;
        color: #FFFFFF !important;
        border-color: #C8552F !important;
        padding: 0.2rem 0.7rem !important;
        font-size: 0.72rem !important;
        letter-spacing: 0.03em;
    }
    [data-testid="stMain"] .stButton > button:hover,
    [data-testid="stMain"] .stDownloadButton > button:hover,
    [data-testid="stMain"] [data-testid="stFormSubmitButton"] > button:hover {
        background-color: #A8451F !important;
        border-color: #A8451F !important;
        color: #FFFFFF !important;
    }
    [data-testid="stMain"] [data-testid="stMetricValue"] {
        font-size: 1.5rem !important; line-height: 1.1 !important;
    }
    [data-testid="stMain"] [data-testid="stMetricLabel"] {
        font-size: 0.75rem !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def uyu(v, decimales: int = 0) -> str:
    """12192334 → '$ 12.192.334'. Separadores uruguayos, no ingleses."""
    try:
        s = f"{float(v):,.{decimales}f}"
    except (TypeError, ValueError):
        return "—"
    return "$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def num(v) -> str:
    """1019 → '1.019'."""
    try:
        return f"{int(v):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "—"


# ---------------------------------------------------------------------
# Tutorial y Novedades (modales abiertos desde el encabezado)
# ---------------------------------------------------------------------

@st.dialog("Tutorial — Scoring Crediticio", width="large")
def _tutorial_dialog():
    tutorial_credito.render()


@st.dialog("Novedades — qué cambió en la app", width="large")
def _cambios_dialog():
    cambios_credito.render()


# =====================================================================
# Auth
# =====================================================================

def _check_password() -> bool:
    """Login con `credito_password`. Key de sesión: `auth_credito`."""
    if st.session_state.get("auth_credito", False):
        return True

    left, center, right = st.columns([1, 2, 1])
    with center:
        st.markdown(
            "<h1 style='margin-bottom:0.25rem;'>Scoring Crediticio</h1>",
            unsafe_allow_html=True,
        )
        st.caption(
            "Quién califica a más plazo de financiación, por cuánto y a qué "
            "tasa. Uso interno de Suprabond."
        )
        with st.form("login_credito"):
            pwd = st.text_input("Contraseña", type="password", placeholder="••••••••")
            ok = st.form_submit_button("Ingresar", use_container_width=True)
        if ok:
            stored = st.secrets.get("credito_password")
            if stored is None:
                st.error(
                    "Falta configurar `credito_password` en los secrets. "
                    "Avisar a Mariano."
                )
                return False
            if hmac.compare_digest(str(stored), pwd):
                st.session_state.auth_credito = True
                st.rerun()
            else:
                st.error("Contraseña incorrecta.")
    return False


if not _check_password():
    st.stop()


# =====================================================================
# Carga de datos
# =====================================================================

@st.cache_resource(show_spinner=False)
def _session():
    return api_loader.obtener_token(
        st.secrets["contabilium_client_id"],
        st.secrets["contabilium_client_secret"],
    )


@st.cache_data(ttl=86_400, show_spinner=False)
def cargar(meses: int, _hoy: date):
    """Baja comprobantes + recibos + clientes. Cache de 24 h.

    Es un pull pesado: los recibos hay que pedirlos día por día porque el
    endpoint no pagina (ver `credito_api`). Con 12 meses son ~370 consultas
    de cabecera y ~6.000 de detalle: unos 10-15 minutos la primera vez.
    Después queda cacheado por 24 h.
    """
    desde = _hoy - timedelta(days=int(meses * 30.44))
    ses = _session()
    barra = st.progress(0.0, text="Conectando…")

    def prog(hecho, total, texto):
        barra.progress(min(hecho / max(total, 1), 1.0), text=texto)

    rep = ca.ReporteCarga()
    ses, df_comp, rep = ca.bajar_comprobantes(ses, desde, _hoy, rep, prog)
    ses, df_imp, df_formas, rep = ca.bajar_cobranzas(ses, desde, _hoy, rep, prog)
    ses, df_cli = ca.bajar_clientes(ses)
    barra.empty()
    return df_comp, df_imp, df_formas, df_cli, rep


# =====================================================================
# Sidebar — parámetros
# =====================================================================

st.sidebar.markdown("### Datos")
meses = st.sidebar.select_slider(
    "Ventana de historia", options=[12, 18, 24], value=12,
    help="Cuánta historia se baja de Contabilium. Más meses = pull más lento.",
)
st.sidebar.markdown("---")
st.sidebar.markdown("### Política de crédito")
cfg = cr.ConfigScore()

with st.sidebar.expander("Plazos por banda", expanded=False):
    for b in ["A", "B", "C", "D"]:
        cfg.plazo_por_banda[b] = st.number_input(
            f"Banda {b} — días", 0, 180, cfg.plazo_por_banda[b], step=15, key=f"pl{b}"
        )

with st.sidebar.expander("Tasa de interés", expanded=True):
    cfg.costo_fondos_anual = (
        st.number_input(
            "Costo de fondos anual (%)", 0.0, 100.0,
            cfg.costo_fondos_anual * 100, step=0.5,
            help="Lo que le cuesta a GSU financiar. El 20% es la tasa real a "
                 "la que GSU se financia, confirmada en agosto de 2026. Si "
                 "cambia, se cambia acá: de este número sale toda la tasa.",
        )
        / 100
    )
    cfg.spread_anual = (
        st.number_input("Spread objetivo (%)", 0.0, 100.0,
                        cfg.spread_anual * 100, step=0.5) / 100
    )

with st.sidebar.expander("Límites y vetos", expanded=False):
    cfg.tope_meses_compra = st.number_input(
        "Tope: meses de compra", 0.5, 12.0, cfg.tope_meses_compra, step=0.5
    )
    cfg.veto_monto_piso = st.number_input(
        "Veto: piso de deuda vencida ($)", 0.0, 1_000_000.0,
        cfg.veto_monto_piso, step=5_000.0,
        help="Debajo de este monto una deuda vieja no veta al cliente. "
             "Con el piso en $1 se vetaba al 44% de la cartera por colas "
             "de centavos.",
    )
    cfg.veto_dias_vencido = st.number_input(
        "Veto: días de vencido", 30.0, 365.0, cfg.veto_dias_vencido, step=15.0
    )

descontar_residuos = st.sidebar.checkbox(
    "Descontar colas de rendición", value=True,
    help="Las facturas viejas con un resto chico (≤25%) casi siempre son la "
         "NC del 10% de la rendición que nunca se imputó, no deuda real. "
         "Destildar para contarlas como mora.",
)

st.sidebar.markdown("---")
if st.sidebar.button("Cerrar sesión", use_container_width=True):
    st.session_state.pop("auth_credito", None)
    st.rerun()


# =====================================================================
# Pipeline
# =====================================================================

HOY = date.today()
hoy_ts = pd.Timestamp(HOY)

with st.spinner("Bajando datos de Contabilium… la primera vez tarda."):
    df_comp, df_imp, df_formas, df_cli, rep = cargar(meses, HOY)

ratio_residuo = cr.RESIDUO_RATIO_MAX if descontar_residuos else -1.0

hist = cr.armar_historial(df_comp, df_imp, hoy=hoy_ts)
rnc = cr.resumen_notas_credito(df_comp, hoy=hoy_ts)
feat = cr.features_por_cliente(
    hist, df_formas, hoy=hoy_ts, resumen_nc=rnc,
    residuo_ratio_max=ratio_residuo,
)
feat = feat.merge(
    df_cli[["id_cliente", "documento", "ciudad"]], on="id_cliente", how="left"
)
pol = cr.politica(cr.scorear(feat, cfg), cfg)
pares_nc = cr.pares_factura_nc(hist, df_comp)


# --- Foto del día -----------------------------------------------------
# El histórico NO se puede reconstruir hacia atrás (ver `metricas_cartera`),
# así que se guarda una foto por día. Best-effort: si el Sheet no está
# configurado o falla, la app sigue igual. Se marca "intentado hoy" ANTES de
# escribir, como en `app.py`, para no reintentar en cada rerun.
if st.session_state.get("credito_snap_dia") != HOY.isoformat():
    st.session_state.credito_snap_dia = HOY.isoformat()
    try:
        gsheets.append_credito_snapshot(
            dict(st.secrets.get("gsheets", {})),
            cr.metricas_cartera(pol, fecha=HOY, pares_nc=pares_nc),
            cr.COLUMNAS_SNAPSHOT,
        )
        st.session_state.credito_snap_error = None
    except Exception as e:  # noqa: BLE001
        # Se guarda el motivo y se muestra en Tendencia. Un `except: pass` acá
        # deja la serie sin escribirse durante meses sin que nadie se entere:
        # el gráfico se ve "vacío porque recién empieza", no "roto".
        st.session_state.credito_snap_error = f"{type(e).__name__}: {e}"

_PLATA = lambda v: uyu(v)  # noqa: E731 — el Styler pide un callable

FMT = {
    "ventas_netas_12m": _PLATA, "exposicion_neta": _PLATA,
    "limite_sugerido": _PLATA, "margen_disponible": _PLATA,
    "saldo_vencido": _PLATA, "capital_excedido": _PLATA,
    "exposicion_extra": _PLATA, "interes_anual": _PLATA,
    "score": "{:.0f}", "dso": "{:.0f}", "exceso_dso": "{:.0f}",
    "dpd_pond": "{:.0f}", "dpd_p90": "{:.0f}", "recargo_pct": "{:.2f}%",
    "tasa_anual": "{:.1%}", "pct_cheque": "{:.0%}",
    "dpd_vivo_max": "{:.0f}", "dias_sin_pago": "{:.0f}",
    # Fecha: el Styler no tolera NaT con un formato de fecha, va callable.
    "ultimo_pago": lambda d: "—" if pd.isna(d) else f"{d:%d/%m/%Y}",
}


def tabla(df: pd.DataFrame, cols: list[str], **kw):
    st.dataframe(
        df[cols].style.format({k: v for k, v in FMT.items() if k in cols}),
        use_container_width=True, hide_index=True, **kw,
    )


# =====================================================================
# Encabezado
# =====================================================================

enc_info, enc_btn = st.columns([3, 1.3])
with enc_info:
    st.markdown("##### Scoring Crediticio")
    st.caption(
        f"{num(len(pol))} clientes · "
        f"{num(int((pol['banda'] != 'S/D').sum()))} con score"
    )
    st.caption(f"Datos al: **{HOY:%d/%m/%Y}** · {meses} meses de historia")
    st.caption(f"Versión de la app: {cambios_credito.ultima_actualizacion()}")
with enc_btn:
    bt1, bt2 = st.columns(2)
    if bt1.button("📖 Tutorial", use_container_width=True):
        _tutorial_dialog()
    if bt2.button("🆕 Novedades", use_container_width=True):
        _cambios_dialog()
    if st.button("↻ Recargar datos", use_container_width=True):
        cargar.clear()
        st.rerun()

c1, c2, c3, c4 = st.columns(4)
exp_total = pol["exposicion_neta"].sum()
ventas_total = pol["ventas_netas_12m"].sum()
dso_cartera = exp_total / (ventas_total / 365) if ventas_total else 0
c1.metric("Exposición total", uyu(exp_total))
c2.metric("DSO de la cartera", f"{dso_cartera:.0f} días")
c3.metric(
    "Capital sobre el plazo pactado",
    uyu(pol["capital_excedido"].sum()),
    help="Plata prestada por encima de los días que se pactaron. Ya se está "
         "financiando a los clientes; hoy, gratis.",
)
c4.metric("Clientes con score", num(int((pol["banda"] != "S/D").sum())))

# Con `st.tabs` el contenido de una sección se derrama a la otra al recargar,
# así que se usa segmented_control + corte explícito del script. La `key` la
# necesitan las pruebas: sin ella el selector no se puede accionar desde
# `streamlit.testing` y no hay forma de probar una sección.
seccion = st.segmented_control(
    "Sección",
    ["Resumen", "Clientes", "Oportunidad", "Riesgo", "Tendencia",
     "Calidad de datos"],
    default="Resumen", label_visibility="collapsed", key="seccion_actual",
)


# ---------------------------------------------------------------- Resumen
if seccion == "Resumen":
    resumen = cr.resumen_cartera(pol)
    st.markdown("#### La cartera por banda")
    ca_, cb_ = st.columns([1, 1])

    with ca_:
        base = alt.Chart(resumen).encode(
            x=alt.X("banda:N", sort=["A", "B", "C", "D", "E", "S/D"],
                    title=None, axis=alt.Axis(labelAngle=0)),
        )
        st.altair_chart(
            base.mark_bar(size=38, color=ACCENT, cornerRadiusTopLeft=4,
                          cornerRadiusTopRight=4)
            .encode(
                y=alt.Y("clientes:Q", title="Clientes"),
                tooltip=["banda", "clientes", alt.Tooltip("%_clientes", format=".1f")],
            )
            .properties(height=240, title="Clientes"),
            use_container_width=True,
        )
    with cb_:
        st.altair_chart(
            alt.Chart(resumen)
            .mark_bar(size=38, color=INK, cornerRadiusTopLeft=4,
                      cornerRadiusTopRight=4)
            .encode(
                x=alt.X("banda:N", sort=["A", "B", "C", "D", "E", "S/D"],
                        title=None, axis=alt.Axis(labelAngle=0)),
                y=alt.Y("ventas_12m:Q", title="Ventas 12m"),
                tooltip=["banda", alt.Tooltip("ventas_12m", format=",.0f")],
            )
            .properties(height=240, title="Facturación"),
            use_container_width=True,
        )

    st.dataframe(
        resumen.style.format(
            {"ventas_12m": _PLATA, "exposicion": _PLATA,
             "saldo_vencido": _PLATA, "capital_excedido": _PLATA,
             "clientes": "{:.0f}", "%_clientes": "{:.1f}%",
             "%_ventas": "{:.1f}%", "dso_prom": "{:.0f}",
             "dpd_pond_prom": "{:.1f}"}
        ),
        use_container_width=True, hide_index=True,
    )

    st.markdown("#### Cuánto cuesta hoy la mora, medido")
    st.caption(
        "La prima de riesgo no está calibrada contra incobrables (casi no hay "
        "en la cartera). Lo que sí se puede medir es cuántos días de más se "
        "atrasa cada banda respecto de la mejor, y cuánto cuesta financiar "
        "esos días. Es un **piso** de la prima, no la prima final."
    )
    prima = cr.prima_por_mora_observada(pol, cfg.costo_fondos_anual)
    if not prima.empty:
        prima["prima_piso"] = (prima["prima_piso"] * 100).round(2)
        st.dataframe(
            prima.style.format(
                {"clientes": "{:.0f}", "dpd_pond_prom": "{:.1f}",
                 "monto_12m": _PLATA, "dias_mora_extra": "{:.1f}",
                 "prima_piso": "{:.2f}%"}
            ),
            use_container_width=True, hide_index=True,
        )


# ---------------------------------------------------------------- Clientes
elif seccion == "Clientes":
    f1, f2, f3 = st.columns([2, 1, 1])
    busca = f1.text_input("Buscar cliente", placeholder="Razón social o RUT")
    bandas = f2.multiselect(
        "Banda", ["A", "B", "C", "D", "E", "S/D"],
        default=["A", "B", "C", "D", "E", "S/D"],
    )
    minimo = f3.number_input("Ventas 12m mínimas", 0, 10_000_000, 0, step=50_000)

    v = pol[pol["banda"].isin(bandas) & (pol["ventas_netas_12m"] >= minimo)]
    if busca:
        m = busca.strip().lower()
        v = v[
            v["razon_social"].str.lower().str.contains(m, na=False)
            | v["documento"].astype(str).str.contains(m, na=False)
        ]

    st.caption(f"{len(v)} clientes")
    tabla(
        v.sort_values("ventas_netas_12m", ascending=False),
        ["semaforo", "banda", "razon_social", "score", "ventas_netas_12m",
         "dso", "exceso_dso", "dpd_pond", "plazo_actual", "plazo_sugerido",
         "limite_sugerido", "exposicion_neta", "margen_disponible",
         "recargo_pct", "motivo_veto"],
        height=420,
    )

    st.markdown("#### Ficha de un cliente")
    elegido = st.selectbox(
        "Cliente", v.sort_values("razon_social")["razon_social"].tolist(),
        index=None, placeholder="Elegir…",
    )
    if elegido:
        r = v[v["razon_social"] == elegido].iloc[0]
        # `delta` con texto: Streamlit no lo puede leer como número y por
        # defecto lo pinta VERDE con flecha para arriba. En esta ficha eso
        # daba vuelta el sentido: "+140 días vs pactado" y "disponible
        # -$80.863" son las dos cosas malas y se veían en verde. Los deltas
        # informativos van en "off" (gris, sin flecha) y el exceso de DSO en
        # "inverse", que es lo que es: más días, peor.
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Score", f"{r['score']:.0f}", f"Banda {r['banda']}",
                  delta_color="off")
        k2.metric("DSO", f"{r['dso']:.0f} d",
                  f"{r['exceso_dso']:+.0f} días vs lo pactado",
                  delta_color="inverse")
        k3.metric("Plazo sugerido", f"{r['plazo_sugerido']} d",
                  f"hoy {r['plazo_actual']} d", delta_color="off")
        k4.metric("Límite sugerido", uyu(r["limite_sugerido"]),
                  f"disponible {uyu(r['margen_disponible'])}",
                  delta_color="inverse" if r["margen_disponible"] < 0 else "off")

        st.markdown("**De dónde sale el puntaje**")
        pilares = pd.DataFrame(
            {
                "pilar": ["Exceso de DSO", "Atraso promedio", "Peor caso (p90)",
                          "Puntualidad", "Antigüedad", "Volumen", "Situación hoy"],
                "puntos": [r["p_dso"], r["p_dpd"], r["p_p90"], r["p_puntual"],
                           r["p_antiguedad"], r["p_volumen"], r["p_situacion"]],
                "maximo": [cfg.peso_dso, cfg.peso_dpd, cfg.peso_p90,
                           cfg.peso_puntualidad, cfg.peso_antiguedad,
                           cfg.peso_volumen, cfg.peso_situacion],
            }
        )
        st.altair_chart(
            alt.Chart(pilares).mark_bar(color=ACCENT, height=18,
                                        cornerRadiusTopRight=4,
                                        cornerRadiusBottomRight=4)
            .encode(
                y=alt.Y("pilar:N", sort=None, title=None),
                x=alt.X("puntos:Q", title="Puntos", scale=alt.Scale(domain=[0, 25])),
                tooltip=["pilar", alt.Tooltip("puntos", format=".1f"), "maximo"],
            ).properties(height=200),
            use_container_width=True,
        )

        h = hist[hist["id_cliente"] == r["id_cliente"]].sort_values(
            "emision", ascending=False
        )
        st.markdown(f"**Sus facturas** ({len(h)})")
        st.dataframe(
            h[["numero", "emision", "cond_venta", "total", "pagado", "saldo",
               "estado", "dpd", "dpd_corriente"]]
            .style.format(
                {"total": _PLATA, "pagado": _PLATA, "saldo": _PLATA,
                 "dpd": "{:.0f}", "dpd_corriente": "{:.0f}"}
            ),
            use_container_width=True, hide_index=True, height=300,
        )


# ---------------------------------------------------------------- Oportunidad
elif seccion == "Oportunidad":
    sube = pol[
        (pol["plazo_sugerido"] > pol["plazo_actual"]) & (pol["banda"] != "S/D")
    ].copy()
    sube["exposicion_extra"] = (
        sube["limite_sugerido"] - sube["exposicion_neta"]
    ).clip(lower=0)
    # Interés anual que dejaría esa financiación si el cliente usa todo el
    # margen y lo mantiene rotando.
    sube["interes_anual"] = sube["exposicion_extra"] * sube["tasa_anual"]

    o1, o2, o3 = st.columns(3)
    o1.metric("Clientes que califican a más plazo", num(len(sube)))
    o2.metric("Capital adicional a inmovilizar",
              uyu(sube["exposicion_extra"].sum()))
    o3.metric("Interés anual si lo usan todo",
              uyu(sube["interes_anual"].sum()))

    st.caption(
        "El interés de arriba es el techo teórico: supone que todos toman "
        "todo el margen y lo mantienen el año entero. Sirve para dimensionar "
        "el orden de magnitud, no para presupuestar."
    )

    st.markdown("#### DSO contra volumen")
    st.caption(
        "Cada punto es un cliente. Cuanto más a la derecha, más días de venta "
        "tiene prestados. La línea marca el plazo de 30 días."
    )
    sc = pol[(pol["banda"] != "S/D") & pol["dso"].notna()].copy()
    sc = sc[sc["dso"].between(-50, 400)]
    pts = (
        alt.Chart(sc)
        .mark_circle(size=70, color=ACCENT, opacity=0.55)
        .encode(
            x=alt.X("dso:Q", title="DSO (días)"),
            y=alt.Y("ventas_netas_12m:Q", title="Ventas 12m",
                    scale=alt.Scale(type="symlog")),
            tooltip=["razon_social", "banda", alt.Tooltip("score", format=".0f"),
                     alt.Tooltip("dso", format=".0f"),
                     alt.Tooltip("ventas_netas_12m", format=",.0f")],
        )
    )
    linea = (
        alt.Chart(pd.DataFrame({"x": [30]}))
        .mark_rule(color=SOFT, strokeDash=[4, 4])
        .encode(x="x:Q")
    )
    st.altair_chart((pts + linea).properties(height=340),
                    use_container_width=True)

    st.markdown("#### A quiénes ofrecerles más plazo")
    tabla(
        sube.sort_values("interes_anual", ascending=False),
        ["semaforo", "banda", "razon_social", "score", "ventas_netas_12m",
         "plazo_actual", "plazo_sugerido", "recargo_pct", "limite_sugerido",
         "exposicion_extra", "interes_anual", "pct_cheque"],
        height=420,
    )


# ---------------------------------------------------------------- Riesgo
elif seccion == "Riesgo":
    st.markdown("#### Clientes con más plazo del que su score justifica")
    baja = pol[
        (pol["plazo_sugerido"] < pol["plazo_actual"]) & (pol["banda"] != "S/D")
    ]
    tabla(
        baja.sort_values("exposicion_neta", ascending=False),
        ["semaforo", "banda", "razon_social", "score", "ventas_netas_12m",
         "dso", "exceso_dso", "plazo_actual", "plazo_sugerido",
         "exposicion_neta", "saldo_vencido", "motivo_veto"],
        height=320,
    )

    st.markdown("#### Quién debe plata vencida, y hace cuánto que no paga")
    st.caption(
        "Solo clientes con facturas ya vencidas. **Días de atraso** es la "
        "factura vencida más vieja que sigue abierta. **Días sin pagar** son "
        "los días desde el último recibo con plata de verdad: una nota de "
        "crédito compensando una factura no cuenta como pago. Cuando los dos "
        "números son altos a la vez, es la señal de que el cliente dejó de "
        "pagar; si el atraso es alto pero sigue pagando, es una cola vieja "
        "que quedó sin imputar."
    )
    deuda = pol[pol["saldo_vencido"] > 0].copy()
    tabla(
        deuda.sort_values("saldo_vencido", ascending=False),
        ["semaforo", "banda", "razon_social", "saldo_vencido", "dpd_vivo_max",
         "dias_sin_pago", "ultimo_pago", "exposicion_neta"],
        height=420,
    )
    d1, d2, d3 = st.columns(3)
    d1.metric("Clientes con deuda vencida", num(len(deuda)))
    d2.metric("Total vencido", uyu(deuda["saldo_vencido"].sum()))
    d3.metric(
        "Vencido de los que hace +90 días que no pagan",
        uyu(deuda.loc[deuda["dias_sin_pago"] > 90, "saldo_vencido"].sum()),
        help="Deben plata vencida y además hace más de 90 días que no entra "
             "un peso de ellos. Es la plata que hay que ir a buscar primero.",
    )

    st.markdown("#### Los que concentran la mora")
    st.caption(
        "Ordenado por capital inmovilizado por encima del plazo pactado: "
        "es la plata que ya se está financiando sin cobrar nada."
    )
    tabla(
        pol.nlargest(30, "capital_excedido"),
        ["semaforo", "banda", "razon_social", "ventas_netas_12m", "dso",
         "exceso_dso", "capital_excedido", "exposicion_neta", "saldo_vencido",
         "motivo_veto"],
        height=420,
    )


# ------------------------------------------------------------ Tendencia
elif seccion == "Tendencia":
    st.markdown("#### Cómo viene evolucionando la cartera")
    st.caption(
        "**Esta serie se construye hacia adelante, no hacia atrás.** "
        "Contabilium devuelve el saldo de HOY, no el que había en una fecha "
        "pasada: si se filtra por fecha, las facturas que desde entonces se "
        "cobraron aparecen en cero, como si nunca hubieran estado impagas "
        "(el DSO \"de hace tres meses\" da 18 días contra los 67 reales de "
        "hoy — es un espejismo, no una mejora). Por eso la app guarda una "
        "foto por día, y el gráfico se va llenando solo."
    )
    try:
        serie = gsheets.read_credito_snapshots(
            dict(st.secrets.get("gsheets", {})), cr.COLUMNAS_SNAPSHOT
        )
    except Exception as e:  # noqa: BLE001
        serie = pd.DataFrame()
        st.warning(
            "No se pudo leer el histórico. Falta configurar la sección "
            f"`[gsheets]` en los secrets de este app. Detalle: {e}"
        )

    _snap_err = st.session_state.get("credito_snap_error")
    if _snap_err:
        st.error(
            f"**La foto de hoy no se pudo guardar**, así que la serie no "
            f"avanza. Motivo: `{_snap_err}`"
        )
    elif not serie.empty and (
        serie["fecha"].max().date() < HOY
    ):
        st.warning(
            f"La última foto es del {serie['fecha'].max():%d/%m/%Y} y hoy es "
            f"el {HOY:%d/%m/%Y}. Si esto se repite, algo está fallando al "
            f"escribir."
        )

    if serie.empty:
        st.info(
            "Todavía no hay fotos guardadas. La primera se guarda sola hoy, "
            "al abrir la app. Volvé mañana y va a haber dos."
        )
    else:
        st.caption(f"{num(len(serie))} fotos · desde el "
                   f"{serie['fecha'].min():%d/%m/%Y}")
        METRICAS = {
            "DSO (días)": "dso",
            "Exposición": "exposicion",
            "Saldo vencido": "saldo_vencido",
            "Capital sobre el plazo pactado": "capital_excedido",
            "Vencido de los que no pagan hace +90 días": "vencido_sin_pagar_90",
        }
        elegida = st.selectbox("Qué mirar", list(METRICAS), index=0)
        col = METRICAS[elegida]
        st.altair_chart(
            alt.Chart(serie).mark_line(point=True, color=ACCENT).encode(
                x=alt.X("fecha:T", title=None),
                y=alt.Y(f"{col}:Q", title=elegida,
                        scale=alt.Scale(zero=False)),
                tooltip=["fecha:T", alt.Tooltip(f"{col}:Q", format=",.0f")],
            ).properties(height=320),
            use_container_width=True,
        )
        if len(serie) >= 2:
            pri, ult = serie.iloc[0], serie.iloc[-1]
            d1, d2, d3 = st.columns(3)
            d1.metric("Hoy", f"{ult[col]:,.0f}".replace(",", "."))
            d2.metric(f"El {pri['fecha']:%d/%m}",
                      f"{pri[col]:,.0f}".replace(",", "."))
            d3.metric("Diferencia", f"{ult[col] - pri[col]:+,.0f}"
                      .replace(",", "."))
        st.dataframe(
            serie.sort_values("fecha", ascending=False),
            use_container_width=True, hide_index=True, height=260,
        )


# ---------------------------------------------------------------- Datos
elif seccion == "Calidad de datos":
    st.markdown("#### Facturas que ya están canceladas por una nota de crédito")
    st.caption(
        "Facturas abiertas que tienen **una nota de crédito abierta del mismo "
        "importe exacto, del mismo cliente**. El patrón es siempre el mismo: "
        "se anuló la factura con una NC y nadie imputó la NC contra ella, así "
        "que quedaron las dos colgadas. La factura figura impaga y vencida, "
        "pero el cliente no debe esa plata."
    )
    if pares_nc.empty:
        st.success("No hay facturas en esta situación.")
    else:
        venc_nc = pares_nc[pares_nc["dpd_corriente"] > 0]
        alta = pares_nc[pares_nc["confianza"] == "alta"]
        q1, q2, q3 = st.columns(3)
        q1.metric("Facturas a revisar", num(len(pares_nc)),
                  help=f"{pares_nc['id_cliente'].nunique()} clientes")
        q2.metric("Contado hoy como vencido", uyu(venc_nc["saldo"].sum()),
                  help="Sobre el total de deuda vencida de la cartera.")
        q3.metric("De confianza alta", uyu(alta["saldo"].sum()),
                  help="La NC se emitió dentro de la semana de la factura: "
                       "es la anulación de esa factura, no una devolución "
                       "posterior.")
        st.caption(
            "**La app no descuenta nada de esto.** Emparejar por importe es "
            "una heurística: dos facturas del mismo monto pueden emparejar "
            "con la NC equivocada, y una NC de meses después puede ser una "
            "devolución real que se aplicó a otra cosa. Por eso las de "
            "`confianza = revisar` hay que mirarlas una por una. Lo que "
            "corresponde es **imputar la NC contra la factura en "
            "Contabilium**; ahí desaparecen de esta lista solas."
        )
        solo_alta = st.checkbox("Ver solo las de confianza alta", value=False)
        ver = alta if solo_alta else pares_nc
        st.dataframe(
            ver.style.format({"saldo": _PLATA, "dpd_corriente": "{:.0f}",
                              "dias_entre": "{:.0f}",
                              "emision_fac": lambda d: f"{d:%d/%m/%Y}",
                              "emision_nc": lambda d: f"{d:%d/%m/%Y}"}),
            use_container_width=True, hide_index=True, height=380,
            column_order=["razon_social", "numero_fac", "emision_fac", "saldo",
                          "dpd_corriente", "numero_nc", "emision_nc",
                          "dias_entre", "confianza"],
        )
        st.download_button(
            "Bajar la lista para administración",
            ver.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"facturas_con_nc_{HOY:%Y-%m-%d}.csv",
            mime="text/csv",
        )

    st.divider()
    st.markdown("#### Qué se pudo bajar")
    if rep.completo:
        st.success(rep.resumen())
    else:
        st.warning(rep.resumen())
    if rep.dias_truncados:
        st.caption(
            f"Días con más de 50 recibos (el endpoint no pagina y se pierden "
            f"los que sobran): {', '.join(rep.dias_truncados[:20])}"
        )

    st.markdown("#### Facturas por estado")
    est = (
        hist.groupby("estado")
        .agg(facturas=("id", "count"), monto=("total", "sum"),
             saldo=("saldo", "sum"))
        .reset_index()
    )
    st.dataframe(
        est.style.format({"monto": _PLATA, "saldo": _PLATA}),
        use_container_width=True, hide_index=True,
    )
    st.caption(
        "**sin_recibo** = la factura está saldada pero no encontramos el "
        "recibo. Se excluye del cálculo de mora en vez de contarse como "
        "impaga: se cobró, no sabemos cuándo."
    )

    st.markdown("#### Colas de rendición")
    st.metric("Saldo tratado como cola, no como deuda",
              uyu(pol["saldo_residual"].sum()))
    st.caption(
        "Facturas vencidas hace más de 60 días a las que les quedó un resto "
        "menor al 25% del total. Casi siempre es la NC del 10% de la "
        "rendición que nunca se imputó. Se puede apagar en la barra lateral."
    )

    sin_map = sorted(set(df_comp["cond_venta"]) - set(cr.PLAZO_POR_CONDICION))
    if sin_map:
        st.error(
            "Condiciones de venta sin plazo definido (se asumen 30 días): "
            + ", ".join(sin_map)
            + ". Agregarlas a `PLAZO_POR_CONDICION` en `credito.py`."
        )
    else:
        st.success("Todas las condiciones de venta tienen plazo definido.")
