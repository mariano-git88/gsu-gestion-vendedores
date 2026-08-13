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
import credito as cr
import credito_api as ca
import theme

st.set_page_config(
    page_title="Scoring Crediticio — GSU",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="expanded",
)
theme.apply_theme()

ACCENT = "#C8552F"
INK = "#1A1A1A"
SOFT = "#767676"


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
if st.sidebar.button("Recargar datos", use_container_width=True):
    cargar.clear()
    st.rerun()

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
            help="Lo que le cuesta a GSU financiar, o lo que rendiría esa "
                 "plata en otro lado. Es el parámetro más importante y hay "
                 "que confirmarlo: el default de 14% es un supuesto.",
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

FMT = {
    "ventas_netas_12m": "{:,.0f}", "exposicion_neta": "{:,.0f}",
    "limite_sugerido": "{:,.0f}", "margen_disponible": "{:,.0f}",
    "saldo_vencido": "{:,.0f}", "capital_excedido": "{:,.0f}",
    "score": "{:.0f}", "dso": "{:.0f}", "exceso_dso": "{:.0f}",
    "dpd_pond": "{:.0f}", "dpd_p90": "{:.0f}", "recargo_pct": "{:.2f}%",
    "tasa_anual": "{:.1%}", "pct_cheque": "{:.0%}",
}


def tabla(df: pd.DataFrame, cols: list[str], **kw):
    st.dataframe(
        df[cols].style.format({k: v for k, v in FMT.items() if k in cols}),
        use_container_width=True, hide_index=True, **kw,
    )


# =====================================================================
# Cabecera
# =====================================================================

st.markdown("## Scoring Crediticio")
st.caption(
    f"Cartera de Suprabond UY · {meses} meses de historia · "
    f"corte {HOY:%d/%m/%Y}"
)

c1, c2, c3, c4 = st.columns(4)
exp_total = pol["exposicion_neta"].sum()
ventas_total = pol["ventas_netas_12m"].sum()
dso_cartera = exp_total / (ventas_total / 365) if ventas_total else 0
c1.metric("Exposición total", f"$ {exp_total:,.0f}")
c2.metric("DSO de la cartera", f"{dso_cartera:.0f} días")
c3.metric(
    "Capital sobre el plazo pactado",
    f"$ {pol['capital_excedido'].sum():,.0f}",
    help="Plata prestada por encima de los días que se pactaron. Ya se está "
         "financiando a los clientes; hoy, gratis.",
)
c4.metric("Clientes con score", f"{int((pol['banda'] != 'S/D').sum())}")

t_res, t_cli, t_opo, t_rie, t_dat, t_doc = st.tabs(
    ["Resumen", "Clientes", "Oportunidad", "Riesgo", "Calidad de datos",
     "Cómo se calcula"]
)


# ---------------------------------------------------------------- Resumen
with t_res:
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

    tabla(
        resumen,
        ["banda", "clientes", "%_clientes", "ventas_12m", "%_ventas",
         "exposicion", "saldo_vencido", "capital_excedido", "dso_prom"],
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
                 "monto_12m": "{:,.0f}", "dias_mora_extra": "{:.1f}",
                 "prima_piso": "{:.2f}%"}
            ),
            use_container_width=True, hide_index=True,
        )


# ---------------------------------------------------------------- Clientes
with t_cli:
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
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Score", f"{r['score']:.0f}", f"Banda {r['banda']}")
        k2.metric("DSO", f"{r['dso']:.0f} d",
                  f"{r['exceso_dso']:+.0f} vs pactado")
        k3.metric("Plazo sugerido", f"{r['plazo_sugerido']} d",
                  f"hoy {r['plazo_actual']} d")
        k4.metric("Límite sugerido", f"$ {r['limite_sugerido']:,.0f}",
                  f"disponible $ {r['margen_disponible']:,.0f}")

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
                {"total": "{:,.0f}", "pagado": "{:,.0f}", "saldo": "{:,.0f}",
                 "dpd": "{:.0f}", "dpd_corriente": "{:.0f}"}
            ),
            use_container_width=True, hide_index=True, height=300,
        )


# ---------------------------------------------------------------- Oportunidad
with t_opo:
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
    o1.metric("Clientes que califican a más plazo", f"{len(sube)}")
    o2.metric("Capital adicional a inmovilizar",
              f"$ {sube['exposicion_extra'].sum():,.0f}")
    o3.metric("Interés anual si lo usan todo",
              f"$ {sube['interes_anual'].sum():,.0f}")

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
with t_rie:
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


