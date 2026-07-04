"""
televentas_app.py — CRM de Televentas GSU.

App Streamlit standalone (deploy separado, password `televentas_password`)
para la Vendedora Televentas: trabaja la base de clientes de Contabilium
como lista de leads, con filtros/campañas, ficha 360°, registro de
gestiones (llamadas/WhatsApp), agenda de seguimientos, carga de pedidos y
alta de clientes nuevos.

Construida sobre la infraestructura Contabilium existente:
  - LEE: clientes enriquecidos + facturación (historial) + productos.
  - ESCRIBE: pedido (reusa `pedidos_orden.crear_orden`) y cliente nuevo
    (`televentas_cliente.crear_cliente`). Ambas con confirmación.
  - CRM propio en Google Sheet (`televentas_crm`, sección
    `[gsheets_televentas]` en secrets).
"""

from __future__ import annotations

import hmac
import urllib.parse
from datetime import date, datetime

import pandas as pd
import streamlit as st

import api_loader
import facturador
import pedidos_orden
import televentas_cliente
import televentas_crm
import televentas_data
import theme
from subrubros import SUBRUBROS
from vendedores import VENDEDORES

try:
    import tutorial_televentas
except ImportError:
    tutorial_televentas = None


# =====================================================================
# Page config + theme + auth
# =====================================================================

st.set_page_config(
    page_title="Televentas — GSU", page_icon="📞",
    layout="wide", initial_sidebar_state="expanded",
)
theme.apply_theme()


@st.dialog("Tutorial — Televentas", width="large")
def _tutorial_dialog():
    if tutorial_televentas:
        tutorial_televentas.render()
    else:
        st.info("Tutorial en preparación.")


def _check_password() -> bool:
    if st.session_state.get("auth_televentas", False):
        return True
    _, center, _ = st.columns([1, 2, 1])
    with center:
        st.markdown("<h1>Televentas GSU</h1>", unsafe_allow_html=True)
        st.caption("CRM y gestión de leads para la Vendedora Televentas.")
        with st.form("login_tv"):
            pwd = st.text_input("Contraseña", type="password")
            ok = st.form_submit_button("Ingresar", use_container_width=True)
        if ok:
            stored = st.secrets.get("televentas_password")
            if stored is None:
                st.error("Falta `televentas_password` en secrets.")
            elif hmac.compare_digest(stored, pwd):
                st.session_state.auth_televentas = True
                st.rerun()
            else:
                st.error("Contraseña incorrecta.")
    return False


if not _check_password():
    st.stop()


# =====================================================================
# Sesión API + loaders cacheados
# =====================================================================

@st.cache_resource
def _api_session():
    return api_loader.obtener_token(
        st.secrets["contabilium_client_id"],
        st.secrets["contabilium_client_secret"],
    )


@st.cache_data(ttl=3600, show_spinner="Cargando clientes…")
def _cargar_clientes(_s) -> pd.DataFrame:
    _, df = televentas_data.cargar_clientes_enriquecidos(_s, vendedores_map=VENDEDORES)
    return df


@st.cache_data(ttl=1800, show_spinner="Cargando historial de compras…")
def _cargar_facturacion(_s, desde: str, hasta: str) -> pd.DataFrame:
    _, df, _errs = api_loader.load_fc_api(_s, desde, hasta, vendedores_map=VENDEDORES)
    return df


@st.cache_data(ttl=3600, show_spinner="Cargando productos…")
def _cargar_productos(_s) -> pd.DataFrame:
    _, df = api_loader.load_productos_api(_s, subrubros_map=SUBRUBROS)
    return df


@st.cache_data(ttl=300, show_spinner="Cargando gestión CRM…")
def _cargar_actividad(_gs) -> pd.DataFrame:
    return televentas_crm.leer_actividad(dict(_gs))


@st.cache_data(ttl=3600, show_spinner="Cargando depósitos…")
def _cargar_depositos(_s) -> list[dict]:
    _, deps = facturador.cargar_inventarios(_s)
    return deps


# =====================================================================
# Helpers de contacto (teléfono / WhatsApp)
# =====================================================================

