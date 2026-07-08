"""
rendicion_app.py — App de Streamlit para la Rendición de Cobranzas GSU.

Entry point separado del dashboard (`app.py`), del Facturador
(`facturador_app.py`), de Pedidos (`pedidos_app.py`) y de Comisiones
(`comisiones_app.py`). Se deploya en Streamlit Cloud como otro app del
mismo repo: misma codebase, URL distinta, secrets propios. Reutiliza
`api_loader.py` y `theme.py`, y delega TODA la lógica a `rendicion.py`.

FASE 1 — SIMULADOR (SOLO LECTURA). Lee la planilla de rendición de
cobranzas del vendedor, busca las facturas en Contabilium, calcula la NC
por descuento comercial (10%) y el cobro esperado, y arma un reporte
OK / REVISAR. **No crea NC, ni recibos, ni imputa nada.** El objetivo es
validar la lógica con riesgo cero antes de automatizar la escritura
(Fase 2). Ver decisión de diseño 2026-07-01.

Auth: 1 password adicional (`rendicion_password` en secrets).

Flujo:
  1. Login con password.
  2. Subir la planilla de rendición (.xlsx).
  3. Elegir rango de fechas de facturas (para acotar el índice) y tolerancia.
  4. "Analizar" → lee planilla + pagina comprobantes + calcula + verifica saldos.
  5. Reporte: KPIs, tabla OK/REVISAR, filas descartadas, descarga Excel.
"""

from __future__ import annotations

import hmac
import io
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

import api_loader
import rendicion
import rendicion_ejecutor
import theme
import tutorial_rendicion


# Modal del tutorial. Se abre desde la sidebar.
@st.dialog("Tutorial — Rendición de Cobranzas", width="large")
def _tutorial_dialog():
    tutorial_rendicion.render()


# =====================================================================
# Page config + theme
# =====================================================================