# ---------------------------------------------------------------- Datos
with t_dat:
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
        est.style.format({"monto": "{:,.0f}", "saldo": "{:,.0f}"}),
        use_container_width=True, hide_index=True,
    )
    st.caption(
        "**sin_recibo** = la factura está saldada pero no encontramos el "
        "recibo. Se excluye del cálculo de mora en vez de contarse como "
        "impaga: se cobró, no sabemos cuándo."
    )

    st.markdown("#### Colas de rendición")
    st.metric("Saldo tratado como cola, no como deuda",
              f"$ {pol['saldo_residual'].sum():,.0f}")
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


# ---------------------------------------------------------------- Doc
with t_doc:
    st.markdown(
        """
#### Qué mide este score

Ordena a los clientes por **comportamiento de pago observado**. No es una
probabilidad de default: para eso haría falta una serie de incobrables y en la
cartera de GSU prácticamente no hay. El score dice quién paga mejor y quién
peor, con datos duros. No dice "este cliente tiene 3% de chance de no pagar".

Tampoco ve nada de afuera. Sin Clearing de Informes ni Central de Riesgos del
BCU, un cliente puede estar impecable con nosotros y en default con medio
mercado.

#### Los siete pilares

| Pilar | Puntos | Qué mira |
|---|---|---|
| Exceso de DSO | 25 | Días de venta que tiene prestados por encima del plazo pactado |
| Atraso promedio | 20 | Días entre el pago y el vencimiento, ponderado por monto |
| Peor caso (p90) | 10 | El atraso del percentil 90: consistencia, no promedio |
| Puntualidad | 10 | % de facturas pagadas dentro de los 5 días del vencimiento |
| Antigüedad | 15 | Mitad por meses como cliente, mitad por continuidad de compra |
| Volumen | 10 | Facturación 12m en escala log, más tendencia de los últimos 3 meses |
| Situación hoy | 10 | Qué proporción de lo que debe está vencido |

**Por qué el DSO pesa más que el atraso por factura.** El atraso por factura
depende de a qué factura se imputó cada recibo, y en Contabilium los recibos no
se aplican a la más vieja: se cierran facturas nuevas y queda una cola de
facturas viejas abiertas. Eso hace que las facturas cerradas parezcan puntuales
aunque la deuda real no baje. El caso testigo es el cliente más grande de la
cartera: tiene un atraso promedio de −2,7 días —o sea, "paga antes"— y un DSO
de 153 días con plazo pactado de 60. El DSO no se deja engañar por eso.

#### Dos correcciones sobre los datos crudos

1. **El vencimiento no se lee de Contabilium.** El campo `FechaVencimiento`
   trae siempre emisión + 30 días, aunque la condición sea 60 o 90. Usarlo
   inflaba en 60 días el atraso de los clientes con plazo largo, que son justo
   los que interesan. Acá el vencimiento se deriva de la condición de venta.
2. **La fecha de pago no está en la factura.** El campo `Pagos[]` viene siempre
   vacío. Sale de cruzar contra los recibos.

#### Cómo salen el límite y la tasa

El **límite** es la exposición natural del plazo: a un cliente que compra $X
por mes y tiene 60 días, en régimen se le van a deber dos meses de compra. Eso
se ajusta por un factor de banda y se recorta por un tope de meses de compra,
para no concentrar riesgo en un solo nombre.

La **tasa** es costo de fondos + spread + prima de riesgo. El costo de fondos
es un parámetro que hay que confirmar (el default de 14% es un supuesto). La
prima por banda sale de la mora que cada banda efectivamente tiene: si la banda
C se atrasa 18 días más que la A, esos días cuestan plata y esa es la prima
piso. No cubre el riesgo de no cobrar nunca, que no se puede medir con estos
datos.

#### Qué haría falta para que esto sea un score de verdad

- **Serie de incobrables**: marcar qué deuda se dio de baja y cuándo. Sin eso
  no hay calibración posible.
- **Clearing de Informes**: para clientes nuevos, y para ver los cheques
  rechazados que con nosotros no pasaron.
- **Registro de cheques rechazados propios**: hoy no está en Contabilium.
        """
    )