def _normalizar_celular_uy(telefono: str) -> str | None:
    """Devuelve el celular en formato internacional UY para wa.me, o None.

    Toma el primer número que parezca celular (empieza en 09) del campo
    (puede traer varios separados por / o ,). Le saca el 0 inicial y
    antepone 598. Ej "44757008/098077510" → "59898077510".
    """
    import re
    for parte in re.split(r"[/,;]", str(telefono or "")):
        digs = re.sub(r"\D", "", parte)
        if digs.startswith("09") and len(digs) == 9:
            return "598" + digs[1:]
        if digs.startswith("598") and len(digs) == 12:
            return digs
    return None


def _link_whatsapp(telefono: str, mensaje: str) -> str | None:
    cel = _normalizar_celular_uy(telefono)
    if not cel:
        return None
    return f"https://wa.me/{cel}?text={urllib.parse.quote(mensaje)}"


def _fmt_money(v) -> str:
    try:
        return f"$ {float(v):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return "—"


# =====================================================================
# Sidebar
# =====================================================================

with st.sidebar:
    if st.button("📖 Tutorial", use_container_width=True):
        _tutorial_dialog()
    st.header("Televentas")
    agente = st.text_input("Tu nombre (agente)", value=st.session_state.get("tv_agente", ""))
    st.session_state.tv_agente = agente
    st.divider()
    st.caption("Datos de Contabilium")
    if st.button("🔄 Resincronizar", use_container_width=True):
        _cargar_clientes.clear(); _cargar_facturacion.clear()
        _cargar_productos.clear(); _cargar_actividad.clear()
        st.rerun()
    meses_hist = st.slider("Meses de historial", 3, 12, 12,
                           help="Ventana de compras para segmentar (activo/dormido).")


# =====================================================================
# Carga de datos + construcción de leads
# =====================================================================

_gs = st.secrets.get("gsheets_televentas")
if not _gs:
    st.warning(
        "No está configurada la sección `[gsheets_televentas]` en secrets. "
        "El registro de gestiones (CRM) no va a funcionar hasta configurarla."
    )

sess = _api_session()
hoy = date.today()
# rango: `meses_hist` hacia atrás desde el día 1 del mes actual
m = hoy.month - (meses_hist - 1)
y = hoy.year
while m <= 0:
    m += 12; y -= 1
desde = date(y, m, 1)

df_cli = _cargar_clientes(sess)
df_fc = _cargar_facturacion(sess, desde.isoformat(), hoy.isoformat())
df_prod = _cargar_productos(sess)

# mapa SKU → sub_rubro para poblar los subgrupos comprados
sku_sub = dict(zip(df_prod["sku"], df_prod["sub_rubro"])) if not df_prod.empty else {}
resumen = televentas_data.resumen_compras(df_fc, sku_subrubro_map=sku_sub)
leads = televentas_data.construir_leads(df_cli, resumen)

# merge del estado CRM
df_act = pd.DataFrame(columns=televentas_crm.ACTIVIDAD_COLS)
if _gs:
    try:
        df_act = _cargar_actividad(_gs)
    except Exception as e:  # noqa: BLE001
        st.warning(f"No se pudo leer el CRM: {e}")
estado_crm = televentas_crm.estado_actual_por_lead(df_act)
if not estado_crm.empty:
    leads = leads.merge(estado_crm, how="left", left_on="documento", right_index=True)
for c in ("estado", "ultimo_resultado", "proximo_seguimiento"):
    if c not in leads.columns:
        leads[c] = ""
leads["estado"] = leads["estado"].fillna("Sin gestionar")


st.title("📞 Televentas GSU")

# =====================================================================
# Tablero rápido
# =====================================================================
k = st.columns(5)
k[0].metric("Clientes", len(leads))
k[1].metric("Con teléfono", int((leads["telefono"].str.len() > 0).sum()))
k[2].metric("Dormidos (>90d)", int(leads["segmento"].isin(["dormido", "dormido_profundo"]).sum()))
k[3].metric("Gestionados", int((leads["estado"] != "Sin gestionar").sum()))
_seg_hoy = leads["proximo_seguimiento"].astype(str) == hoy.isoformat()
k[4].metric("Seguimientos hoy", int(_seg_hoy.sum()))

