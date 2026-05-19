"""
pedidos_app.py — App de Streamlit para leer y verificar los pedidos que
los vendedores mandan por mail (NOTA DE PEDIDO G.S.U.).

Entry point separado del dashboard (`app.py`), Comisiones
(`comisiones_app.py`) y Facturación (`facturador_app.py`). Cuarto deploy
de Streamlit Cloud del mismo repo: misma codebase, URL distinta, secrets
propios. Reutiliza `pedidos.py` (toda la lógica de lectura) y `theme.py`.

Fase 1 (esta versión): lee el xlsx tal cual viene del mail —protegido y
con celdas combinadas, no importa—, muestra los pedidos limpios y verifica
el control de totales (suma sin IVA × 1,22 == TOTAL CON IVA del Excel).
NO toca Contabilium.

Fase 1b (identificación del cliente, read-only): resuelve el Nro. Cliente
del Excel al cliente real de Contabilium vía el campo `Codigo` (0XXXX-C)
y chequea que la razón social cuadre con el nombre que escribió el
vendedor. La suma de facturas vencidas queda fuera (ver pedidos_deuda.py:
la API no la expone directamente). Si la API no está configurada, el
lector funciona igual y la identificación queda deshabilitada.

Auth: 1 password adicional (`pedidos_password` en secrets), distinto de
los otros tres.
"""

from __future__ import annotations

import hmac
import io
from datetime import datetime

import pandas as pd
import streamlit as st

import api_loader
import pedidos
import pedidos_deuda
import pedidos_orden
import theme

try:
    import gsheets  # opcional: audit log de Fase 2 si está configurado.
except ImportError:
    gsheets = None

# =====================================================================
# Page config + theme  (idéntico a facturador_app.py para misma UI)
# =====================================================================

