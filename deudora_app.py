"""
deudora_app.py — La cuenta corriente de cada cliente, por vendedor.

Entry point independiente, como Comisiones, Listas, Facturador, Rendición y
Scoring: misma codebase, URL distinta, password propia (`deudora_password`).
Reusa `contabilium_client_id` / `contabilium_client_secret`.

Público: Ernesto (jefatura) y Valeria (operaciones). Los vendedores todavía
no entran acá — reciben su PDF. Cuando se decida darles acceso hará falta
una password por vendedor, que hoy la app no tiene.

Toda la lógica vive en `deudora.py` (puro), la bajada en `credito_api.py` y
el PDF en `deudora_pdf.py`. Este archivo es solo UI.

Por qué no es una tab del Scoring: son dos preguntas distintas. El Scoring
responde "a quién le damos más plazo"; la deudora responde "a quién hay que
ir a cobrarle esta semana". Comparten los datos, no la lectura.
"""

from __future__ import annotations

import hmac
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import streamlit as st

import api_loader
import cambios_deudora
import credito_api as ca
import cuotas as CU
import deudora as D
import deudora_pdf as DP
import gsheets
import theme
import tutorial_deudora
import vendedores as V

st.set_page_config(
    page_title="Cuenta Corriente — GSU",
    page_icon="🧾",
    layout="wide",
    initial_sidebar_state="expanded",
)

ACCENT = "#C8552F"
INK = "#1A1A1A"

theme.apply_theme()

# Botones en naranja y métricas más chicas, como en el resto de las apps. Sin
# achicar la métrica, un importe de 8 cifras se corta en la mitad de las
# columnas. El prefijo `stMain` y el `!important` NO son decorativos: sin
# ellos el default de Streamlit gana y el importe vuelve a cortarse.
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


# ---------------------------------------------------------------------
# Formato
# ---------------------------------------------------------------------

def uyu(v, decimales: int = 0) -> str:
    """1234.5 → '$ 1.235'. Vacío legible si no es número."""
    try:
        s = f"{float(v):,.{decimales}f}"
    except (TypeError, ValueError):
        return "—"
    return "$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def fecha_corta(d) -> str:
    return "—" if pd.isna(d) else f"{pd.Timestamp(d):%d/%m/%Y}"


def dias(v) -> str:
    return "—" if pd.isna(v) else f"{int(v)}"


NOMBRE_TRAMO = {clave: etiqueta for clave, etiqueta, _, _ in D.BUCKETS}


def _mapa_vendedores() -> dict[int, str]:
    """ID → nombre legible. Cae al email cuando no hay nombre corto."""
    return {
        vid: V.NOMBRE_VENDEDOR.get(vid, email.split("@")[0].title())
        for vid, email in V.VENDEDORES.items()
    }


# ---------------------------------------------------------------------
# Tutorial y Novedades (modales abiertos desde el encabezado)
# ---------------------------------------------------------------------

@st.dialog("Tutorial — Cuenta Corriente", width="large")
def _tutorial_dialog():
    tutorial_deudora.render()


@st.dialog("Novedades — qué cambió en la app", width="large")
def _cambios_dialog():
    cambios_deudora.render()


# =====================================================================
# Auth
# =====================================================================

def _check_password() -> bool:
    """Login con `deudora_password`. Key de sesión: `auth_deudora`."""
    if st.session_state.get("auth_deudora", False):
        return True

    left, center, right = st.columns([1, 2, 1])
    with center:
        st.markdown(
            "<h1 style='margin-bottom:0.25rem;'>Cuenta Corriente</h1>",
            unsafe_allow_html=True,
        )
        st.caption(
            "Cuánto debe cada cliente, desde cuándo, y qué facturas quedaron "
            "abiertas. Uso interno de Suprabond."
        )
        with st.form("login_deudora"):
            pwd = st.text_input("Contraseña", type="password",
                                placeholder="••••••••")
            ok = st.form_submit_button("Ingresar", use_container_width=True)
        if ok:
            stored = st.secrets.get("deudora_password")
            if stored is None:
                st.error(
                    "Falta configurar `deudora_password` en los secrets. "
                    "Avisar a Mariano."
                )
                return False
            if hmac.compare_digest(str(stored), pwd):
                st.session_state.auth_deudora = True
                st.rerun()
            else:
                st.error("Contraseña incorrecta.")
    return False


if not _check_password():
    st.stop()


# =====================================================================
# Carga
# =====================================================================