# =====================================================================
# Ficha 360° + gestión + pedido (definidas antes de usarse en los tabs)
# =====================================================================
def _ficha_360(lead, df_fc, df_act, df_prod, sess):
    st.divider()
    nombre = lead["nombre_fantasia"] or lead["razon_social"]
    st.markdown(f"### 🗂️ {nombre}")
    a, b, c = st.columns(3)
    a.markdown(f"**Razón social:** {lead['razon_social']}")
    a.markdown(f"**RUT/Doc:** {lead['documento']}  ·  **Código:** {lead['codigo']}")
    a.markdown(f"**Vendedor:** {lead['vendedor'] or '—'}")
    b.markdown(f"**📍** {lead['domicilio']}, {lead['ciudad']} ({lead['departamento']})")
    b.markdown(f"**☎️** {lead['telefono'] or '—'}")
    b.markdown(f"**✉️** {lead['email'] or '—'}")
    dias = lead.get("dias_sin_compra")
    c.metric("Días sin comprar", f"{int(dias)}" if pd.notna(dias) else "—")
    c.metric("Ticket promedio", _fmt_money(lead.get("ticket_prom")))
    if str(lead.get("observaciones") or "").strip():
        st.info(f"📝 Nota de entrega: {lead['observaciones']}")

    msg = (f"Hola {nombre}! Te contacto de Suprabond (GSU). "
           "¿Cómo andás de stock? Tenemos novedades para tu ferretería.")
    link = _link_whatsapp(lead["telefono"], msg)
    if link:
        st.link_button("💬 Abrir WhatsApp con mensaje", link)
    else:
        st.caption("Sin celular válido para WhatsApp.")

    with st.expander("🛒 Historial de compras"):
        top = lead.get("top_skus") or []
        if top:
            st.write("**Top productos:**")
            st.table(pd.DataFrame(top, columns=["SKU", "Monto"]))
        subs = sorted(lead.get("subrubros_comprados") or [])
        if subs:
            st.write("**Subgrupos que compra:** " + ", ".join(subs))
        h = df_fc[(df_fc["documento"].astype(str) == lead["documento"]) & (df_fc["tipo"] == "FAC")]
        if not h.empty:
            st.dataframe(h[["fecha", "sku", "producto", "unidades", "monto"]]
                         .sort_values("fecha", ascending=False).head(30),
                         use_container_width=True, hide_index=True)

    with st.expander("📇 Gestiones anteriores"):
        g = df_act[df_act["documento"].astype(str) == lead["documento"]]
        if g.empty:
            st.caption("Sin gestiones registradas.")
        else:
            st.dataframe(g[["timestamp", "canal", "resultado", "nota", "proximo_seguimiento"]]
                         .sort_values("timestamp", ascending=False),
                         use_container_width=True, hide_index=True)

    _registrar_gestion(lead)
    _cargar_pedido(lead, df_prod, sess)


def _registrar_gestion(lead):
    st.markdown("#### 📝 Registrar gestión")
    if not _gs:
        st.caption("Configurá `[gsheets_televentas]` para registrar gestiones.")
        return
    with st.form(f"gestion_{lead['documento']}"):
        g1, g2 = st.columns(2)
        canal = g1.selectbox("Canal", ["Llamada", "WhatsApp"])
        resultado = g2.selectbox("Resultado", televentas_crm.RESULTADOS)
        nota = st.text_area("Nota", placeholder="Qué se habló, qué quedó pendiente…")
        seg = st.date_input("Próximo seguimiento (opcional)", value=None, format="DD/MM/YYYY")
        enviar = st.form_submit_button("Guardar gestión", type="primary")
    if enviar:
        if not st.session_state.get("tv_agente"):
            st.error("Poné tu nombre de agente en la barra izquierda antes de registrar.")
            return
        try:
            televentas_crm.registrar_actividad(
                dict(_gs),
                {
                    "documento": lead["documento"],
                    "razon_social": lead["razon_social"],
                    "agente": st.session_state.tv_agente,
                    "canal": canal,
                    "resultado": resultado,
                    "nota": nota,
                    "proximo_seguimiento": seg.isoformat() if seg else "",
                },
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
            )
            _cargar_actividad.clear()
            st.success("Gestión registrada ✓")
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"No se pudo registrar: {e}")