st.set_page_config(
    page_title="Carga de Pedidos — GSU",
    page_icon="📥",
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
    """Login con `pedidos_password`. Key session: `auth_pedidos`."""
    if st.session_state.get("auth_pedidos", False):
        return True

    left, center, right = st.columns([1, 2, 1])
    with center:
        st.markdown(
            "<h1 style='margin-bottom:0.25rem;'>Carga de Pedidos</h1>",
            unsafe_allow_html=True,
        )
        st.caption(
            "Lectura y control de los pedidos que mandan los vendedores. "
            "Acceso restringido a personal autorizado de Suprabond."
        )
        with st.form("login_pedidos", clear_on_submit=False):
            pwd = st.text_input(
                "Contraseña",
                type="password",
                autocomplete="current-password",
                placeholder="••••••••",
            )
            submit = st.form_submit_button("Ingresar", use_container_width=True)
        if submit:
            stored = st.secrets.get("pedidos_password")
            if stored is None:
                st.error(
                    "La contraseña no está configurada en secrets. "
                    "Avisar a Mariano."
                )
                return False
            if hmac.compare_digest(stored, pwd):
                st.session_state.auth_pedidos = True
                st.rerun()
            else:
                st.error("Contraseña incorrecta.")
    return False


if not _check_password():
    st.stop()


# =====================================================================
# Helpers
# =====================================================================

# Por debajo de esto la "deuda vencida" es ruido de redondeo (centavos):
# no se escala como caso para Valeria. Ajustable.
UMBRAL_VENCIDA_UYU = 1.0


def _fmt_uyu(v: float) -> str:
    return f"$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _pedidos_a_df(lista: list[pedidos.Pedido]) -> pd.DataFrame:
    """Aplana todos los pedidos a una fila por ítem, para exportar/auditar."""
    filas = []
    for p in lista:
        for i in p.items:
            filas.append(
                {
                    "pedido": p.hoja,
                    "nro_pedido_vend": p.nro_pedido,
                    "cliente": p.cliente,
                    "nro_cliente": p.nro_cliente,
                    "vendedor": p.nro_vendedor,
                    "cond_pago": p.cond_pago,
                    "codigo": i.codigo,
                    "descripcion": i.descripcion,
                    "und_x_caja": i.und_x_caja,
                    "cantidad": i.cantidad,
                    "precio_sin_iva": i.precio_sin_iva,
                    "subtotal": i.subtotal,
                    "es_combo": i.es_combo,
                    "control_total_ok": p.total_ok,
                }
            )
    return pd.DataFrame(filas)


# =====================================================================
# API (read-only) — identificación de clientes. Degrada elegante si
# las credenciales de Contabilium no están configuradas.
# =====================================================================

@st.cache_resource(show_spinner=False)
def _api_session():
    cid = st.secrets.get("contabilium_client_id")
    csec = st.secrets.get("contabilium_client_secret")
    if not cid or not csec or "email-admin@suprabond" in str(cid):
        return None  # placeholder o sin configurar
    try:
        return api_loader.obtener_token(cid, csec)
    except Exception:  # noqa: BLE001 — sin API el lector igual sirve
        return None


@st.cache_data(ttl=3600, show_spinner="Identificando clientes en Contabilium...")
def _mapa_clientes():
    sess = _api_session()
    if sess is None:
        return None
    try:
        _, mapa = pedidos_deuda.cargar_mapa_clientes(sess)
        return mapa
    except Exception:  # noqa: BLE001
        return None


@st.cache_data(
    ttl=86400,
    show_spinner="Consultando deuda (pull histórico — la 1ª vez del día tarda)...",
)
def _deuda_map(meses: int):
    """Deuda viva por RUT, reusando la maquinaria del dashboard.
    Cacheado 24h: se paga el N+1 una vez por día."""
    sess = _api_session()
    if sess is None:
        return None
    try:
        _, m, _err = pedidos_deuda.deuda_por_documento(sess, meses)
        return m
    except Exception:  # noqa: BLE001
        return None


@st.cache_data(ttl=3600, show_spinner="Cargando catálogo de Contabilium...")
def _maps_orden():
    """(mapa_conceptos, mapa_clientes_full, inventario_VENTAS) o None.
    Read-only, cacheado 1h. Necesario para armar/cargar órdenes."""
    sess = _api_session()
    if sess is None:
        return None
    try:
        sess, mc = pedidos_orden.cargar_mapa_conceptos(sess)
        sess, mcli = pedidos_orden.cargar_mapa_clientes_full(sess)
        sess, inv = pedidos_orden.resolver_inventario_ventas(sess)
        if inv is None:
            return None
        return (mc, mcli, inv)
    except Exception:  # noqa: BLE001
        return None


# =====================================================================
# Sidebar
# =====================================================================

with st.sidebar:
    st.markdown("### Carga de Pedidos")
    st.caption(
        "Subí el Excel `NOTA DE PEDIDO G.S.U.` tal cual lo manda el "
        "vendedor. No hace falta desproteger ni descombinar nada."
    )
    st.markdown("---")
    if st.button("Cerrar sesión", use_container_width=True):
        st.session_state.pop("auth_pedidos", None)
        st.rerun()


# =====================================================================
# Cuerpo principal — Fase 1: lector + control de totales
# =====================================================================

st.markdown("<h1 style='margin-bottom:0;'>Carga de Pedidos</h1>",
            unsafe_allow_html=True)
st.caption(
    "Lee el archivo del mail, descarta el catálogo de relleno y deja "
    "solo los pedidos reales, con el control de totales hecho."
)

archivo = st.file_uploader(
    "Excel del pedido (.xlsx)",
    type=["xlsx"],
    accept_multiple_files=False,
)

if archivo is None:
    st.info("Subí el archivo `NOTA DE PEDIDO G.S.U.` para empezar.")
    st.stop()

try:
    lista = pedidos.leer_pedidos(io.BytesIO(archivo.getvalue()))
except Exception as e:  # noqa: BLE001 — queremos mostrar el error, no romper
    st.error(
        "No pude leer el archivo. ¿Es la plantilla `NOTA DE PEDIDO "
        f"G.S.U.`? Detalle técnico: {e}"
    )
    st.stop()

if not lista:
    st.warning("El archivo no tiene hojas de pedido reconocibles.")
    st.stop()

# --- Resumen global ---
n_total = len(lista)
n_ok = sum(1 for p in lista if p.total_ok)
n_rev = n_total - n_ok
total_general = sum(
    (p.total_con_iva_declarado or 0.0) for p in lista
)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Pedidos", n_total)
c2.metric("Control OK", n_ok)
c3.metric("A revisar", n_rev)
c4.metric("Total con IVA", _fmt_uyu(total_general))

if n_rev:
    st.warning(
        f"{n_rev} pedido(s) no pasan el control de totales. "
        "Revisar antes de cargar en Contabilium."
    )

st.markdown("---")

# --- Detalle por pedido ---
for p in lista:
    sello = "🟢 Control OK" if p.total_ok else "🔴 REVISAR totales"
    combo = " · ⚠️ contiene combo" if p.tiene_combos else ""
    encabezado = (
        f"{p.hoja} — {p.cliente} (Nro. Cliente {p.nro_cliente}) · "
        f"{len(p.items)} ítems · {_fmt_uyu(p.total_con_iva_declarado or 0)} · "
        f"{sello}{combo}"
    )
    with st.expander(encabezado, expanded=not p.total_ok):
        meta = f"Vendedor {p.nro_vendedor} · Pedido del vendedor #{p.nro_pedido}"
        if p.fecha:
            meta += f" · Fecha {p.fecha:%d/%m/%Y}"
        st.caption(meta)
        if p.cond_pago:
            st.info(f"**Condición de pago (texto libre, revisar a mano):** "
                    f"{p.cond_pago}")

        df = pd.DataFrame(
            [
                {
                    "Código": i.codigo,
                    "Descripción": i.descripcion,
                    "Und x caja": i.und_x_caja,
                    "Cantidad": i.cantidad,
                    "Precio s/IVA": i.precio_sin_iva,
                    "Subtotal": i.subtotal,
                    "Combo": "Sí" if i.es_combo else "",
                }
                for i in p.items
            ]
        )
        st.dataframe(df, use_container_width=True, hide_index=True)

        ctrl = (
            f"Suma sin IVA {_fmt_uyu(p.suma_subtotales)}  ·  "
            f"con IVA calculado {_fmt_uyu(p.total_con_iva_calculado)}  ·  "
            f"declarado en Excel {_fmt_uyu(p.total_con_iva_declarado or 0)}"
        )
        if p.total_ok:
            st.success(f"✅ {ctrl}")
        else:
            st.error(
                f"❌ {ctrl} — la diferencia supera la tolerancia. "
                "El Excel del vendedor puede estar pifiado: NO cargar sin chequear."
            )

# --- Export ---
st.markdown("---")
df_export = _pedidos_a_df(lista)
st.download_button(
    "⬇️ Descargar pedidos en CSV",
    data=df_export.to_csv(index=False).encode("utf-8-sig"),
    file_name="pedidos_leidos.csv",
    mime="text/csv",
)

# --- Fase 1b: identificación del cliente en Contabilium ---
st.markdown("---")
st.subheader("Identificación del cliente en Contabilium")

_mapa = _mapa_clientes()
if _mapa is None:
    st.warning(
        "La API de Contabilium no está configurada o no respondió "
        "(`contabilium_client_id` / `contabilium_client_secret` en "
        "secrets). El lector y el control de totales funcionan igual; "
        "la identificación de clientes queda deshabilitada."
    )
else:
    _SEM = {
        "ok": "🟢 OK",
        "revisar_nombre": "🟠 Revisar nombre",
        "no_encontrado": "🔴 No encontrado",
        "sin_codigo": "⚪ Sin Nro. Cliente",
    }
    filas_id = []
    n_ok = n_rev = n_prob = 0
    for p in lista:
        r = pedidos_deuda.identificar(p.nro_cliente, p.cliente, _mapa)
        est = r["estado"]
        if est == "ok":
            n_ok += 1
        elif est == "revisar_nombre":
            n_rev += 1
        else:
            n_prob += 1
        filas_id.append(
            {
                "Pedido": p.hoja,
                "Nombre en Excel": p.cliente,
                "Nro. Cliente": p.nro_cliente,
                "Código Contab.": r.get("codigo") or "—",
                "Cliente Contabilium": r.get("razon_social", "—"),
                "RUT": r.get("rut", "—"),
                "Estado": _SEM.get(est, est),
            }
        )

    cA, cB, cC = st.columns(3)
    cA.metric("Identificados OK", n_ok)
    cB.metric("Revisar nombre", n_rev)
    cC.metric("Sin identificar", n_prob)
    if n_rev or n_prob:
        st.warning(
            "Hay pedidos cuyo cliente NO se identifica con claridad. "
            "Revisar el Nro. Cliente antes de cargar en Contabilium: "
            "cargar al cliente equivocado factura mal."
        )
    st.dataframe(
        pd.DataFrame(filas_id), use_container_width=True, hide_index=True
    )
    st.markdown("---")
    st.markdown("**Deuda vencida del cliente**")
    cD1, _cD2 = st.columns([1, 3])
    with cD1:
        _meses = st.number_input(
            "Ventana (meses hacia atrás)",
            min_value=3, max_value=36, value=12, step=3,
            help="Se pullea facturación por fecha de emisión; una factura "
                 "más vieja que la ventana no aparece. 12 = igual que el "
                 "dashboard.",
        )
    if st.button(
        "🔎 Chequear deuda vencida (consulta histórica — la 1ª vez del "
        "día puede tardar 1-2 min; después es instantáneo)"
    ):
        st.session_state["pedidos_deuda_meses"] = int(_meses)

    _m = st.session_state.get("pedidos_deuda_meses")
    if _m is not None:
        deuda = _deuda_map(_m)
        if deuda is None:
            st.error(
                "No pude traer la deuda (API no configurada o el pull "
                "falló). La identificación de arriba igual es válida."
            )
        else:
            filas_d = []
            casos_valeria = []
            for p in lista:
                ident = pedidos_deuda.identificar(
                    p.nro_cliente, p.cliente, _mapa
                )
                rut = str(ident.get("rut", "") or "").strip()
                d = deuda.get(rut)
                if d is None:
                    sem, venc, peor, tot = "🟢 Sin deuda viva", 0.0, "—", 0.0
                elif d["vencida"] >= UMBRAL_VENCIDA_UYU:
                    sem = "🔴 DEUDA VENCIDA"
                    venc, peor, tot = (
                        d["vencida"], d["peor_bucket"] or "—", d["deuda_total"]
                    )
                    casos_valeria.append(
                        f"**{p.hoja}** · {ident.get('razon_social', p.cliente)} "
                        f"(RUT {rut}) · vencido {_fmt_uyu(d['vencida'])} "
                        f"· tramo {d['peor_bucket'] or '—'}"
                    )
                else:
                    sem, venc, peor, tot = (
                        "🟡 Deuda al día", 0.0, "—", d["deuda_total"]
                    )
                filas_d.append(
                    {
                        "Pedido": p.hoja,
                        "Cliente": ident.get("razon_social", p.cliente),
                        "RUT": rut or "—",
                        "Deuda total": _fmt_uyu(tot),
                        "Vencida": _fmt_uyu(venc),
                        "Tramo más viejo": peor,
                        "Estado": sem,
                    }
                )
            if casos_valeria:
                st.error(
                    f"{len(casos_valeria)} pedido(s) con **deuda vencida** "
                    "— requieren autorización de Valeria antes de cargar:"
                )
                for c in casos_valeria:
                    st.markdown(f"- {c}")
            else:
                st.success("Ningún pedido tiene deuda vencida. 🟢")
            st.dataframe(
                pd.DataFrame(filas_d),
                use_container_width=True, hide_index=True,
            )
            st.caption(
                "Reusa la lógica de la tab Cobranzas del dashboard "
                "(`metrics.aging_por_cliente`), join por RUT. Ventana "
                f"{_m} meses. Una factura impaga más vieja que la ventana "
                "no aparece (misma limitación que el dashboard)."
            )


# =====================================================================
# Fase 2 — Carga de órdenes en Contabilium (⚠️ ESCRIBE EN PRODUCCIÓN)
# =====================================================================

st.markdown("---")
st.subheader("Carga de órdenes en Contabilium")
st.warning(
    "⚠️ Esta sección **crea órdenes de venta reales en Contabilium** y "
    "**reserva stock al instante**. Revisá todo antes de confirmar."
)

_maps = _maps_orden()
_m_deuda = st.session_state.get("pedidos_deuda_meses")
_deuda = _deuda_map(_m_deuda) if _m_deuda else None

if _maps is None:
    st.info(
        "La API de Contabilium no está configurada o no respondió. "
        "La carga de órdenes no está disponible (el lector sí funciona)."
    )
elif _deuda is None:
    st.info(
        "Primero corré **«🔎 Chequear deuda vencida»** acá arriba. Sin "
        "saber la deuda no se puede saber qué pedidos necesitan "
        "autorización de Valeria — la carga queda bloqueada hasta eso."
    )
else:
    _mapa_conc, _mapa_cli_full, _inv_id = _maps
    _hoy = datetime.now().date()

    seleccionados = []
    resumen_rows = []
    for p in lista:
        ident = pedidos_deuda.identificar(p.nro_cliente, p.cliente, _mapa)
        rut = str(ident.get("rut", "") or "").strip()
        d = _deuda.get(rut)
        deuda_vencida = bool(d and d["vencida"] >= UMBRAL_VENCIDA_UYU)
        tiene_comentario = bool((p.cond_pago or "").strip())

        descuentos = {}
        for it in p.items:
            v = st.session_state.get(f"desc_{p.hoja}_{it.fila}", 0.0)
            if v:
                descuentos[it.fila] = float(v)

        armado = pedidos_orden.armar_body_orden(
            p, _mapa_cli_full, _mapa_conc, _inv_id,
            descuentos=descuentos, fecha=_hoy,
        )

        titulo = (
            f"{p.hoja} — {ident.get('razon_social', p.cliente)} "
            f"({len(p.items)} ítems)"
            + ("  ·  🔴 deuda vencida" if deuda_vencida else "")
            + ("  ·  📝 comentario de precio" if tiene_comentario else "")
        )
        with st.expander(titulo, expanded=deuda_vencida or tiene_comentario):
            if not armado.ok:
                st.error("No se puede cargar:")
                for pr in armado.problemas:
                    st.markdown(f"- {pr}")
                resumen_rows.append(
                    {"Pedido": p.hoja, "Estado": "🔴 No cargable"}
                )
                continue

            apr_deuda = True
            apr_precio = True

            if deuda_vencida:
                st.error(
                    f"Deuda vencida {_fmt_uyu(d['vencida'])} "
                    f"(tramo {d['peor_bucket'] or '—'})."
                )
                apr_deuda = st.checkbox(
                    "✅ APROBADO — deuda (Valeria autorizó cargar igual)",
                    key=f"aprdeuda_{p.hoja}",
                )

            if tiene_comentario:
                st.info(
                    f"Comentario del vendedor (Cond. de Pago): "
                    f"**{p.cond_pago}**"
                )
                apr_precio = st.checkbox(
                    "✅ APROBADO — precio (comentario revisado)",
                    key=f"aprprecio_{p.hoja}",
                )
                with st.expander("Desglosar / aplicar descuento por ítem"):
                    for it in p.items:
                        c1, c2 = st.columns([3, 1])
                        c1.markdown(
                            f"`{it.codigo}` {it.descripcion[:32]} — "
                            f"{_fmt_uyu(it.precio_sin_iva)} ×{it.cantidad:g}"
                        )
                        c2.number_input(
                            "Desc %", min_value=0.0, max_value=99.0,
                            step=1.0, format="%g",
                            key=f"desc_{p.hoja}_{it.fila}",
                            label_visibility="collapsed",
                        )
                    st.caption(
                        "Ej: precio 100 con 32 → se carga 68 "
                        "(va al campo Bonificación)."
                    )

            if not (apr_deuda and apr_precio):
                falta = []
                if deuda_vencida and not apr_deuda:
                    falta.append("APROBADO deuda")
                if tiene_comentario and not apr_precio:
                    falta.append("APROBADO precio")
                st.warning(
                    "Falta: " + " + ".join(falta)
                    + " — este pedido NO se va a cargar."
                )
                resumen_rows.append(
                    {"Pedido": p.hoja, "Estado": "🟠 Falta aprobación"}
                )
                continue

            incluir = st.checkbox(
                "Incluir este pedido en la carga",
                key=f"incluir_{p.hoja}", value=False,
            )
            st.success(
                "Listo para cargar · "
                f"{_fmt_uyu(p.total_con_iva_declarado or 0)}"
                + ("  ·  ⚠️ contiene combo" if armado.tiene_combo else "")
            )
            resumen_rows.append({
                "Pedido": p.hoja,
                "Estado": "🟢 Seleccionado" if incluir
                else "⚪ Listo (sin tildar)",
            })
            if incluir:
                seleccionados.append((p, armado.body, {
                    "rut": rut,
                    "razon_social": ident.get("razon_social", p.cliente),
                    "deuda_vencida": deuda_vencida,
                    "tiene_comentario": tiene_comentario,
                    "descuentos": descuentos,
                }))

    st.markdown("### Confirmación")
    if resumen_rows:
        st.dataframe(
            pd.DataFrame(resumen_rows),
            use_container_width=True, hide_index=True,
        )
    st.markdown(f"**{len(seleccionados)}** pedido(s) tildados para cargar.")

    if seleccionados:
        with st.expander("Ver exactamente qué se va a mandar (revisión final)"):
            for p, body, _meta in seleccionados:
                st.markdown(f"**{p.hoja}**")
                st.json(body)

        usuario = st.text_input(
            "Tu nombre/iniciales (para el registro de auditoría)",
            key="pedidos_usuario",
        )
        st.warning(
            "Al confirmar se crean las órdenes en Contabilium (reservan "
            "stock). **La primera vez**, tildá UN solo pedido de prueba, "
            "verificalo en Contabilium, y recién después cargá el resto."
        )
        gate = st.text_input(
            "Escribí CARGAR PEDIDOS para habilitar el botón",
            key="pedidos_gate",
        )
        listo = (
            gate.strip() == "CARGAR PEDIDOS"
            and len(seleccionados) > 0
            and bool(usuario.strip())
        )
        if st.button(
            "🚀 CARGAR PEDIDOS EN CONTABILIUM",
            disabled=not listo, type="primary",
        ):
            session = _api_session()
            if session is None:
                st.error("No hay sesión con Contabilium. Revisá los secrets.")
            else:
                resultados, log_rows = [], []
                barra = st.progress(0.0)
                for idx, (p, body, meta) in enumerate(seleccionados, 1):
                    fila = {
                        "timestamp": datetime.now().isoformat(
                            timespec="seconds"),
                        "usuario": usuario.strip(),
                        "pedido": p.hoja,
                        "nro_cliente": p.nro_cliente,
                        "cliente": meta["razon_social"],
                        "rut": meta["rut"],
                        "id_cliente": body["idCliente"],
                        "id_vendedor": body["IDVendedor"],
                        "n_items": len(body["items"]),
                        "total_sin_iva": round(sum(
                            i["precioUnitario"] * i["cantidad"]
                            * (1 - i["bonificacion"] / 100.0)
                            for i in body["items"]), 2),
                        "aprobado_deuda": "SI" if meta["deuda_vencida"]
                        else "N/A",
                        "aprobado_precio": "SI" if meta["tiene_comentario"]
                        else "N/A",
                        "descuentos": "; ".join(
                            f"fila{f}:{v:g}%"
                            for f, v in meta["descuentos"].items()) or "—",
                    }
                    try:
                        session, r = pedidos_orden.crear_orden(session, body)
                        if r.status_code in (200, 201):
                            try:
                                j = r.json()
                            except Exception:  # noqa: BLE001
                                j = {}
                            if not isinstance(j, dict):
                                j = {}
                            num = str(j.get("NumeroOrden")
                                      or j.get("Numero")
                                      or j.get("numeroOrden") or "")
                            oid = str(j.get("ID") or j.get("Id")
                                      or j.get("id") or "")
                            fila.update(status="OK", numero_orden=num,
                                        id_orden=oid, error="")
                            resultados.append({
                                "Pedido": p.hoja, "Resultado": "✅ Cargada",
                                "Orden": num or oid or "(sin nº en respuesta)",
                            })
                        else:
                            msg = f"HTTP {r.status_code}: {r.text[:200]}"
                            fila.update(status="ERROR", numero_orden="",
                                        id_orden="", error=msg)
                            resultados.append({
                                "Pedido": p.hoja, "Resultado": "❌ Error",
                                "Orden": msg})
                    except Exception as exc:  # noqa: BLE001
                        fila.update(status="ERROR", numero_orden="",
                                    id_orden="", error=str(exc)[:250])
                        resultados.append({
                            "Pedido": p.hoja, "Resultado": "❌ Error",
                            "Orden": str(exc)[:120]})
                    log_rows.append(fila)
                    barra.progress(idx / len(seleccionados))

                ok = sum(1 for r in resultados if "✅" in r["Resultado"])
                ko = len(resultados) - ok
                if ko == 0:
                    st.success(
                        f"✅ {ok} orden(es) cargada(s) correctamente "
                        "en Contabilium."
                    )
                else:
                    st.error(
                        f"{ok} cargada(s), {ko} con error. Revisar abajo."
                    )
                st.dataframe(
                    pd.DataFrame(resultados),
                    use_container_width=True, hide_index=True,
                )

                if gsheets is not None and "gsheets_facturacion" in st.secrets:
                    try:
                        n = gsheets.append_log_carga_pedidos(
                            dict(st.secrets["gsheets_facturacion"]), log_rows)
                        st.caption(
                            f"Registro de auditoría guardado ({n} fila/s) "
                            "en Google Sheet (tab log_carga_pedidos)."
                        )
                    except Exception as exc:  # noqa: BLE001
                        st.warning(
                            "Órdenes procesadas, pero NO pude guardar el "
                            f"audit log: {exc}"
                        )
                else:
                    st.caption(
                        "Audit log a Sheet deshabilitado "
                        "(`[gsheets_facturacion]` no configurado en secrets)."
                    )