st.set_page_config(
    page_title="Rendición de Cobranzas — GSU",
    page_icon="💵",
    layout="wide",
    initial_sidebar_state="expanded",
)
theme.apply_theme()

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
        font-size: 1.5rem !important;
        line-height: 1.1 !important;
    }
    [data-testid="stMain"] [data-testid="stMetricLabel"] {
        font-size: 0.75rem !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# =====================================================================
# Auth gate
# =====================================================================

def _check_password() -> bool:
    """Login con `rendicion_password`. Key session: `auth_rendicion`."""
    if st.session_state.get("auth_rendicion", False):
        return True

    left, center, right = st.columns([1, 2, 1])
    with center:
        st.markdown(
            "<h1 style='margin-bottom:0.25rem;'>Rendición de Cobranzas</h1>",
            unsafe_allow_html=True,
        )
        st.caption(
            "Simulador de cobranzas desde la planilla de los vendedores. "
            "Acceso restringido a administración y operaciones."
        )
        with st.form("login_rendicion", clear_on_submit=False):
            pwd = st.text_input(
                "Contraseña",
                type="password",
                autocomplete="current-password",
                placeholder="••••••••",
            )
            submit = st.form_submit_button("Ingresar", use_container_width=True)
        if submit:
            stored = st.secrets.get("rendicion_password")
            if stored is None:
                st.error(
                    "La contraseña no está configurada en secrets. "
                    "Avisar a Mariano."
                )
                return False
            if hmac.compare_digest(stored, pwd):
                st.session_state.auth_rendicion = True
                st.rerun()
            else:
                st.error("Contraseña incorrecta.")
    return False


if not _check_password():
    st.stop()


# =====================================================================
# Caches: API session + índice de facturas
# =====================================================================

@st.cache_resource
def _api_session():
    """Token OAuth cacheado por process. ApiSession dura ~24h."""
    return api_loader.obtener_token(
        st.secrets["contabilium_client_id"],
        st.secrets["contabilium_client_secret"],
    )


@st.cache_data(ttl=600, show_spinner="Consultando facturas en Contabilium...")
def _cargar_indice(fecha_desde_iso: str, fecha_hasta_iso: str) -> dict:
    """Índice de facturas del rango, por Nº. Cacheado 10min (paginar el
    rango es lo más caro del flujo)."""
    session = _api_session()
    session, indice = rendicion.construir_indice_facturas(
        session, fecha_desde_iso, fecha_hasta_iso
    )
    return indice


# =====================================================================
# Helpers
# =====================================================================

DIAS_DEFAULT = 90  # ventana de búsqueda de facturas por defecto


def _rango_default() -> tuple[date, date]:
    """Default: últimos DIAS_DEFAULT días. Cubre el grueso de las cobranzas
    (facturas recientes) sin bajar de más. Se puede ampliar en la UI."""
    hoy = date.today()
    return hoy - timedelta(days=DIAS_DEFAULT), hoy


def _fmt(v: float) -> str:
    return f"$ {v:,.0f}".replace(",", ".")


# =====================================================================
# Cuerpo
# =====================================================================

st.title("💵 Rendición de Cobranzas")
st.caption(
    "Fase 1 — Simulador (solo lectura). Analiza la planilla y arma el "
    "reporte OK / REVISAR. **No** genera NC, recibos ni imputaciones."
)

with st.sidebar:
    if st.button("📖 Tutorial", use_container_width=True, key="btn_tutorial"):
        _tutorial_dialog()
    st.header("Parámetros")
    d_ini, d_fin = _rango_default()
    # El rango de fechas es una opción secundaria: por defecto se buscan las
    # facturas de los últimos 90 días. Solo hace falta tocarlo si se cobran
    # facturas más viejas. Va en un expander colapsado.
    with st.expander("📅 Rango de fechas de facturas"):
        st.caption(
            "Ventana de **emisión** de facturas donde el tool busca (no es la "
            "fecha del recibo). Default: últimos 90 días. Ampliá el «desde» si "
            "cobrás facturas más viejas; cuanto más ancho, más tarda."
        )
        fecha_desde = st.date_input(
            "Facturas desde", value=d_ini, format="DD/MM/YYYY"
        )
        fecha_hasta = st.date_input(
            "Facturas hasta", value=d_fin, format="DD/MM/YYYY"
        )
    st.caption(
        f"Buscando facturas: **{fecha_desde.strftime('%d/%m/%Y')} → "
        f"{fecha_hasta.strftime('%d/%m/%Y')}**"
    )
    tolerancia = st.number_input(
        "Tolerancia ± ($)",
        min_value=0.0, value=rendicion.TOLERANCIA_DEFAULT, step=10.0,
        help="Diferencia máxima entre lo cobrado y lo esperado para "
             "considerar que la cobranza cuadra.",
    )
    if st.button("🔄 Refrescar facturas de Contabilium"):
        _cargar_indice.clear()
        st.rerun()

archivo = st.file_uploader(
    "Planilla de Rendición de Cobranzas (.xlsx)", type=["xlsx"]
)

if archivo is None:
    st.info(
        "Subí la planilla que arman los vendedores. La herramienta espera "
        "las columnas: Fecha, Nro. Cliente, Nro Factura, Cobro Efectivo, "
        "Cobro Cheque, Total Recibo (y opcional: Descuento, Nº Cheque)."
    )
    st.stop()

if fecha_desde > fecha_hasta:
    st.error("El rango de fechas es inválido: 'desde' es posterior a 'hasta'.")
    st.stop()

# --- Leer planilla ---
try:
    filas, descartadas = rendicion.leer_planilla(archivo.getvalue())
except Exception as e:  # noqa: BLE001 — mostrar el error al usuario, no crashear.
    st.error(f"No se pudo leer la planilla: {e}")
    st.stop()

if not filas:
    st.warning("La planilla no tiene filas con Nº de factura para procesar.")
    if descartadas:
        st.dataframe(pd.DataFrame(descartadas), use_container_width=True)
    st.stop()

st.success(f"Planilla leída: {len(filas)} fila(s) con factura.")

# El análisis pega a la API (saldos), así que corre SOLO al tocar "Analizar"
# y se guarda en session_state. Clave: cualquier interacción con la tabla
# (marcar un checkbox) dispara un rerun de todo el script; si el análisis
# viviera detrás del botón, el rerun lo perdería (el botón da True una sola
# vez) y habría que re-analizar. Guardándolo, los reruns re-renderizan la
# tabla desde el resultado guardado y el data_editor conserva las marcas
# por su `key`.
if st.button("▶️ Analizar", type="primary"):
    indice = _cargar_indice(fecha_desde.isoformat(), fecha_hasta.isoformat())
    resultados = rendicion.analizar(filas, indice, tolerancia=tolerancia)
    with st.spinner("Verificando saldos de las facturas encontradas..."):
        session = _api_session()
        rendicion.verificar_saldos(session, resultados)
    st.session_state.rend_df = rendicion.resultados_a_dataframe(resultados)
    st.session_state.rend_resultados = resultados  # objetos, para la ejecución
    st.session_state.rend_descartadas = descartadas
    # Nueva corrida: descartar marcas del editor previo y ejecuciones previas.
    st.session_state.pop("editor_reporte", None)
    st.session_state.pop("rend_ejecutadas", None)

if "rend_df" not in st.session_state:
    st.info("Tocá **▶️ Analizar** para procesar la planilla.")
    st.stop()

df = st.session_state.rend_df
descartadas = st.session_state.get("rend_descartadas", [])
n_ok = int((df["Estado"] == rendicion.ESTADO_OK).sum())
n_rev = int((df["Estado"] == rendicion.ESTADO_REVISAR).sum())

# --- KPIs ---
c1, c2, c3, c4 = st.columns(4)
c1.metric("Filas analizadas", len(df))
c2.metric("✅ OK (automatizables)", n_ok)
c3.metric("⚠️ A revisar", n_rev)
c4.metric("Descartadas (sin factura)", len(descartadas))

# --- Reporte + aprobación en UNA sola tabla editable ---
st.subheader("Reporte y aprobación")
st.caption(
    "Cada fila trae su clasificación (✅ OK / ⚠️ Revisar) y el motivo. La "
    "columna **Aprobar** (a la izquierda) define qué cobranzas quedan "
    "automatizables: las OK vienen pre-marcadas; revisá las ⚠️ y marcá las "
    "que valides (o desmarcá una OK que no quieras automatizar)."
)

# Tabla del reporte con la casilla Aprobar embebida. Estado con emoji para
# que se lea de un vistazo (el data_editor no soporta colores de fila).
df_ed = df.copy()
df_ed["Estado"] = df_ed["Estado"].map(
    {rendicion.ESTADO_OK: "✅ OK", rendicion.ESTADO_REVISAR: "⚠️ Revisar"}
).fillna(df_ed["Estado"])
df_ed.insert(0, "Aprobar", df["Estado"] == rendicion.ESTADO_OK)

edited = st.data_editor(
    df_ed,
    use_container_width=True,
    hide_index=True,
    key="editor_reporte",
    column_config={
        "Aprobar": st.column_config.CheckboxColumn(
            "Aprobar",
            help="Marcá las cobranzas que quedan automatizables. Las OK vienen "
                 "pre-marcadas; marcá las ⚠️ que verifiques manualmente.",
            default=False,
        ),
    },
    disabled=[c for c in df_ed.columns if c != "Aprobar"],
)

aprobadas = edited[edited["Aprobar"]].drop(columns=["Aprobar"])
pendientes = edited[~edited["Aprobar"]].drop(columns=["Aprobar"])

st.metric("👍 Automatizables (aprobadas)", len(aprobadas), help=(
    f"De {len(df)} filas: {n_ok} OK y {n_rev} a revisar. "
    "Cambia al marcar/desmarcar la casilla Aprobar."
))

if descartadas:
    with st.expander(f"Filas descartadas ({len(descartadas)}) — sin Nº de factura"):
        st.dataframe(pd.DataFrame(descartadas), use_container_width=True, hide_index=True)

# --- Descarga Excel (reporte + automatizables + pendientes + descartadas) ---
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as xl:
    df.to_excel(xl, index=False, sheet_name="Reporte completo")
    aprobadas.to_excel(xl, index=False, sheet_name="Automatizables")
    if not pendientes.empty:
        pendientes.to_excel(xl, index=False, sheet_name="Pendientes revision")
    if descartadas:
        pd.DataFrame(descartadas).to_excel(xl, index=False, sheet_name="Descartadas")
st.download_button(
    "⬇️ Descargar (reporte + automatizables + pendientes)",
    data=buf.getvalue(),
    file_name=f"cobranzas_{date.today().isoformat()}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

# =====================================================================
# Fase 2 — Ejecución en Contabilium (ESCRITURA)
# =====================================================================

st.divider()
st.subheader("⚙️ Ejecutar en Contabilium (Fase 2)")
st.warning(
    "Esto **escribe en Contabilium de verdad**: crea la Nota de Crédito del "
    "10%, el recibo, e imputa contra la factura. Se hace **de a una cobranza**, "
    "con vista previa y confirmación. Empezá probando con **una factura chica**."
)

# ResultadoFila por nº de fila de Excel (para cruzar con la tabla de aprobación).
_resultados = st.session_state.get("rend_resultados", [])
_res_por_fila = {r.fila.fila_excel: r for r in _resultados}
_filas_aprobadas = set(aprobadas["Fila"]) if not aprobadas.empty else set()

# Ejecutables = aprobadas + auto-ejecutables (1 factura, no NC, no parcial).
_ejecutables = {
    f: _res_por_fila[f] for f in _filas_aprobadas
    if f in _res_por_fila and _res_por_fila[f].es_ejecutable
}
_aprob_no_ejec = sorted(_filas_aprobadas - set(_ejecutables))

st.session_state.setdefault("rend_ejecutadas", {})
_ejecutadas = st.session_state.rend_ejecutadas

if _aprob_no_ejec:
    st.info(
        "Filas aprobadas que **no** se pueden ejecutar automáticamente "
        f"(multi-factura o entrega/pago parcial): {_aprob_no_ejec}. "
        "Esas se cargan a mano en Contabilium."
    )

# Candidatas: ejecutables que todavía no se ejecutaron OK en esta sesión.
_pendientes_ejec = {
    f: r for f, r in _ejecutables.items()
    if not _ejecutadas.get(f, {}).get("ok")
}


@st.cache_data(show_spinner=False)
def _factura_detalle(_sess, id_factura: int) -> dict:
    """Detalle de la factura para armar el plan. Cacheado por id para no
    re-pegar a la API en cada rerun (el `_sess` va con guion bajo: no se
    hashea)."""
    _, fac = rendicion_ejecutor.obtener_factura(_sess, id_factura)
    return fac


if not _ejecutables:
    st.caption(
        "No hay cobranzas aprobadas auto-ejecutables. Marcá alguna OK (1 "
        "factura, sin entrega) en la tabla de arriba."
    )
else:
    st.caption(
        f"{len(_ejecutables)} cobranza(s) aprobada(s) ejecutable(s); "
        f"{len(_ejecutadas)} ya ejecutada(s) esta sesión."
    )

    if _pendientes_ejec:
        opciones = {
            f"Fila {f} · {r.params_ejecucion()['numero_factura']} · "
            f"cliente {r.fila.nro_cliente} · cobrado ${r.fila.cobrado:,.0f}": f
            for f, r in _pendientes_ejec.items()
        }
        sel_label = st.selectbox(
            "Elegí UNA cobranza para ejecutar", options=list(opciones)
        )
        fila_sel = opciones[sel_label]
        res_sel = _pendientes_ejec[fila_sel]
        params = res_sel.params_ejecucion()

        # --- Nº de cheque: se ingresa/confirma acá (opción 2, decisión Valeria
        # 2026-07-08). Se prellena con lo que el vendedor haya puesto en la
        # planilla (queda como registro), pero el número que se IMPUTA es el que
        # Valeria confirma con el cheque físico a la vista — así un typo del
        # vendedor no rompe la imputación. ---
        nro_cheque_conf = params["nro_cheque"]
        if params["cobro_cheque"] > 0:
            nro_cheque_conf = st.text_input(
                "Nº de cheque (con el cheque a la vista)",
                value=params["nro_cheque"],
                key=f"chq_{fila_sel}",
                help="El cheque ya está precargado en Contabilium y se referencia "
                     "por su número. Verificá el número del cheque físico. Si el "
                     "vendedor lo anotó en la planilla, viene precargado acá.",
            )

        # --- Vista previa (dry-run) ---
        try:
            sess = _api_session()
            fac = _factura_detalle(sess, params["id_factura"])
            plan = rendicion_ejecutor.planificar(
                fac,
                aplica_nc=params["aplica_nc"],
                cobro_efectivo=params["cobro_efectivo"],
                cobro_cheque=params["cobro_cheque"],
                nro_cheque=nro_cheque_conf,
                fecha_emision_iso=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            )
        except Exception as e:  # noqa: BLE001
            st.error(f"No se pudo preparar la ejecución: {e}")
            st.stop()

        st.markdown("**Vista previa — esto es lo que se va a hacer:**")
        pv1, pv2, pv3 = st.columns(3)
        pv1.metric("Total factura (c/IVA)", f"$ {plan.total_con_iva:,.2f}")
        pv2.metric("NC 10%" if plan.aplica_nc else "NC", f"$ {plan.nc_con_iva:,.2f}")
        pv3.metric("Cobro (efec+cheque)",
                   f"$ {plan.cobro_efectivo + plan.cobro_cheque:,.2f}")
        st.caption(
            f"Factura {plan.numero_factura} · saldo actual "
            f"$ {plan.saldo_actual:,.2f} → quedará en $ 0."
            + (f" · Cheque nº {plan.nro_cheque}" if plan.cobro_cheque else "")
        )
        for adv in plan.advertencias:
            st.warning(f"⚠️ {adv}")

        with st.expander("Ver los datos técnicos que se enviarán"):
            if plan.body_nc:
                st.markdown("**1) Nota de Crédito** (`/comprobantes/anularComprobante`)")
                st.json(plan.body_nc)
            st.markdown("**2) Recibo + imputación** (`/comprobantes/cobrar`)")
            st.json(plan.body_cobro)

        # --- Gate de confirmación ---
        st.markdown("---")
        confirm = st.text_input(
            "Para ejecutar, escribí **CONFIRMAR** en mayúsculas:",
            key=f"confirm_{fila_sel}",
        )
        falta_cheque = params["cobro_cheque"] > 0 and not nro_cheque_conf.strip()
        if falta_cheque:
            st.info("Ingresá el **Nº de cheque** (arriba) para poder ejecutar.")
        if st.button(
            "🚀 Ejecutar esta cobranza en Contabilium",
            type="primary",
            disabled=(confirm.strip() != "CONFIRMAR" or falta_cheque),
        ):
            with st.spinner("Escribiendo en Contabilium…"):
                sess = _api_session()
                sess, resultado = rendicion_ejecutor.ejecutar(
                    sess, plan, dry_run=False
                )
            st.session_state.rend_ejecutadas[fila_sel] = {
                "ok": resultado.ok,
                "pasos": resultado.pasos,
                "error": resultado.error,
                "id_nc": resultado.id_nc,
                "numero_nc": resultado.numero_nc,
                "numero_factura": plan.numero_factura,
            }
            st.rerun()
    else:
        st.success("Todas las cobranzas ejecutables ya se procesaron. 🎉")

# --- Resultados de ejecuciones de esta sesión ---
if _ejecutadas:
    st.markdown("**Ejecuciones de esta sesión:**")
    for f, r in sorted(_ejecutadas.items()):
        icono = "✅" if r.get("ok") else "❌"
        titulo = (
            f"{icono} Fila {f} · {r.get('numero_factura','?')}"
            + (f" · NC {r.get('numero_nc')}" if r.get("numero_nc") else "")
        )
        with st.expander(titulo, expanded=not r.get("ok")):
            for paso in r.get("pasos", []):
                st.write("•", paso)
            if r.get("error"):
                st.error(r["error"])

st.caption(
    "Las ejecuciones son reales e inmediatas en Contabilium. Si una NC se "
    "crea pero el recibo falla, el detalle de arriba muestra el nº de NC para "
    "revertirla a mano. Las aprobaciones y ejecuciones valen para esta sesión."
)