def _cargar_pedido(lead, df_prod, sess):
    st.markdown("#### 🧾 Cargar pedido")
    if not lead.get("id_cliente"):
        st.caption("Cliente sin id de Contabilium; no se puede cargar pedido.")
        return
    if not lead.get("id_vendedor"):
        st.warning("El cliente no tiene vendedor asignado en Contabilium — "
                   "asignarlo antes de cargar (afecta comisiones).")

    key = f"tv_pedido_{lead['documento']}"
    st.session_state.setdefault(key, [])

    deps = _cargar_depositos(sess)
    dep_opts = {f"{d.get('Nombre','').strip()}": d.get("Id") for d in deps}
    dep_default = next((n for n in dep_opts if n.strip().upper() == "VENTAS"), None)
    dep_nom = st.selectbox("Depósito", list(dep_opts),
                           index=list(dep_opts).index(dep_default) if dep_default else 0,
                           key=f"dep_{lead['documento']}")

    q = st.text_input("Buscar producto (SKU o nombre)", key=f"q_{lead['documento']}")
    if q:
        m = df_prod[df_prod["sku"].str.contains(q, case=False, na=False)
                    | df_prod["nombre"].str.contains(q, case=False, na=False)].head(20)
        for _, p in m.iterrows():
            cc = st.columns([3, 1, 1, 1])
            cc[0].write(f"**{p['sku']}** — {p['nombre']}")
            cc[1].write(f"stock {int(p['stock'])}")
            cant = cc[2].number_input("cant", min_value=0, value=0, step=1,
                                      key=f"cant_{lead['documento']}_{p['sku']}", label_visibility="collapsed")
            if cc[3].button("➕", key=f"add_{lead['documento']}_{p['sku']}"):
                if cant > 0:
                    st.session_state[key].append({
                        "sku": p["sku"], "nombre": p["nombre"],
                        "cantidad": int(cant),
                        "precioUnitario": round(float(p["precio"]), 2),
                    })
                    st.rerun()

    items = st.session_state[key]
    if items:
        dfp = pd.DataFrame(items)
        dfp["subtotal"] = dfp["cantidad"] * dfp["precioUnitario"]
        st.table(dfp[["sku", "nombre", "cantidad", "precioUnitario", "subtotal"]])
        total = float(dfp["subtotal"].sum())
        st.metric("Total del pedido (neto)", _fmt_money(total))
        cc = st.columns(2)
        if cc[1].button("🗑️ Vaciar pedido", key=f"clr_{lead['documento']}"):
            st.session_state[key] = []
            st.rerun()
        confirm = cc[0].text_input("Escribí CONFIRMAR para cargar el pedido",
                                   key=f"cf_{lead['documento']}")
        if st.button("🚀 Cargar pedido en Contabilium", type="primary",
                     disabled=(confirm.strip() != "CONFIRMAR"),
                     key=f"go_{lead['documento']}"):
            s2, mapa = pedidos_orden.cargar_mapa_conceptos(sess)
            body_items = []
            faltan = []
            for it in items:
                c = mapa.get(pedidos_orden._norm_sku(it["sku"]))
                if not c:
                    faltan.append(it["sku"]); continue
                body_items.append({
                    "idConcepto": str(c["id"]),
                    "cantidad": it["cantidad"],
                    "precioUnitario": it["precioUnitario"],
                    "bonificacion": 0.0,
                })
            if faltan:
                st.error(f"SKUs sin match en Contabilium: {faltan}")
            else:
                body = {
                    "idCliente": lead["id_cliente"],
                    "fechaEmision": date.today().isoformat(),
                    "observaciones": "Pedido cargado desde Televentas GSU.",
                    "bonificacionGlobal": 0.0,
                    "IDInventario": dep_opts[dep_nom],
                    "IDVendedor": int(lead["id_vendedor"]) if lead.get("id_vendedor") else None,
                    "origen": "GSU-Televentas",
                    "items": body_items,
                }
                try:
                    s2, r = pedidos_orden.crear_orden(sess, body)
                    if r.status_code not in (200, 201):
                        st.error(f"Contabilium devolvió HTTP {r.status_code}: {r.text[:300]}")
                    else:
                        s2, nro = pedidos_orden.obtener_numero_orden(s2, r)
                        st.success(f"Pedido cargado ✓  Orden {nro or '(sin nº)'}")
                        if _gs and st.session_state.get("tv_agente"):
                            televentas_crm.registrar_actividad(
                                dict(_gs),
                                {"documento": lead["documento"], "razon_social": lead["razon_social"],
                                 "agente": st.session_state.tv_agente, "canal": "Llamada",
                                 "resultado": "Pedido cargado", "nota": f"Pedido por {_fmt_money(total)}",
                                 "monto_pedido": total, "nro_orden": nro or ""},
                                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
                            )
                            _cargar_actividad.clear()
                        st.session_state[key] = []
                except Exception as e:  # noqa: BLE001
                    st.error(f"No se pudo cargar el pedido: {e}")