@st.cache_resource(show_spinner=False)
def _session():
    return api_loader.obtener_token(
        st.secrets["contabilium_client_id"],
        st.secrets["contabilium_client_secret"],
    )


@st.cache_data(ttl=86_400, show_spinner=False)
def cargar(meses: int, _hoy: date):
    """Comprobantes + recibos + clientes. Cache de 24 h.

    Los recibos se piden día por día porque el endpoint no pagina (ver
    `credito_api`), así que la primera carga del día tarda varios minutos.
    Es el mismo pull que hace el Scoring; si ya se abrió esa app hoy, esta
    igual lo repite: son procesos distintos en Streamlit Cloud.
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
    return df_comp, df_imp, df_cli, rep


# --- Sidebar ---------------------------------------------------------
st.sidebar.markdown("### Parámetros")
meses = st.sidebar.slider(
    "Meses de historia", 3, 24, 12,
    help="Las facturas emitidas antes de esta ventana no aparecen, aunque "
         "sigan impagas. Con 12 meses la cartera queda completa salvo casos "
         "muy viejos.",
)
solo_deuda = st.sidebar.checkbox(
    "Mostrar solo clientes con saldo", value=True,
    help="Destildar para ver también a los que tienen crédito a favor.",
)
st.sidebar.markdown("---")
if st.sidebar.button("Cerrar sesión", use_container_width=True):
    st.session_state.pop("auth_deudora", None)
    st.rerun()


HOY = date.today()
hoy_ts = pd.Timestamp(HOY)

with st.spinner("Bajando datos de Contabilium… la primera vez del día tarda."):
    df_comp, df_imp, df_cli, rep = cargar(meses, HOY)

@st.cache_data(ttl=300, show_spinner=False)
def cargar_cuotas():
    """Divisiones en cuotas cargadas a mano. TTL corto: se editan a diario.

    Best-effort igual que la foto del día del Scoring: si la planilla no está
    configurada o falla, la deudora funciona sin las cuotas. Lo que NO se
    hace es un `except: pass` mudo — el motivo se guarda y se muestra, porque
    si no la app parecería "sin cuotas cargadas" en vez de "rota".
    """
    try:
        df = gsheets.read_cuotas(dict(st.secrets.get("gsheets", {})))
        return CU.indexar(df), None
    except Exception as e:  # noqa: BLE001
        return {}, f"{type(e).__name__}: {e}"


cuotas_idx, error_cuotas = cargar_cuotas()

df_cli = D.agregar_vendedor(df_cli, _mapa_vendedores(), V.VENDEDORES_OP_EXCLUIDOS)
resumen = D.resumen_por_cliente(df_comp, df_imp, df_cli, hoy=hoy_ts,
                                cuotas_idx=cuotas_idx)
movimientos = D.armar_movimientos(df_comp, df_imp, hoy=hoy_ts,
                                  cuotas_idx=cuotas_idx)
if solo_deuda:
    resumen = resumen[resumen["deuda_total"] > 1.0].reset_index(drop=True)
totales = D.totales_por_vendedor(resumen)


# =====================================================================
# Encabezado
# =====================================================================

enc_info, enc_btn = st.columns([3, 1.3])
with enc_info:
    st.markdown("##### Cuenta Corriente")
    st.caption(
        f"{len(resumen)} clientes con saldo · al {HOY:%d/%m/%Y} · "
        f"{meses} meses de historia"
    )
    st.caption(f"Versión de la app: {cambios_deudora.ultima_actualizacion()}")
with enc_btn:
    bt1, bt2 = st.columns(2)
    if bt1.button("📖 Tutorial", use_container_width=True):
        _tutorial_dialog()
    if bt2.button("🆕 Novedades", use_container_width=True):
        _cambios_dialog()
    if st.button("↻ Recargar datos", use_container_width=True):
        cargar.clear()
        st.session_state.pop("pdf_todos", None)
        st.rerun()

if not rep.completo:
    st.warning(f"Carga incompleta — {rep.resumen()}", icon="⚠️")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Deuda total", uyu(resumen["deuda_total"].sum()))
c2.metric("Vencido", uyu(resumen["vencido_neto"].sum()),
          help="Descontando las notas de crédito que el cliente todavía "
               "tiene sin aplicar.")
c3.metric("Más de 90 días", uyu(resumen["b_90_mas"].sum()))
c4.metric("Clientes con vencido",
          f"{int((resumen['vencido_neto'] > 1).sum())}")

seccion = st.segmented_control(
    "Sección",
    ["Por vendedor", "Clientes", "Cuenta de un cliente"],
    default="Por vendedor", label_visibility="collapsed", key="seccion_deudora",
)


# ------------------------------------------------------- Por vendedor
if seccion == "Por vendedor":
    st.markdown("#### La cartera de cada uno")
    vista = totales.copy()
    vista.columns = [
        "Vendedor", "Clientes", "Deuda", "Vencido bruto", "Vencido",
        "% vencido", *[NOMBRE_TRAMO[c] for c in D.COLS_BUCKET],
    ]
    st.dataframe(
        vista.drop(columns=["Vencido bruto"]).style.format({
            "Deuda": uyu, "Vencido": uyu, "% vencido": "{:.0f}%",
            **{NOMBRE_TRAMO[c]: uyu for c in D.COLS_BUCKET},
        }),
        use_container_width=True, hide_index=True,
    )
    st.caption(
        "«Vencido» ya descuenta las notas de crédito sin aplicar. Los "
        "clientes sin vendedor asignado en Contabilium aparecen agrupados "
        "aparte: su deuda es real y tiene que estar contada en algún lado."
    )

    st.markdown("#### Descargar el informe")
    d1, d2 = st.columns([2, 2])
    with d1:
        v_sel = st.selectbox("Vendedor", totales["vendedor"].tolist(),
                             key="pdf_vendedor")
        st.download_button(
            f"📄 PDF de {v_sel}",
            data=DP.generar_pdf_vendedor(v_sel, resumen, movimientos, HOY),
            file_name=f"Cuenta corriente {v_sel} {HOY:%Y-%m-%d}.pdf",
            mime="application/pdf",
        )
    with d2:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        # La cartera entera son ~145 páginas: se arma recién cuando alguien la
        # pide, no en cada rerun.
        if st.button("Armar el PDF de toda la cartera"):
            st.session_state.pdf_todos = DP.generar_pdf_todos(
                resumen, movimientos, HOY)
        if st.session_state.get("pdf_todos"):
            st.download_button(
                "📄 Descargar toda la cartera",
                data=st.session_state.pdf_todos,
                file_name=f"Cuenta corriente {HOY:%Y-%m-%d}.pdf",
                mime="application/pdf",
            )


# ------------------------------------------------------------ Clientes
elif seccion == "Clientes":
    fc1, fc2, fc3 = st.columns([2, 2, 3])
    with fc1:
        filtro_v = st.selectbox(
            "Vendedor", ["Todos"] + totales["vendedor"].tolist(),
            key="filtro_vendedor")
    with fc2:
        filtro_t = st.selectbox(
            "Antigüedad", ["Todas"] + [NOMBRE_TRAMO[c] for c in D.COLS_BUCKET],
            key="filtro_tramo")
    with fc3:
        busca = st.text_input("Buscar cliente", placeholder="Nombre o RUT",
                              key="busca_cliente")

    vista = resumen.copy()
    if filtro_v != "Todos":
        vista = vista[vista["vendedor"] == filtro_v]
    if filtro_t != "Todas":
        clave = next(c for c, e in NOMBRE_TRAMO.items() if e == filtro_t)
        vista = vista[vista["peor_tramo"] == clave]
    if busca.strip():
        t = busca.strip().lower()
        vista = vista[
            vista["razon_social"].str.lower().str.contains(t, na=False)
            | vista["documento"].astype(str).str.contains(t, na=False)
        ]

    cols = ["razon_social", "vendedor", "facturas_abiertas", "deuda_total",
            "vencido_neto", "credito_a_favor", "dias_mas_vieja",
            "ultima_compra", "ultimo_pago", "ciudad", "telefono"]
    vista = vista[cols].rename(columns={
        "razon_social": "Cliente", "vendedor": "Vendedor",
        "facturas_abiertas": "Facturas", "deuda_total": "Deuda",
        "vencido_neto": "Vencido", "credito_a_favor": "A favor",
        "dias_mas_vieja": "Días de la más vieja",
        "ultima_compra": "Última compra", "ultimo_pago": "Último pago",
        "ciudad": "Ciudad", "telefono": "Teléfono",
    })
    st.dataframe(
        vista.style.format({
            "Deuda": uyu, "Vencido": uyu, "A favor": uyu,
            "Días de la más vieja": dias,
            "Última compra": fecha_corta, "Último pago": fecha_corta,
        }),
        use_container_width=True, hide_index=True, height=520,
    )
    st.caption(f"{len(vista)} clientes.")


# --------------------------------------------------- Cuenta de un cliente
else:
    if resumen.empty:
        st.info("No hay clientes con saldo para mostrar.")
        st.stop()

    etiquetas = {
        f"{r.razon_social} — {uyu(r.deuda_total)}": r.id_cliente
        for r in resumen.itertuples()
    }
    elegido = st.selectbox("Cliente", list(etiquetas), key="cliente_cuenta")
    cid = etiquetas[elegido]
    cli = resumen[resumen["id_cliente"] == cid].iloc[0]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Deuda", uyu(cli["deuda_total"]))
    m2.metric("Vencido", uyu(cli["vencido_neto"]))
    m3.metric("Factura más vieja", f"{dias(cli['dias_mas_vieja'])} días")
    m4.metric("Último pago", fecha_corta(cli["ultimo_pago"]))

    ficha = " · ".join(
        str(x) for x in [cli["documento"], cli["ciudad"], cli["telefono"],
                         cli["vendedor"]] if x and str(x) != "nan"
    )
    st.caption(ficha)

    solo_abiertas = st.toggle(
        "Solo facturas con saldo", value=True,
        help="Apagalo para ver la cuenta completa: facturas, notas de "
             "crédito y pagos en orden cronológico. Es la vista que sirve "
             "cuando el cliente discute un movimiento.",
    )
    ext = D.extracto_de_cliente(movimientos, cid, solo_abiertas=solo_abiertas)

    vista = ext[["fecha", "tipo_mov", "comprobante", "cuota", "cond_pago",
                 "vencimiento", "dias_vencido", "debe", "haber",
                 "saldo_pendiente"]].rename(columns={
        "fecha": "Fecha", "tipo_mov": "Tipo", "comprobante": "Comprobante",
        "cuota": "Cuota", "cond_pago": "Condición", "vencimiento": "Vence",
        "dias_vencido": "Días", "debe": "Debe", "haber": "Haber",
        "saldo_pendiente": "Saldo",
    })
    st.dataframe(
        vista.style.format({
            "Fecha": fecha_corta, "Vence": fecha_corta, "Días": dias,
            "Debe": lambda v: uyu(v, 2) if float(v or 0) else "",
            "Haber": lambda v: uyu(v, 2) if float(v or 0) else "",
            "Saldo": lambda v: "" if pd.isna(v) else uyu(v, 2),
        }),
        use_container_width=True, hide_index=True, height=460,
    )

    if not solo_abiertas:
        st.caption(
            "La cuenta arranca donde arranca la ventana de meses elegida, "
            "así que el primer saldo puede venir de antes. Por eso no hay "
            "columna de saldo acumulado: sería un total que empieza a "
            "contar por la mitad."
        )

    # ---------------------------------------------------- Dividir en cuotas
    st.markdown("---")
    with st.expander("✂️ Dividir una factura en cuotas"):
        st.caption(
            "Contabilium emite una sola factura aunque la venta sea a "
            "30/60/90: no sabe qué parte vence en cada cuota. Acá se carga "
            "esa división una vez y queda hasta que la factura se cobre. "
            "**Dividir no cambia lo que el cliente debe**, solo reparte el "
            "saldo entre las fechas que le corresponden."
        )
        if error_cuotas:
            st.error(
                f"No pude leer la planilla de cuotas: {error_cuotas}",
                icon="⚠️",
            )

        abiertas_cli = D.extracto_de_cliente(movimientos, cid,
                                             solo_abiertas=True)
        ids_fac = (abiertas_cli[["id_comprobante", "comprobante", "cond_pago"]]
                   .drop_duplicates(subset="id_comprobante"))
        if ids_fac.empty:
            st.info("Este cliente no tiene facturas con saldo para dividir.")
        else:
            etiq = {
                f"{r.comprobante} — {r.cond_pago}"
                + ("  ·  YA DIVIDIDA" if int(r.id_comprobante) in cuotas_idx
                   else ""): int(r.id_comprobante)
                for r in ids_fac.itertuples()
            }
            elegida = st.selectbox("Factura", list(etiq), key="fac_cuotas")
            fid = etiq[elegida]
            fila = df_comp[df_comp["id"] == fid].iloc[0]
            total_fac = float(abs(fila["total"]))

            st.caption(
                f"Total de la factura: **{uyu(total_fac, 2)}** · "
                f"emitida el {fecha_corta(fila['emision'])}"
            )

            if fid in cuotas_idx:
                propuesta = cuotas_idx[fid].copy()
            else:
                propuesta = CU.sugerir(fid, fila["cond_venta"],
                                       fila["emision"], total_fac)
                if propuesta.empty:
                    # Condición sin cuotas: igual se puede dividir a mano,
                    # que es el caso del cliente al que se le cobra de a poco
                    # sin que la condición lo diga.
                    propuesta = CU.sugerir(
                        fid, "1/2", fila["emision"], total_fac)
                    propuesta["vencimiento"] = [
                        pd.Timestamp(fila["emision"]) + pd.Timedelta(days=30),
                        pd.Timestamp(fila["emision"]) + pd.Timedelta(days=60),
                    ]

            editada = st.data_editor(
                propuesta[["nro_cuota", "vencimiento", "importe"]],
                num_rows="dynamic", use_container_width=True, hide_index=True,
                key=f"editor_cuotas_{fid}",
                column_config={
                    "nro_cuota": st.column_config.NumberColumn(
                        "Cuota", min_value=1, step=1, format="%d"),
                    "vencimiento": st.column_config.DateColumn(
                        "Vence", format="DD/MM/YYYY"),
                    "importe": st.column_config.NumberColumn(
                        "Importe", min_value=0.0, step=100.0, format="%.2f"),
                },
            )

            suma = pd.to_numeric(editada["importe"], errors="coerce").sum()
            dif = suma - total_fac
            if abs(dif) > 0.05:
                st.warning(
                    f"Las cuotas suman {uyu(suma, 2)} y la factura es de "
                    f"{uyu(total_fac, 2)} — faltan {uyu(abs(dif), 2)}.",
                    icon="⚠️",
                )
            else:
                st.success(f"Las cuotas cierran con la factura: {uyu(suma, 2)}")

            quien = st.text_input(
                "Tu nombre", key=f"quien_cuotas_{fid}",
                placeholder="Para saber quién cargó la división")
            nota = st.text_input(
                "Nota (opcional)", key=f"nota_cuotas_{fid}",
                placeholder="Ej: acordado con el cliente por teléfono")

            b1, b2 = st.columns([1, 1])
            if b1.button("Guardar la división", key=f"guardar_cuotas_{fid}"):
                problemas = CU.validar(editada, total_fac)
                if problemas:
                    for p in problemas:
                        st.error(p)
                else:
                    # Un solo timestamp para todas las filas: es lo que las
                    # agrupa como una carga. Va en UTC — `%X` con locale es-AR
                    # imprime las 19:02 como 07:02.
                    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    filas = [
                        {
                            "timestamp": ts,
                            "id_comprobante": str(fid),
                            "numero": str(fila["numero"]),
                            "id_cliente": str(cid),
                            "nro_cuota": str(int(r.nro_cuota)),
                            "vencimiento": f"{pd.Timestamp(r.vencimiento):%Y-%m-%d}",
                            "importe": f"{float(r.importe):.2f}",
                            "usuario": quien.strip(),
                            "nota": nota.strip(),
                            "anulado": "",
                        }
                        for r in editada.itertuples()
                    ]
                    try:
                        n = gsheets.append_cuotas(
                            dict(st.secrets.get("gsheets", {})), filas)
                        cargar_cuotas.clear()
                        st.success(f"Guardadas {n} cuotas.")
                        st.rerun()
                    except Exception as e:  # noqa: BLE001
                        st.error(f"No se pudo guardar: {type(e).__name__}: {e}")

            if fid in cuotas_idx and b2.button(
                    "Deshacer la división", key=f"anular_cuotas_{fid}"):
                ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
                try:
                    gsheets.append_cuotas(
                        dict(st.secrets.get("gsheets", {})),
                        [{
                            "timestamp": ts, "id_comprobante": str(fid),
                            "numero": str(fila["numero"]), "id_cliente": str(cid),
                            "nro_cuota": "1", "vencimiento": "", "importe": "0",
                            "usuario": quien.strip(), "nota": "anulación",
                            "anulado": "SI",
                        }])
                    cargar_cuotas.clear()
                    st.success("La factura vuelve a mostrarse entera.")
                    st.rerun()
                except Exception as e:  # noqa: BLE001
                    st.error(f"No se pudo deshacer: {type(e).__name__}: {e}")
