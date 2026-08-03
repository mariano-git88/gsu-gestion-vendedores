"""
televentas_app.py — CRM de Televentas GSU.

App Streamlit standalone (password `televentas_password`) para la
Vendedora Televentas: trabaja la base de clientes de Contabilium como
lista de leads, con filtros/campañas, ficha 360°, registro de gestiones,
agenda de seguimientos, carga de pedidos y alta de clientes.

Rendimiento: para segmentar (activo/dormido) alcanza con los ENCABEZADOS
de facturación (rápido). El detalle de SKUs por cliente (para el filtro
"compró tal subgrupo") es un enriquecimiento OPCIONAL bajo demanda.
"""

from __future__ import annotations

import hmac
import re
import urllib.parse
from datetime import date, datetime

import pandas as pd
import streamlit as st

import api_loader
import facturador
import gsheets
import pedidos_orden
import televentas_cliente
import televentas_crm
import televentas_data
import theme
from subrubros import SUBRUBROS
from vendedores import VENDEDORES, NOMBRE_VENDEDOR, ID_VENDEDOR_TELEVENTAS

try:
    import tutorial_televentas
except ImportError:
    tutorial_televentas = None


# =====================================================================
# Page config + theme + auth
# =====================================================================

st.set_page_config(page_title="Televentas — GSU", page_icon="📞",
                   layout="wide", initial_sidebar_state="expanded")
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


@st.cache_data(ttl=21600, show_spinner="Cargando clientes…")
def _cargar_clientes(_s) -> pd.DataFrame:
    _, df = televentas_data.cargar_clientes_enriquecidos(_s, vendedores_map=VENDEDORES)
    return df


@st.cache_data(ttl=21600, show_spinner="Cargando historial (una vez por sesión)…")
def _cargar_headers(_s, desde: str, hasta: str) -> list[dict]:
    _, items = televentas_data.cargar_headers_facturacion(_s, desde, hasta)
    return items


@st.cache_data(ttl=21600, show_spinner="Cargando productos (stock VENTAS)…")
def _cargar_productos(_s) -> pd.DataFrame:
    # Stock del depósito VENTAS únicamente (lo que se puede vender/reservar).
    _s2, stock_ventas = api_loader.load_stock_depositos(_s, nombres=("VENTAS",))
    _, df = api_loader.load_productos_api(_s2, subrubros_map=SUBRUBROS,
                                          stock_por_concepto=stock_ventas)
    return df


@st.cache_data(ttl=21600, show_spinner=False)
def _ventas_deposito_id(_s) -> int | None:
    _, deps = facturador.cargar_inventarios(_s)
    for d in deps:
        if str(d.get("Nombre") or "").strip().upper() == "VENTAS":
            return d.get("Id")
    return None