tab_leads, tab_seg, tab_nuevo, tab_tablero = st.tabs(
    ["🎯 Leads", "📅 Seguimientos de hoy", "➕ Cliente nuevo", "📊 Actividad"]
)

# =====================================================================
# TAB 1 — Leads (filtros + tabla + ficha 360 + gestión + pedido)
# =====================================================================
with tab_leads:
    st.subheader("Campañas rápidas")
    camp = st.radio(
        "Elegí una cola de llamado",
        ["Todos", "Recuperar dormidos (>90d)", "Sin compras en la ventana",
         "Activos (profundizar)"],
        horizontal=True,
    )
    seg_map = {
        "Recuperar dormidos (>90d)": ["dormido", "dormido_profundo"],
        "Sin compras en la ventana": ["sin_compras"],
        "Activos (profundizar)": ["activo"],
    }
    with st.expander("🔎 Filtros avanzados"):
        c1, c2, c3 = st.columns(3)
        f_dep = c1.multiselect("Departamento", sorted(leads["departamento"].loc[leads["departamento"].str.len() > 0].unique()))
        f_vend = c2.multiselect("Vendedor asignado", sorted(leads["vendedor"].loc[leads["vendedor"].str.len() > 0].unique()))
        subrubros = sorted({s for st_ in leads["subrubros_comprados"] for s in st_})
        f_compro = c3.selectbox("Compró subgrupo", ["(cualquiera)"] + subrubros)
        f_nocompro = c1.selectbox("NO compró subgrupo", ["(cualquiera)"] + subrubros)
        f_tel = c2.checkbox("Solo con teléfono", value=True)
        f_busq = c3.text_input("Buscar (nombre / RUT / código)")

    filt = televentas_data.filtrar_leads(
        leads,
        segmentos=seg_map.get(camp),
        departamentos=f_dep or None,
        vendedores=f_vend or None,
        con_telefono=f_tel,
        compro_subrubro=None if f_compro == "(cualquiera)" else f_compro,
        no_compro_subrubro=None if f_nocompro == "(cualquiera)" else f_nocompro,
        busqueda=f_busq or None,
    )
    st.caption(f"**{len(filt)}** leads en esta vista.")

    cols_show = ["codigo", "nombre_fantasia", "razon_social", "ciudad",
                 "departamento", "telefono", "segmento", "dias_sin_compra",
                 "ticket_prom", "estado", "proximo_seguimiento"]
    cols_show = [c for c in cols_show if c in filt.columns]
    st.dataframe(filt[cols_show], use_container_width=True, hide_index=True,
                 column_config={
                     "ticket_prom": st.column_config.NumberColumn("Ticket prom", format="$ %.0f"),
                     "dias_sin_compra": st.column_config.NumberColumn("Días s/compra", format="%.0f"),
                 })

    # --- Selección de un lead para la ficha 360 ---
    if filt.empty:
        st.info("No hay leads con estos filtros.")
    else:
        opciones = {
            f"{r['codigo']} · {r['nombre_fantasia'] or r['razon_social']} · {r['ciudad']}": r["documento"]
            for _, r in filt.iterrows()
        }
        sel = st.selectbox("Abrí la ficha de un cliente", list(opciones))
        doc_sel = opciones[sel]
        lead = leads[leads["documento"] == doc_sel].iloc[0]
        _ficha_360(lead, df_fc, df_act, df_prod, sess)


# =====================================================================
# TAB 2 — Seguimientos de hoy
# =====================================================================
with tab_seg:
    st.subheader("📅 Seguimientos programados para hoy")
    hoy_iso = hoy.isoformat()
    due = leads[leads["proximo_seguimiento"].astype(str) <= hoy_iso]
    due = due[due["proximo_seguimiento"].astype(str).str.len() == 10]
    if due.empty:
        st.success("No hay seguimientos pendientes para hoy. 🎉")
    else:
        st.caption(f"{len(due)} lead(s) para rellamar (incluye atrasados).")
        st.dataframe(
            due[["proximo_seguimiento", "codigo", "nombre_fantasia", "telefono",
                 "ultimo_resultado", "ciudad"]].sort_values("proximo_seguimiento"),
            use_container_width=True, hide_index=True)


# =====================================================================
# TAB 3 — Cliente nuevo (ESCRIBE en Contabilium)
# =====================================================================
with tab_nuevo:
    st.subheader("➕ Alta de cliente nuevo")
    st.caption("Se crea en Contabilium. Después tocá «Resincronizar» para verlo en los leads.")
    with st.form("nuevo_cliente"):
        n1, n2 = st.columns(2)
        razon = n1.text_input("Razón social *")
        fantasia = n2.text_input("Nombre de fantasía")
        tipo_doc = n1.selectbox("Tipo doc", ["RUT", "CI"])
        nro_doc = n2.text_input("Nº documento")
        tel = n1.text_input("Teléfono")
        email = n2.text_input("Email")
        depto = n1.text_input("Departamento")
        ciudad = n2.text_input("Ciudad")
        domicilio = st.text_input("Domicilio")
        vend_nom = st.selectbox("Vendedor asignado", ["(ninguno)"] + [v for v in VENDEDORES.values()])
        confirm_c = st.text_input("Escribí CONFIRMAR para crear")
        crear = st.form_submit_button("Crear cliente en Contabilium", type="primary")
    if crear:
        if confirm_c.strip() != "CONFIRMAR":
            st.error("Escribí CONFIRMAR para crear el cliente.")
        elif not razon.strip():
            st.error("La razón social es obligatoria.")
        else:
            id_vend = next((k for k, v in VENDEDORES.items() if v == vend_nom), None)
            try:
                body = televentas_cliente.armar_body_cliente(
                    razon_social=razon, nombre_fantasia=fantasia, tipo_doc=tipo_doc,
                    nro_doc=nro_doc, telefono=tel, email=email, departamento=depto,
                    ciudad=ciudad, domicilio=domicilio, id_vendedor=id_vend,
                )
                _, resp = televentas_cliente.crear_cliente(sess, body)
                st.success(f"Cliente creado ✓ (código {resp.get('Codigo', '—')})")
                _cargar_clientes.clear()
            except Exception as e:  # noqa: BLE001
                st.error(f"No se pudo crear el cliente: {e}")


# =====================================================================
# TAB 4 — Tablero de actividad
# =====================================================================
with tab_tablero:
    st.subheader("📊 Actividad de televentas")
    if df_act.empty:
        st.info("Todavía no hay gestiones registradas.")
    else:
        da = df_act.copy()
        da["_ts"] = pd.to_datetime(da["timestamp"], errors="coerce")
        da["dia"] = da["_ts"].dt.date
        t = st.columns(4)
        t[0].metric("Gestiones totales", len(da))
        t[1].metric("Contactos efectivos",
                    int(da["resultado"].str.startswith("Contactado").sum()
                        + (da["resultado"] == "Pedido cargado").sum()))
        t[2].metric("Pedidos cargados", int((da["resultado"] == "Pedido cargado").sum()))
        t[3].metric("Monto generado", _fmt_money(da["monto_pedido"].sum()))
        st.markdown("**Gestiones por día**")
        st.bar_chart(da.groupby("dia").size())
        st.markdown("**Por resultado**")
        st.bar_chart(da["resultado"].value_counts())