@st.cache_data(ttl=300, show_spinner="Cargando CRM…")
def _cargar_crm(_gs) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Actividad + listas importadas en una sola apertura del Sheet.

    Van juntas por cuota: Sheets da 60 lecturas por minuto al Service
    Account que comparten TODAS las apps GSU, y cada apertura gasta una.
    """
    return televentas_crm.leer_crm(dict(_gs))


def _leer_archivo_subido(uploaded) -> pd.DataFrame:
    """Lee un .csv / .xlsx / .xls subido, todo como texto."""
    nombre = uploaded.name.lower()
    if nombre.endswith(".csv"):
        return pd.read_csv(uploaded, dtype=str)
    return pd.read_excel(uploaded, dtype=str)


@st.cache_data(ttl=21600, show_spinner="Enriqueciendo con detalle de productos (lento)…")
def _enriquecer_skus(_s, desde: str, hasta: str) -> pd.DataFrame:
    """Carga PESADA (detalle línea por línea) para poblar los SKUs/subgrupos
    que compró cada cliente. Solo se corre si la agente lo pide."""
    _, df_fc, _e = api_loader.load_fc_api(_s, desde, hasta, vendedores_map=VENDEDORES)
    return df_fc


# =====================================================================
# Helpers de contacto / formato
# =====================================================================

def _normalizar_celular_uy(telefono: str) -> str | None:
    for parte in re.split(r"[/,;]", str(telefono or "")):
        digs = re.sub(r"\D", "", parte)
        if digs.startswith("09") and len(digs) == 9:
            return "598" + digs[1:]
        if digs.startswith("598") and len(digs) == 12:
            return digs
    return None


def _link_whatsapp(telefono: str, mensaje: str) -> str | None:
    cel = _normalizar_celular_uy(telefono)
    return f"https://wa.me/{cel}?text={urllib.parse.quote(mensaje)}" if cel else None


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
    meses_hist = st.slider("Meses de historial", 3, 12, 6,
                           help="Ventana para segmentar (activo/dormido). Más meses = "
                                "carga inicial más lenta.")
    if st.button("🔄 Resincronizar", use_container_width=True):
        _cargar_clientes.clear(); _cargar_headers.clear()
        _cargar_productos.clear(); _cargar_crm.clear(); _enriquecer_skus.clear()
        st.rerun()


# =====================================================================
# Carga de datos + construcción de leads
# =====================================================================

_gs = st.secrets.get("gsheets_televentas")
if not _gs:
    st.warning("Falta `[gsheets_televentas]` en secrets: el registro de gestiones no funciona.")

sess = _api_session()
hoy = date.today()
m = hoy.month - (meses_hist - 1); y = hoy.year
while m <= 0:
    m += 12; y -= 1
desde = date(y, m, 1)

df_cli = _cargar_clientes(sess)
headers = _cargar_headers(sess, desde.isoformat(), hoy.isoformat())
df_prod = _cargar_productos(sess)

doc_by_id = {int(i): d for i, d in zip(df_cli["id_cliente"], df_cli["documento"])
             if i is not None}
resumen = televentas_data.resumen_compras_rapido(headers, doc_by_id)
leads = televentas_data.construir_leads(df_cli, resumen)

# Enriquecimiento opcional de SKUs/subgrupos (para filtros por producto +
# sugerencias de "próximo mejor producto"). Se guardan a nivel módulo para
# que la ficha 360 pueda usarlos.
df_fc_det = None
sku_sub = dict(zip(df_prod["sku"], df_prod["sub_rubro"])) if not df_prod.empty else {}
if st.session_state.get("tv_enriquecido"):
    try:
        df_fc_det = _enriquecer_skus(sess, desde.isoformat(), hoy.isoformat())
        res_det = televentas_data.resumen_compras(df_fc_det, sku_subrubro_map=sku_sub)
        if not res_det.empty:
            for c in ("skus_comprados", "subrubros_comprados", "top_skus"):
                leads = leads.drop(columns=[c]).merge(
                    res_det[[c]], how="left", left_on="documento", right_index=True)
            for c in ("skus_comprados", "subrubros_comprados"):
                leads[c] = leads[c].apply(lambda v: v if isinstance(v, set) else set())
            leads["top_skus"] = leads["top_skus"].apply(lambda v: v if isinstance(v, list) else [])
    except Exception as e:  # noqa: BLE001
        st.warning(f"No se pudo enriquecer con productos: {e}")

# Estado CRM + listas importadas (una sola apertura del Sheet).
df_act = pd.DataFrame(columns=televentas_crm.ACTIVIDAD_COLS)
df_imp = pd.DataFrame(columns=televentas_crm.IMPORTACIONES_COLS)
if _gs:
    try:
        df_act, df_imp = _cargar_crm(_gs)
    except Exception as e:  # noqa: BLE001
        # El 429 de cuota no es un error de configuración y se cura solo:
        # decirlo así evita mandar a nadie a revisar credenciales al pedo.
        if gsheets.es_rate_limit(e) or isinstance(e, gsheets.RateLimitError):
            st.warning(
                "⏳ **Google Sheets nos frenó por exceso de lecturas por "
                "minuto.** Es transitorio (el cupo lo comparten todas las "
                "apps GSU) y se libera en menos de un minuto. Las gestiones "
                "y las listas no se perdieron: esperá un momento y tocá "
                "«Resincronizar».")
        else:
            st.warning(f"No se pudo leer el CRM: {e}")

estado_crm = televentas_crm.estado_actual_por_lead(df_act)
if not estado_crm.empty:
    # Se cruza por clave normalizada (no por el documento crudo): las filas
    # que el CRM escribió antes del fix de RAW perdieron el cero inicial del
    # RUT, y si no, la gestión de esos clientes no volvería nunca.
    leads["_clave_doc"] = leads["documento"].map(televentas_data.clave_documento)
    leads = (leads.merge(estado_crm, how="left",
                         left_on="_clave_doc", right_index=True)
             .drop(columns=["_clave_doc"]))
for c in ("estado", "ultimo_resultado", "proximo_seguimiento"):
    if c not in leads.columns:
        leads[c] = ""
leads["estado"] = leads["estado"].fillna("Sin gestionar")

# Lo que anotó el vendedor de calle en las listas importadas (comentario y
# marca de problema de pago). Se cruza contra TODAS las listas, no solo la
# que se está trabajando: es info del cliente, no de la lista.
notas_crm = televentas_crm.notas_por_lead(df_imp)
if not notas_crm.empty:
    leads["_clave_doc"] = leads["documento"].map(televentas_data.clave_documento)
    leads = (leads.merge(notas_crm, how="left",
                         left_on="_clave_doc", right_index=True)
             .drop(columns=["_clave_doc"]))
for c in televentas_crm.NOTAS_COLS:
    if c not in leads.columns:
        leads[c] = "" if c != "problema_pago" else False
leads["problema_pago"] = leads["problema_pago"].fillna(False).astype(bool)
for c in ("comentario", "nota_lista", "nota_fecha"):
    leads[c] = leads[c].fillna("").astype(str)


st.title("📞 Televentas GSU")
k = st.columns(5)
k[0].metric("Clientes", len(leads))
k[1].metric("Con teléfono", int((leads["telefono"].str.len() > 0).sum()))
k[2].metric("Dormidos (>90d)", int(leads["segmento"].isin(["dormido", "dormido_profundo"]).sum()))
k[3].metric("Gestionados", int((leads["estado"] != "Sin gestionar").sum()))
k[4].metric("Seguimientos hoy", int((leads["proximo_seguimiento"].astype(str) == hoy.isoformat()).sum()))


# =====================================================================
# Ficha 360° + gestión + pedido (definidas antes de usarse)
# =====================================================================
def _ficha_360(lead):
    st.divider()
    nombre = lead["nombre_fantasia"] or lead["razon_social"]
    st.markdown(f"### 🗂️ {nombre}")
    a, b, c = st.columns(3)
    a.markdown(f"**Razón social:** {lead['razon_social']}")
    a.markdown(f"**RUT/Doc:** {lead['documento']} · **Código:** {lead['codigo']}")
    a.markdown(f"**Vendedor:** {lead['vendedor'] or '—'}")
    b.markdown(f"**📍** {lead['domicilio']}, {lead['ciudad']} ({lead['departamento']})")
    b.markdown(f"**☎️** {lead['telefono'] or '—'}")
    b.markdown(f"**✉️** {lead['email'] or '—'}")
    dias = lead.get("dias_sin_compra")
    c.metric("Días sin comprar", f"{int(dias)}" if pd.notna(dias) else "—")
    c.metric("Ticket promedio", _fmt_money(lead.get("ticket_prom")))

    # Lo que anotó el vendedor de calle. Va PRIMERO, antes que cualquier
    # otra alerta: es lo que puede hacer que no convenga tomarle el pedido.
    if lead.get("problema_pago"):
        origen = str(lead.get("nota_lista") or "").strip()
        detalle = f" (marcado en «{origen}»)" if origen else ""
        st.error(
            f"⚠️ **PROBLEMA DE PAGO**{detalle} — el vendedor de calle lo marcó "
            "como cliente con problemas para pagar. Consultá antes de cerrar "
            "una venta a crédito.")
    if str(lead.get("comentario") or "").strip():
        fecha_nota = str(lead.get("nota_fecha") or "").strip()
        pie = f"  \n_Anotado el {fecha_nota}._" if fecha_nota else ""
        st.info(f"🗒️ **Comentario del vendedor:** {lead['comentario']}{pie}")

    # Guardrail: no pisar al vendedor de calle si compró hace muy poco.
    if lead.get("atendido_reciente"):
        st.warning(
            f"🚧 Compró hace {int(dias)} días — probablemente su vendedor de "
            "calle lo atendió recién. Evitá pisarlo salvo que sea a pedido suyo.")
    # Alerta de deuda.
    deuda = float(lead.get("deuda_total") or 0.0)
    vencida = float(lead.get("deuda_vencida") or 0.0)
    if deuda > 0.5:
        txt = f"💰 Deuda pendiente: **{deuda:,.2f} UYU**"
        if vencida > 0.5:
            txt += f" · de la cual **{vencida:,.2f} UYU vencida**"
        st.error(txt)
    if str(lead.get("observaciones") or "").strip():
        st.info(f"📝 Nota de entrega: {lead['observaciones']}")

    # Próximo mejor producto (requiere el detalle enriquecido).
    if df_fc_det is not None:
        sug = televentas_data.sugerencias_producto(
            lead["documento"], df_fc_det, sku_sub)
        nombres = dict(zip(df_prod["sku"], df_prod["nombre"])) if not df_prod.empty else {}
        col_r, col_c = st.columns(2)
        with col_r:
            st.markdown("**🔁 Volvé a ofrecerle** (dejó de comprar)")
            if sug["recompra"]:
                for sk, _m in sug["recompra"]:
                    st.write(f"• {sk} — {nombres.get(sk, '')[:40]}")
            else:
                st.caption("—")
        with col_c:
            st.markdown("**➕ Podés sumarle** (popular en lo suyo)")
            if sug["cross"]:
                for sk, _m in sug["cross"]:
                    st.write(f"• {sk} — {nombres.get(sk, '')[:40]}")
            else:
                st.caption("—")
    else:
        st.caption("💡 Activá «Cargar detalle de productos» (arriba) para ver "
                   "sugerencias de qué ofrecerle.")

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
            st.dataframe(pd.DataFrame(top, columns=["SKU", "Monto"]),
                         hide_index=True, use_container_width=True,
                         column_config={"Monto": st.column_config.NumberColumn("Monto", format="%.2f UYU")})
        subs = sorted(lead.get("subrubros_comprados") or [])
        if subs:
            st.write("**Subgrupos que compra:** " + ", ".join(subs))
        # Comprobantes del cliente (nivel encabezado, rápido)
        idc = lead.get("id_cliente")
        comps = [{"Fecha": api_loader.parse_fecha_iso(h.get("FechaEmision")),
                  "Comprobante": h.get("Numero"),
                  "Tipo": h.get("TipoFc"),
                  "Monto": api_loader.parse_monto_uy(h.get("ImporteTotalNeto"))}
                 for h in headers
                 if h.get("IdCliente") == idc and str(h.get("TipoFc") or "") == "FAC"]
        if comps:
            dfc = pd.DataFrame(comps).sort_values("Fecha", ascending=False)
            st.dataframe(dfc, hide_index=True, use_container_width=True,
                         column_config={
                             "Fecha": st.column_config.DateColumn("Fecha", format="DD/MM/YYYY"),
                             "Monto": st.column_config.NumberColumn("Monto", format="%.2f UYU"),
                         })
        elif not top:
            st.caption("Sin compras en la ventana cargada.")
        if not st.session_state.get("tv_enriquecido"):
            st.caption("💡 Para ver qué productos compra, activá «Cargar detalle de productos» arriba.")

    with st.expander("📇 Gestiones anteriores"):
        g = df_act[df_act["documento"].astype(str) == lead["documento"]]
        if g.empty:
            st.caption("Sin gestiones registradas.")
        else:
            st.dataframe(g[["timestamp", "canal", "resultado", "nota", "proximo_seguimiento"]]
                         .sort_values("timestamp", ascending=False),
                         use_container_width=True, hide_index=True)

    _registrar_gestion(lead)
    _cargar_pedido(lead)


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
            st.error("Poné tu nombre de agente en la barra izquierda.")
            return
        try:
            televentas_crm.registrar_actividad(
                dict(_gs),
                {"documento": lead["documento"], "razon_social": lead["razon_social"],
                 "agente": st.session_state.tv_agente, "canal": canal,
                 "resultado": resultado, "nota": nota,
                 "proximo_seguimiento": seg.isoformat() if seg else ""},
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
            )
            _cargar_crm.clear()
            st.success("Gestión registrada ✓")
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"No se pudo registrar: {e}")


def _cargar_pedido(lead):
    st.markdown("#### 🧾 Cargar pedido (stock de VENTAS)")
    if not lead.get("id_cliente"):
        st.caption("Cliente sin id de Contabilium; no se puede cargar pedido.")
        return
    if not lead.get("id_vendedor"):
        st.warning("El cliente no tiene vendedor asignado en Contabilium (afecta comisiones).")

    key = f"tv_pedido_{lead['documento']}"
    st.session_state.setdefault(key, [])

    # Buscador que va premostrando resultados (solo productos con stock en VENTAS)
    q = st.text_input("Buscar producto (SKU o nombre) y hacé click para agregarlo",
                      key=f"q_{lead['documento']}")
    if q:
        disp = df_prod[df_prod["stock"] > 0]
        m = disp[disp["sku"].str.contains(q, case=False, na=False)
                 | disp["nombre"].str.contains(q, case=False, na=False)].head(12)
        if m.empty:
            st.caption("Sin resultados con stock en VENTAS.")
        for _, p in m.iterrows():
            if st.button(f"➕ {p['sku']} — {p['nombre']}  (stock {int(p['stock'])})",
                         key=f"add_{lead['documento']}_{p['sku']}", use_container_width=True):
                if not any(it["sku"] == p["sku"] for it in st.session_state[key]):
                    st.session_state[key].append({
                        "sku": p["sku"], "nombre": p["nombre"],
                        "cantidad": 1, "precioUnitario": round(float(p["precio"]), 2),
                        "stock": int(p["stock"]),
                    })
                st.rerun()

    items = st.session_state[key]
    if not items:
        st.caption("Buscá productos arriba para armar el pedido.")
        return

    # Tabla editable: acá se definen las CANTIDADES
    dfp = pd.DataFrame(items)
    edited = st.data_editor(
        dfp[["sku", "nombre", "cantidad", "precioUnitario", "stock"]],
        hide_index=True, use_container_width=True, key=f"ed_{lead['documento']}",
        column_config={
            "cantidad": st.column_config.NumberColumn("Cantidad", min_value=0, step=1),
            "precioUnitario": st.column_config.NumberColumn("Precio", format="%.2f UYU"),
            "stock": st.column_config.NumberColumn("Stock VENTAS", disabled=True),
            "sku": st.column_config.TextColumn("SKU", disabled=True),
            "nombre": st.column_config.TextColumn("Producto", disabled=True),
        },
        num_rows="fixed",
    )
    # Persistir cantidades editadas
    for i, it in enumerate(items):
        it["cantidad"] = int(edited.iloc[i]["cantidad"])
    lineas = [it for it in items if it["cantidad"] > 0]
    total = sum(it["cantidad"] * it["precioUnitario"] for it in lineas)
    st.metric("Total del pedido (neto)", f"{total:,.2f} UYU".replace(",", "X").replace(".", ",").replace("X", "."))

    cc = st.columns(2)
    if cc[1].button("🗑️ Vaciar", key=f"clr_{lead['documento']}"):
        st.session_state[key] = []
        st.rerun()
    confirm = cc[0].text_input("Escribí CONFIRMAR para cargar", key=f"cf_{lead['documento']}")
    if st.button("🚀 Cargar pedido en Contabilium", type="primary",
                 disabled=(confirm.strip() != "CONFIRMAR" or not lineas),
                 key=f"go_{lead['documento']}"):
        s2, mapa = pedidos_orden.cargar_mapa_conceptos(sess)
        body_items, faltan = [], []
        for it in lineas:
            c = mapa.get(pedidos_orden._norm_sku(it["sku"]))
            if not c:
                faltan.append(it["sku"]); continue
            body_items.append({"idConcepto": str(c["id"]), "cantidad": it["cantidad"],
                               "precioUnitario": it["precioUnitario"], "bonificacion": 0.0})
        if faltan:
            st.error(f"SKUs sin match en Contabilium: {faltan}")
        else:
            dep_id = _ventas_deposito_id(sess)
            body = {
                "idCliente": lead["id_cliente"], "fechaEmision": date.today().isoformat(),
                "observaciones": "Pedido cargado desde Televentas GSU.",
                "bonificacionGlobal": 0.0, "IDInventario": dep_id,
                "IDVendedor": int(lead["id_vendedor"]) if lead.get("id_vendedor") else None,
                "origen": "GSU-Televentas", "items": body_items,
            }
            try:
                s2, r = pedidos_orden.crear_orden(sess, body)
                if r.status_code not in (200, 201):
                    st.error(f"Contabilium HTTP {r.status_code}: {r.text[:300]}")
                else:
                    s2, nro = pedidos_orden.obtener_numero_orden(s2, r)
                    st.success(f"Pedido cargado ✓  Orden {nro or '(sin nº)'}")
                    if _gs and st.session_state.get("tv_agente"):
                        televentas_crm.registrar_actividad(
                            dict(_gs),
                            {"documento": lead["documento"], "razon_social": lead["razon_social"],
                             "agente": st.session_state.tv_agente, "canal": "Llamada",
                             "resultado": "Pedido cargado", "nota": f"Pedido por {total:,.2f} UYU",
                             "monto_pedido": total, "nro_orden": nro or ""},
                            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"))
                        _cargar_crm.clear()
                    st.session_state[key] = []
            except Exception as e:  # noqa: BLE001
                st.error(f"No se pudo cargar el pedido: {e}")


# Navegación por secciones. Se usa `st.segmented_control` (no `st.tabs`) a
# propósito: st.tabs renderiza el contenido de TODAS las tabs en el DOM y las
# oculta por CSS, y durante cada rerun (esta app dispara muchos por botones y
# tablas con on_select) el contenido oculto se ve apilado — parecía que cada
# sección mostraba las demás. Con un selector + `if`, solo se ejecuta y
# renderiza la sección activa: sin derrames y además más rápido.
_SECCIONES = ["🎯 Leads", "📋 Listas importadas", "📅 Seguimientos de hoy",
              "➕ Cliente nuevo", "📊 Actividad"]
seccion = st.segmented_control(
    "Sección", _SECCIONES, default=_SECCIONES[0],
    key="tv_seccion", label_visibility="collapsed")
if not seccion:                       # single-select permite deseleccionar → default
    seccion = _SECCIONES[0]
st.write("")  # respiro visual antes del contenido de la sección

# =====================================================================
# SECCIÓN 1 — Leads
# =====================================================================
if seccion == _SECCIONES[0]:
    # Trabajar sobre una lista importada (o toda la base)
    nombres_imp = televentas_crm.nombres_importaciones(df_imp)
    lista_sel = st.selectbox(
        "📋 Trabajar con lista importada",
        ["(Toda la base)"] + nombres_imp,
        help="Elegí una lista subida en la pestaña «Listas importadas» para "
             "trabajar solo sobre esos clientes.")
    docs_imp = None
    if lista_sel != "(Toda la base)":
        docs_imp = televentas_crm.documentos_de_importacion(df_imp, lista_sel)
        st.caption(f"Trabajando sobre **{lista_sel}** — {len(docs_imp)} clientes.")

    st.subheader("Campañas rápidas")
    camp = st.radio("Cola de llamado",
                    ["Todos", "Recuperar dormidos (>90d)", "Sin compras en la ventana",
                     "Activos (profundizar)"], horizontal=True)
    seg_map = {"Recuperar dormidos (>90d)": ["dormido", "dormido_profundo"],
               "Sin compras en la ventana": ["sin_compras"],
               "Activos (profundizar)": ["activo"]}

    if not st.session_state.get("tv_enriquecido"):
        if st.button("🛒 Cargar detalle de productos (para filtrar por SKU/subgrupo)"):
            st.session_state.tv_enriquecido = True
            st.rerun()

    with st.expander("🔎 Filtros avanzados"):
        c1, c2, c3 = st.columns(3)
        f_dep = c1.multiselect("Departamento", sorted(leads["departamento"].loc[leads["departamento"].str.len() > 0].unique()))
        f_vend = c2.multiselect("Vendedor asignado", sorted(leads["vendedor"].loc[leads["vendedor"].str.len() > 0].unique()))
        subrubros = sorted({s for st_ in leads["subrubros_comprados"] for s in st_})
        f_compro = c3.selectbox("Compró subgrupo", ["(cualquiera)"] + subrubros)
        f_nocompro = c1.selectbox("NO compró subgrupo", ["(cualquiera)"] + subrubros)
        f_tel = c2.checkbox("Solo con teléfono", value=True)
        f_busq = c3.text_input("Buscar (nombre / RUT / código)")
        f_guard = c1.checkbox("🚧 Ocultar atendidos recién (no pisar al vendedor)", value=False)
        f_deuda = c2.checkbox("💰 Solo con deuda", value=False)

    filt = televentas_data.filtrar_leads(
        leads, segmentos=seg_map.get(camp), departamentos=f_dep or None,
        vendedores=f_vend or None, con_telefono=f_tel,
        compro_subrubro=None if f_compro == "(cualquiera)" else f_compro,
        no_compro_subrubro=None if f_nocompro == "(cualquiera)" else f_nocompro,
        busqueda=f_busq or None, documentos=docs_imp,
        ocultar_atendido_reciente=f_guard, solo_con_deuda=f_deuda)
    st.caption(f"**{len(filt)}** leads. Hacé click en una fila para abrir la ficha 👇")

    # Cuando se trabaja sobre una lista importada, el número de arriba casi
    # nunca coincide con el de la lista, y los filtros que lo explican están
    # escondidos en el expander colapsado ("Solo con teléfono" viene
    # prendido). Sin este desglose parece que la app perdió clientes.
    if docs_imp:
        _en_base = televentas_data.filtrar_leads(leads, documentos=docs_imp)
        _sin_cruce = len(docs_imp) - len(_en_base)
        _sin_tel = int((_en_base["telefono"].str.len() == 0).sum()) if f_tel else 0
        _otros = len(_en_base) - _sin_tel - len(filt)
        _motivos = []
        if _sin_cruce > 0:
            _motivos.append(f"{_sin_cruce} sin cruce con la base")
        if _sin_tel > 0:
            _motivos.append(f"{_sin_tel} sin teléfono")
        if _otros > 0:
            _motivos.append(f"{_otros} por campaña/filtros")
        if _motivos:
            st.caption(
                f"↳ De los **{len(docs_imp)}** de «{lista_sel}» quedan "
                f"{len(filt)}: " + ", ".join(_motivos) + "."
            )

    # Tabla de display: la marca de problema de pago va PRIMERA para que se
    # vea antes de llamar, y el comentario del vendedor al final.
    tabla = filt.copy()
    tabla["alerta"] = tabla["problema_pago"].map(lambda x: "⚠️ PAGO" if x else "")
    n_problema = int(tabla["problema_pago"].sum())
    if n_problema:
        st.caption(
            f"⚠️ **{n_problema}** de estos {len(tabla)} tienen **problema de "
            "pago** marcado por el vendedor de calle — mirá la ficha antes de "
            "tomarle el pedido.")

    cols_show = [c for c in ["alerta", "codigo", "nombre_fantasia", "razon_social",
                             "ciudad", "departamento", "telefono", "segmento",
                             "dias_sin_compra", "ticket_prom", "deuda_total",
                             "estado", "proximo_seguimiento", "comentario"]
                 if c in tabla.columns]
    if filt.empty:
        st.info("No hay leads con estos filtros.")
    else:
        ev = st.dataframe(
            tabla[cols_show], use_container_width=True, hide_index=True,
            on_select="rerun", selection_mode="single-row", key="tabla_leads",
            column_config={
                "alerta": st.column_config.TextColumn("⚠️", width="small"),
                "ticket_prom": st.column_config.NumberColumn("Ticket prom", format="%.2f UYU"),
                "deuda_total": st.column_config.NumberColumn("Deuda", format="%.2f UYU"),
                "dias_sin_compra": st.column_config.NumberColumn("Días s/compra", format="%.0f"),
                "comentario": st.column_config.TextColumn(
                    "Comentario del vendedor", width="medium"),
            })
        sel = ev.selection.rows if ev and ev.selection else []
        if sel:
            doc_sel = filt.iloc[sel[0]]["documento"]
            _ficha_360(leads[leads["documento"] == doc_sel].iloc[0])
        else:
            st.info("👆 Seleccioná una fila para ver la ficha del cliente.")


# =====================================================================
# SECCIÓN — Listas importadas
# =====================================================================
if seccion == _SECCIONES[1]:
    st.subheader("📋 Subir una lista de leads")
    st.caption(
        "Subí un Excel/CSV con los clientes seleccionados (ej. por Ernesto). "
        "La app los cruza con la base por **código** o **documento/RUT**. "
        "Después podés trabajar solo sobre esa lista desde la pestaña Leads.  \n"
        "Si la planilla trae una columna de **Comentario** y otra de "
        "**Problema de pago**, se guardan con la lista y aparecen en la ficha "
        "del cliente. Volver a subir una lista con el mismo nombre es seguro: "
        "no duplica clientes, actualiza lo que sabemos de cada uno.")
    if not _gs:
        st.info("Configurá `[gsheets_televentas]` para guardar listas.")
    else:
        up = st.file_uploader("Archivo (.xlsx / .xls / .csv)", type=["xlsx", "xls", "csv"])
        if up is not None:
            try:
                df_sub = _leer_archivo_subido(up)
                matched, faltan = televentas_data.matchear_seleccion(df_sub, leads)
                st.success(f"{len(matched)} clientes encontrados de {len(df_sub)} filas.")
                if faltan:
                    st.warning(f"{len(faltan)} sin match (revisá código/RUT): {faltan[:15]}"
                               + (" …" if len(faltan) > 15 else ""))
                if not matched.empty:
                    _n_pago = int(matched["problema_pago"].sum())
                    _n_com = int((matched["comentario"].str.len() > 0).sum())
                    if _n_pago or _n_com:
                        st.info(
                            f"La planilla trae **{_n_com}** comentario(s) y "
                            f"**{_n_pago}** cliente(s) marcados con problema "
                            "de pago. Se guardan con la lista.")
                    else:
                        st.caption(
                            "No se detectaron columnas de comentario ni de "
                            "problema de pago en esta planilla.")
                    _prev = matched.copy()
                    _prev["problema_pago"] = _prev["problema_pago"].map(
                        lambda x: "⚠️ PAGO" if x else "")
                    st.dataframe(_prev[["codigo", "nombre_fantasia", "razon_social",
                                        "ciudad", "telefono", "problema_pago",
                                        "comentario"]],
                                 use_container_width=True, hide_index=True,
                                 column_config={
                                     "problema_pago": st.column_config.TextColumn(
                                         "⚠️", width="small"),
                                     "comentario": st.column_config.TextColumn(
                                         "Comentario del vendedor", width="medium"),
                                 })
                    nombre_imp = st.text_input(
                        "Nombre de la lista",
                        placeholder="SELECCIONADOS POR ERNESTO 04 07 26")
                    if st.button("💾 Guardar lista", type="primary",
                                 disabled=not nombre_imp.strip()):
                        filas = [{"documento": r["documento"], "codigo": r["codigo"],
                                  "razon_social": r["razon_social"],
                                  "comentario": r["comentario"],
                                  "problema_pago": r["problema_pago"]}
                                 for _, r in matched.iterrows()]
                        try:
                            n = televentas_crm.guardar_importacion(
                                dict(_gs), nombre_imp, filas,
                                st.session_state.get("tv_agente", ""),
                                datetime.now().strftime("%Y-%m-%d %H:%M"))
                            _cargar_crm.clear()
                            st.success(f"Lista «{nombre_imp}» guardada con {n} clientes. "
                                       "Ya podés elegirla en la pestaña Leads.")
                        except Exception as e:  # noqa: BLE001
                            st.error(f"No se pudo guardar: {e}")
            except Exception as e:  # noqa: BLE001
                st.error(f"No se pudo leer el archivo: {e}")

    st.divider()
    st.markdown("**Listas guardadas**")
    if df_imp.empty:
        st.caption("Todavía no hay listas importadas.")
    else:
        _imp = df_imp.copy()
        _imp["_clave"] = _imp["documento"].map(televentas_data.clave_documento)
        _imp["_pago"] = _imp["problema_pago"].map(televentas_data.es_marca_positiva)
        _imp["_com"] = _imp["comentario"].astype(str).str.strip().str.len() > 0
        # Primero una fila por (lista, cliente): la tab es append-only, así
        # que re-importar duplica filas y contarlas duplicaría los totales.
        _por_cli = (_imp.groupby(["nombre", "_clave"])
                    .agg(_pago=("_pago", "max"), _com=("_com", "max"),
                         fecha=("fecha_carga", "max"))
                    .reset_index())
        resumen_imp = (_por_cli.groupby("nombre")
                       .agg(clientes=("_clave", "nunique"),
                            problema_pago=("_pago", "sum"),
                            comentarios=("_com", "sum"),
                            fecha=("fecha", "max"))
                       .reset_index().sort_values("fecha", ascending=False))
        st.dataframe(resumen_imp, use_container_width=True, hide_index=True,
                     column_config={
                         "problema_pago": st.column_config.NumberColumn("⚠️ Pago"),
                         "comentarios": st.column_config.NumberColumn("🗒️ Coment."),
                     })

    if _gs:
        st.divider()
        with st.expander("🛠 Mantenimiento — reparar RUT en la Sheet"):
            st.caption(
                "Las filas guardadas antes del arreglo perdieron el **cero "
                "inicial** del RUT (Google Sheets las tomó como número), así "
                "que esos clientes no cruzaban contra la base: no aparecían "
                "en las listas y quedaban «Sin gestionar» aunque se los "
                "hubiera llamado. Esto reescribe el RUT completo. Solo toca "
                "la columna del documento y se puede correr las veces que "
                "quieras (la segunda vez repara 0).")
            if st.button("🔍 Revisar (no escribe nada)"):
                with st.spinner("Leyendo la Sheet…"):
                    try:
                        st.session_state.tv_reparacion = televentas_crm.reparar_documentos(
                            dict(_gs), leads["documento"], dry_run=True)
                    except Exception as e:  # noqa: BLE001
                        st.session_state.tv_reparacion = None
                        st.error(f"No se pudo revisar: {e}")

            rep = st.session_state.get("tv_reparacion")
            if rep:
                total = sum(v["reparadas"] for v in rep.values())
                for tab, v in rep.items():
                    st.write(f"· `{tab}`: {v['reparadas']} de {v['filas']} filas a reparar.")
                if total == 0:
                    st.success("No hay nada que reparar. 👌")
                elif st.button(f"🛠 Reparar {total} fila(s)", type="primary"):
                    with st.status("Reparando la Sheet…", expanded=True) as _st_rep:
                        try:
                            st.write("Reescribiendo la columna documento…")
                            hecho = televentas_crm.reparar_documentos(
                                dict(_gs), leads["documento"])
                            _cargar_crm.clear()
                            st.session_state.tv_reparacion = None
                            _st_rep.update(label="Sheet reparada", state="complete",
                                           expanded=False)
                            _res_rep = ("ok", "Reparadas " + ", ".join(
                                f"{v['reparadas']} en `{t}`" for t, v in hecho.items())
                                + ". Volvé a la pestaña Leads para ver los números.")
                        except Exception as e:  # noqa: BLE001
                            _st_rep.update(label="Falló la reparación", state="error",
                                           expanded=False)
                            _res_rep = ("err", f"No se pudo reparar: {e}")
                    if _res_rep[0] == "ok":
                        st.success(_res_rep[1])
                    else:
                        st.error(_res_rep[1])


# =====================================================================
# SECCIÓN 2 — Seguimientos de hoy
# =====================================================================
if seccion == _SECCIONES[2]:
    st.subheader("📅 Seguimientos para hoy (y atrasados)")
    due = leads[(leads["proximo_seguimiento"].astype(str).str.len() == 10)
                & (leads["proximo_seguimiento"].astype(str) <= hoy.isoformat())]
    if due.empty:
        st.success("No hay seguimientos pendientes. 🎉")
    else:
        st.caption(f"{len(due)} lead(s) para rellamar.")
        st.dataframe(due[["proximo_seguimiento", "codigo", "nombre_fantasia", "telefono",
                          "ultimo_resultado", "ciudad"]].sort_values("proximo_seguimiento"),
                     use_container_width=True, hide_index=True)


# =====================================================================
# SECCIÓN 3 — Cliente nuevo
# =====================================================================
if seccion == _SECCIONES[3]:
    st.subheader("➕ Alta de cliente nuevo")
    st.caption("Se crea en Contabilium. Después «Resincronizar» para verlo en los leads.")
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
        # Selector curado: 4 comerciales activos + "Televentas" (→ cuenta OP@).
        opciones_vend = {nom: vid for vid, nom in NOMBRE_VENDEDOR.items()}
        opciones_vend["Televentas"] = ID_VENDEDOR_TELEVENTAS
        vend_nom = st.selectbox("Vendedor asignado", list(opciones_vend))
        confirm_c = st.text_input("Escribí CONFIRMAR para crear")
        crear = st.form_submit_button("Crear cliente en Contabilium", type="primary")
    if crear:
        id_vend = opciones_vend.get(vend_nom)
        if confirm_c.strip() != "CONFIRMAR":
            st.error("Escribí CONFIRMAR para crear.")
        elif not razon.strip():
            st.error("La razón social es obligatoria.")
        elif vend_nom == "Televentas" and id_vend is None:
            st.error("Falta configurar el ID de la cuenta Televentas "
                     "(OP@SUPRABOND.COM.UY) en vendedores.py. Avisá a Mariano.")
        else:
            try:
                body = televentas_cliente.armar_body_cliente(
                    razon_social=razon, nombre_fantasia=fantasia, tipo_doc=tipo_doc,
                    nro_doc=nro_doc, telefono=tel, email=email, departamento=depto,
                    ciudad=ciudad, domicilio=domicilio, id_vendedor=id_vend)
                _, resp = televentas_cliente.crear_cliente(sess, body)
                st.success(f"Cliente creado ✓ (código {resp.get('Codigo', '—')})")
                _cargar_clientes.clear()
            except Exception as e:  # noqa: BLE001
                st.error(f"No se pudo crear el cliente: {e}")


# =====================================================================
# SECCIÓN 4 — Actividad
# =====================================================================
if seccion == _SECCIONES[4]:
    st.subheader("📊 Actividad de televentas")
    if df_act.empty:
        st.info("Todavía no hay gestiones registradas.")
    else:
        da = df_act.copy()
        da["_ts"] = pd.to_datetime(da["timestamp"], errors="coerce")
        da["dia"] = da["_ts"].dt.date
        t = st.columns(4)
        t[0].metric("Gestiones", len(da))
        t[1].metric("Contactos efectivos",
                    int(da["resultado"].str.startswith("Contactado").sum()
                        + (da["resultado"] == "Pedido cargado").sum()))
        t[2].metric("Pedidos cargados", int((da["resultado"] == "Pedido cargado").sum()))
        t[3].metric("Monto generado", _fmt_money(da["monto_pedido"].sum()))
        st.bar_chart(da.groupby("dia").size())
        st.bar_chart(da["resultado"].value_counts())
