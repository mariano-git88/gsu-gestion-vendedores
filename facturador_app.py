"""
facturador_app.py — App de Streamlit para Facturación Masiva desde órdenes
de venta de Contabilium UY.

Entry point separado del dashboard principal (`app.py`) y del módulo de
Comisiones (`comisiones_app.py`). Se deploya en Streamlit Cloud como un
tercer app del mismo repo: misma codebase, URL distinta, secrets propios.
Reutiliza `api_loader.py`, `theme.py`, y delega TODA la lógica de API a
`facturador.py`.

Excepción explícita en `claude.md.txt`: este es el ÚNICO entry point
autorizado a llamar endpoints de escritura (`POST /api/comprobantes/crear`,
`GET /api/comprobantes/emitirFE`, `DELETE /api/comprobantes/?id=`).

Auth: 1 password adicional (`facturador_password` en secrets).

Flujo:
  1. Login con password.
  2. Cargar combos (condiciones venta, puntos venta, inventarios).
  3. Selector de rango de fechas + condición venta + punto venta + inventario.
  4. Botón "Buscar pendientes" → pull órdenes + facturas via API + cruza
     RefExterna → tabla de pendientes facturables (Estado=Pendiente,
     IDComprobante=0, no en set de RefExterna).
  5. Checkbox por orden + resumen "Vas a facturar N órdenes por $X UYU".
  6. Gate explícito: tipear `FACTURAR`.
  7. Run estrictamente secuencial respetando throttling UY (15 req/10s →
     ≥0.7s entre requests, ya manejado dentro de facturador.py).
  8. Reporte final con tabla resultado + descarga CSV.

Caveats heredados de facturador.py (ver docstring del módulo):
  - El borrador colgado entre `crear` y `emitir_fe` se limpia automáticamente.
  - Las órdenes con línea libre (IdConcepto null) se descartan con error claro.
  - La orden NO queda vinculada al comprobante post-emisión (ver patrón
    anti-doble-facturación con RefExterna).
"""

from __future__ import annotations

import hmac
from datetime import date, datetime, timedelta

import io
import zipfile

import pandas as pd
import streamlit as st
from pypdf import PdfReader, PdfWriter

import api_loader
import facturador
import theme
import tutorial_facturador

try:
    import gsheets  # opcional: solo si está configurado [gsheets_facturacion].
except ImportError:
    gsheets = None  # log a Sheet queda deshabilitado.


# Modal del tutorial. Se abre desde la sidebar.
@st.dialog("Tutorial — Facturación masiva", width="large")
def _tutorial_dialog():
    tutorial_facturador.render()


# =====================================================================
# Page config + theme
# =====================================================================

st.set_page_config(
    page_title="Facturación Masiva — GSU",
    page_icon="🧾",
    layout="wide",
    initial_sidebar_state="expanded",
)
theme.apply_theme()

# Override local de botones del CUERPO PRINCIPAL: naranja ACCENT y compacto.
# Mantiene la sidebar con su tema INK oscuro.
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
    /* Achica el valor de las st.metric para que totales con 6 cifras
       (ej. "$ 999.999 UYU") no se trunquen aún en columnas estrechas. */
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
    """Login con `facturador_password`. Key session: `auth_facturador`."""
    if st.session_state.get("auth_facturador", False):
        return True

    left, center, right = st.columns([1, 2, 1])
    with center:
        st.markdown(
            "<h1 style='margin-bottom:0.25rem;'>Facturación Masiva</h1>",
            unsafe_allow_html=True,
        )
        st.caption(
            "Emisión masiva de facturas electrónicas desde órdenes de venta "
            "de Contabilium. Acceso restringido al Jefe de Ventas y administración."
        )
        with st.form("login_facturador", clear_on_submit=False):
            pwd = st.text_input(
                "Contraseña",
                type="password",
                autocomplete="current-password",
                placeholder="••••••••",
            )
            submit = st.form_submit_button("Ingresar", use_container_width=True)
        if submit:
            stored = st.secrets.get("facturador_password")
            if stored is None:
                st.error(
                    "La contraseña no está configurada en secrets. "
                    "Avisar a Mariano."
                )
                return False
            if hmac.compare_digest(stored, pwd):
                st.session_state.auth_facturador = True
                st.rerun()
            else:
                st.error("Contraseña incorrecta.")
    return False


if not _check_password():
    st.stop()


# =====================================================================
# Caches: API session + combos
# =====================================================================

@st.cache_resource
def _api_session():
    """Token OAuth cacheado por process. ApiSession dura ~24h."""
    return api_loader.obtener_token(
        st.secrets["contabilium_client_id"],
        st.secrets["contabilium_client_secret"],
    )


@st.cache_data(ttl=3600, show_spinner="Cargando combos...")
def _cargar_combos() -> dict:
    """Combos de configuración: condiciones de venta, puntos de venta,
    inventarios. Cacheo 1h — son maestros que rara vez cambian."""
    session = _api_session()
    session, cvs = facturador.cargar_condiciones_venta(session)
    session, pvs = facturador.cargar_puntos_venta(session)
    session, invs = facturador.cargar_inventarios(session)
    return {
        "condiciones_venta": [c for c in cvs if c.get("Activa", True)],
        "puntos_venta": [p for p in pvs if p.get("Activo", True)],
        "inventarios": [i for i in invs if i.get("Activo", True)],
    }


# =====================================================================
# Helpers de período
# =====================================================================

def _rango_default() -> tuple[date, date]:
    """Default conservador: día 1 del mes anterior → hoy. Cubre el cierre
    típico de facturación mensual con margen para órdenes atrasadas."""
    hoy = date.today()
    if hoy.month == 1:
        ini = date(hoy.year - 1, 12, 1)
    else:
        ini = date(hoy.year, hoy.month - 1, 1)
    return ini, hoy


def _fmt_uyu(v: float) -> str:
    return f"$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# =====================================================================
# Pull de órdenes pendientes (con anti-doble-facturación via RefExterna)
# =====================================================================

@st.cache_data(
    ttl=300,  # 5 minutos — fresh enough para flujo de facturación.
    show_spinner=False,
)
def _pull_pendientes(fecha_desde_iso: str, fecha_hasta_iso: str) -> pd.DataFrame:
    """Devuelve DataFrame de órdenes pendientes con clasificación en
    3 buckets:

      1. **facturable**: Estado=Pendiente, IDComprobante=0, RefExterna NO
         presente en facturas via API, todos los items con IdConcepto
         válido del catálogo. → aparece en la tabla seleccionable.
      2. **ya_via_api**: misma condición pero con RefExterna en el set
         de facturas via API. → expander "Ya facturadas vía API".
      3. **linea_libre**: misma condición pero algún item con IdConcepto
         null. → expander "No facturables: línea libre". Esas órdenes
         tienen que facturarse desde la UI Web manualmente o convertirse
         a items del catálogo antes de procesar via API.

    Costo: 1 paginación de órdenes (search no trae items) + 1 paginación
    de comprobantes + 1 GET por orden pendiente para inspeccionar Items.
    Para 50-150 órdenes pendientes esto agrega 50-150 requests con
    throttling UY (~35-105s). Cacheado 5min.
    """
    session = _api_session()

    # 1. Pull de órdenes en el rango (search NO trae items).
    path_ordenes = (
        f"/api/ordenesventa/search"
        f"?fechaDesde={fecha_desde_iso}&fechaHasta={fecha_hasta_iso}"
    )
    session, ordenes = api_loader.api_paginate(session, path_ordenes)

    # 2. Pull de facturas via API para el anti-duplicado.
    #
    # El rango de facturas va desde el inicio del rango de órdenes HASTA HOY,
    # no hasta `fecha_hasta`. Una orden se factura el mismo día o después,
    # nunca antes: si se buscan las facturas en la misma ventana que las
    # órdenes, una orden del 20/8 facturada el 2/9 no aparece como facturada
    # cuando se consulta agosto.
    #
    # Normalmente eso lo ataja igual el estado de la orden (Finalizada si se
    # facturó desde la web, Cancelada si se facturó por API). Pero
    # `facturar_orden` cancela con best effort: si ese Cancel falla, la orden
    # queda Pendiente con IDComprobante=0 y **lo único que la delata es
    # RefExterna**. Ahí el rango decide entre detectarla o volver a
    # facturarla.
    hoy_iso = date.today().isoformat()
    session, refs_facturadas = facturador.cargar_facturas_via_api(
        session, fecha_desde_iso, max(fecha_hasta_iso, hoy_iso)
    )

    # 3. Filtrar pendientes y traer detalle (con items) de cada una.
    filas = []
    for o in ordenes:
        estado = (o.get("Estado") or "").strip()
        if estado != "Pendiente":
            continue
        if (o.get("IDComprobante") or 0) > 0:
            continue
        id_orden = o.get("ID") or o.get("Id")
        if id_orden is None:
            continue
        ya_facturada_via_api = str(id_orden) in refs_facturadas

        # Inspeccionar items para detectar línea libre. Si la orden
        # ya está marcada via API, no necesitamos el detalle (se va al
        # bucket "ya_via_api" igual).
        tiene_linea_libre = False
        es_asu = False
        if not ya_facturada_via_api:
            try:
                session, detalle = facturador.obtener_orden(session, int(id_orden))
                items = detalle.get("Items") or []
                if not items:
                    tiene_linea_libre = True  # orden vacía, no facturable.
                else:
                    for it in items:
                        if it.get("IdConcepto") in (None, "", 0):
                            tiene_linea_libre = True
                            break
                # Clientes con formato ASU (Grupo Disco): la factura tiene
                # que llevar el número de orden y el GLN en el XML, y esos
                # datos salen del campo Ref. Externa de la ORDEN, que la API
                # no expone. Emitirlas por API las manda sin esos datos y la
                # cadena no las paga.
                es_asu = _cliente_es_asu(str(detalle.get("IDCliente") or ""))
            except Exception:
                # Si falla el detalle, asumimos facturable y dejamos
                # que el run lo capture como error real.
                tiene_linea_libre = False

        if ya_facturada_via_api:
            bucket = "ya_via_api"
        elif es_asu:
            bucket = "formato_asu"
        elif tiene_linea_libre:
            bucket = "linea_libre"
        else:
            bucket = "facturable"

        filas.append({
            "id_orden": int(id_orden),
            "numero_orden": o.get("NumeroOrden", ""),
            "fecha_creacion": o.get("FechaCreacion", ""),
            "comprador": o.get("Comprador", ""),
            "rut": o.get("NroDocumento", ""),
            "vendedor": o.get("Vendedor", ""),
            "moneda": o.get("Moneda") or "",
            "total_str": o.get("Total", "") or "0",
            "total": api_loader.parse_monto_uy(o.get("Total")),
            "bucket": bucket,
            "id_comprobante_existente": refs_facturadas.get(str(id_orden), 0),
        })

    df = pd.DataFrame(filas)
    if df.empty:
        return df
    df = df.sort_values("numero_orden").reset_index(drop=True)
    return df


# =====================================================================
# Sidebar — configuración del lote
# =====================================================================

with st.sidebar:
    st.markdown("### Facturación Masiva")
    st.caption("Sprint C • UY • API REST oficial")

    try:
        combos = _cargar_combos()
    except Exception as exc:
        st.error(f"No pude cargar combos: {exc}")
        st.stop()

    # Rango de fechas
    fd_default, fh_default = _rango_default()
    rango = st.date_input(
        "Rango de búsqueda",
        value=(fd_default, fh_default),
        format="DD/MM/YYYY",
        help="Fechas de creación de las órdenes a procesar.",
    )
    if isinstance(rango, tuple) and len(rango) == 2:
        fecha_desde, fecha_hasta = rango
    else:
        fecha_desde, fecha_hasta = fd_default, fh_default

    st.markdown("---")
    st.markdown("**Configuración del lote**")
    st.caption(
        "Estos parámetros se aplican a TODAS las órdenes seleccionadas. "
        "Si tenés órdenes con condiciones distintas, hacelas en lotes separados."
    )

    # Condición de venta
    cvs_options = {
        f"{c['Nombre']} (ID {c.get('ID') or c.get('Id')})": c.get("Nombre")
        for c in combos["condiciones_venta"]
    }
    cv_default_label = next(
        (k for k, _ in cvs_options.items() if "30 cuenta corriente" in k.lower()),
        next(iter(cvs_options.keys())),
    )
    cv_label = st.selectbox(
        "Condición de venta",
        options=list(cvs_options.keys()),
        index=list(cvs_options.keys()).index(cv_default_label),
    )
    condicion_venta_nombre = cvs_options[cv_label]

    # Punto de venta
    pvs_options = {
        f"{p.get('Nombre', '')} (ID {p.get('Id') or p.get('ID')})":
        int(p.get("Id") or p.get("ID"))
        for p in combos["puntos_venta"]
    }
    pv_label = st.selectbox(
        "Punto de venta",
        options=list(pvs_options.keys()),
        index=0,
    )
    punto_venta_id = pvs_options[pv_label]

    # Inventario
    invs_options = {
        f"{i.get('Nombre', '').strip()} (ID {i.get('Id') or i.get('ID')})":
        int(i.get("Id") or i.get("ID"))
        for i in combos["inventarios"]
    }
    inv_default_label = next(
        (k for k, _ in invs_options.items() if "ventas" in k.lower()),
        next(iter(invs_options.keys())),
    )
    inv_label = st.selectbox(
        "Depósito / Inventario",
        options=list(invs_options.keys()),
        index=list(invs_options.keys()).index(inv_default_label),
    )
    inventario_id = invs_options[inv_label]
    # El stock se consulta por NOMBRE de depósito (getStockBySKU devuelve
    # "VENTAS", "MFLEX"…), y el selector muestra "VENTAS (ID 832)".
    st.session_state["inventario_nombre"] = inv_label.split(" (ID")[0].strip()

    st.markdown("---")
    if st.button("Buscar pendientes", use_container_width=True, type="primary"):
        st.session_state.pop("emision_resultados", None)
        st.session_state.pop("seleccion_ids", None)
        st.cache_data.clear()  # forzar refresh del pull
        st.session_state["last_search"] = (
            fecha_desde.isoformat(),
            fecha_hasta.isoformat(),
        )
        st.rerun()

    if st.session_state.get("auth_facturador"):
        st.markdown("---")
        if st.button("Cerrar sesión", use_container_width=True):
            st.session_state.pop("auth_facturador", None)
            st.rerun()


# =====================================================================
# Listos para facturar — la cola que manda el depósito
# =====================================================================
#
# La app de Picking corre local en la PC del depósito y escribe cada pedido
# que termina de armar en el buzón (`[gsheets_buzon]`, tab `armados`). Acá se
# lee esa cola: reemplaza el papel que hoy los chicos le llevan a Valeria.
#
# El buzón es un log de eventos append-only; el estado de cada orden se deriva
# con `facturador.derivar_estado_armados`.

def _seccion_buzon() -> dict:
    """Config de acceso al buzón.

    Si `[gsheets_buzon]` no trae credenciales propias, usa las de
    `[gsheets_facturacion]`: es el MISMO Service Account, y hacer copiar la
    clave privada de nuevo en los Secrets es pura oportunidad de error —
    una línea cortada al pegar y no anda, sin decir por qué. Así, dar de
    alta el buzón es agregar dos líneas con el `spreadsheet_id`.
    """
    sec = dict(st.secrets["gsheets_buzon"])
    if not sec.get("service_account") and not sec.get("service_account_json_path"):
        for nombre in ("gsheets_facturacion", "gsheets"):
            if nombre not in st.secrets:
                continue
            fuente = dict(st.secrets[nombre])
            if fuente.get("service_account"):
                sec["service_account"] = fuente["service_account"]
                break
            if fuente.get("service_account_json_path"):
                sec["service_account_json_path"] = fuente["service_account_json_path"]
                break
    return sec


@st.cache_data(ttl=60, show_spinner=False)
def _leer_buzon() -> pd.DataFrame:
    """Eventos del buzón de armados. TTL corto: es una cola de trabajo, y el
    depósito va agregando pedidos durante el día.

    Envuelto en `reintentar_lectura` porque las 60 lecturas por minuto de
    Sheets son por Service Account y las comparten TODAS las apps de GSU.
    """
    return gsheets.reintentar_lectura(
        lambda: gsheets.read_armados(_seccion_buzon())
    )


@st.cache_data(ttl=180, show_spinner=False)
def _estado_real_de_las_ordenes(dias_atras: int = 90) -> dict[str, dict]:
    """Estado real en Contabilium de las órdenes recientes, en UNA paginación.

    **Por qué hace falta:** el buzón es un log de lo que hizo el DEPÓSITO, y
    solo se entera de una factura si se emitió desde la pestaña del depósito.
    Lo que se factura desde el lote masivo o desde la web de Contabilium nunca
    escribe el evento `facturado`, así que esos armados quedaban en la cola
    para siempre. Medido el 3/9/2026: de 58 pedidos en la cola, **55 ya
    estaban facturados** (49 Cancelada por el flujo de la API, 6 Finalizada
    con comprobante desde la web) y solo 3 estaban realmente pendientes.

    Preguntarle a Contabilium orden por orden serían ~58 requests con
    throttling; esto es una sola paginación para todas.
    """
    session = _api_session()
    hasta = date.today()
    desde = hasta - timedelta(days=dias_atras)
    session, ordenes = api_loader.api_paginate(
        session,
        f"/api/ordenesventa/search"
        f"?fechaDesde={desde.isoformat()}&fechaHasta={hasta.isoformat()}",
    )
    session, refs = facturador.cargar_facturas_via_api(
        session, desde.isoformat(), hasta.isoformat()
    )
    out: dict[str, dict] = {}
    for o in ordenes:
        idd = str(o.get("ID") or o.get("Id") or "").strip()
        if not idd:
            continue
        out[idd] = {
            "estado": (o.get("Estado") or "").strip(),
            "id_comprobante": o.get("IDComprobante") or 0,
            "facturada_via_api": idd in refs,
            # El detalle (GET /?id=) NO trae el vendedor: solo el listado.
            # Sin esto, Contabilium le pone el vendedor del API key y la
            # factura sale a nombre de "OP".
            "vendedor": str(o.get("Vendedor") or "").strip(),
        }
    return out


def _ya_esta_facturada(estado_real: dict | None) -> bool:
    """¿Esta orden ya tiene factura, mire por donde se mire?

    Los tres caminos por los que una orden puede estar facturada:
      - desde la web de Contabilium  -> Finalizada + IDComprobante
      - desde el lote masivo o el depósito -> RefExterna en un comprobante
        (y la orden queda Cancelada, porque se cancela para liberar la reserva)
      - a mano, cancelando antes     -> Cancelada sin comprobante: ese caso NO
        se puede afirmar desde acá, y por eso Cancelada sola no cuenta.
    """
    if not estado_real:
        return False
    if (estado_real.get("id_comprobante") or 0) > 0:
        return True
    return bool(estado_real.get("facturada_via_api"))


def _vendedor_de_la_orden(id_orden: str) -> str:
    """Email del vendedor de la orden, para que la factura NO salga a nombre
    del usuario del API key ("OP"). Sale del listado de órdenes, que ya se
    pagina entero en `_estado_real_de_las_ordenes`."""
    try:
        return (_estado_real_de_las_ordenes().get(str(id_orden)) or {}).get("vendedor", "")
    except Exception:
        return ""


@st.cache_data(ttl=1800, show_spinner=False)
def _cliente_es_asu(id_cliente: str) -> bool:
    """¿Este cliente tiene el tag ASU en Contabilium?

    Las cadenas del Grupo Disco (Disco, Devoto, Geant) necesitan que la
    factura electrónica lleve el número de orden y el GLN de entrega en el
    XML. Eso lo produce Contabilium con el formato ASU, tomando datos que se
    cargan a mano en el campo **Ref. Externa de la orden de venta** — un campo
    que la API **no expone** (verificado el 4/9/2026: el detalle de la orden
    devuelve solo Comprador, Estado, EstadoWMS, FechaCreacion,
    FechaVencimiento, ID, IDCliente, IDComprobante, IDPack, Integracion,
    NumeroOrden, Observaciones, Total, TotalNeto e Items).

    No podemos leer ese dato, así que no lo podemos trasladar: estas facturas
    hay que emitirlas desde Contabilium. Cacheado 30 min porque la ficha del
    cliente casi no cambia.
    """
    if not id_cliente:
        return False
    session = _api_session()
    session, es = facturador.cliente_tiene_tag(session, id_cliente)
    return es


@st.cache_data(ttl=300, show_spinner=False)
def _comprobante_de_la_orden(id_orden: str) -> dict | None:
    """¿Esta orden ya tiene factura emitida? Cacheado porque cuesta ~13s:
    hay que traer todos los comprobantes del rango y filtrar por RefExterna
    (el server ignora `?refExterna=`). Solo se consulta para las órdenes que
    figuran Canceladas, que son la excepción."""
    session = _api_session()
    session, comp = facturador.comprobante_de_la_orden(session, int(id_orden))
    return comp


@st.cache_data(ttl=300, show_spinner=False)
def _datos_orden(id_orden: str) -> dict:
    """Cliente y total de la orden, que el buzón no trae (el depósito no los
    necesita para armar). Se piden a Contabilium al mostrar la cola."""
    session = _api_session()
    session, orden = facturador.obtener_orden(session, int(id_orden))
    return {
        "comprador": orden.get("Comprador") or "",
        "total": api_loader.parse_monto_uy(orden.get("Total")),
        "estado": (orden.get("Estado") or "").strip(),
        "id_comprobante": orden.get("IDComprobante") or 0,
        "observaciones": orden.get("Observaciones") or "",
        "problemas_iva": facturador.revisar_iva_de_la_orden(orden),
    }


def _registrar_facturado_en_buzon(fila_armado, emision: dict, adenda: str) -> None:
    """Apenda el evento `facturado` al buzón para que la orden salga de la
    cola. Best effort: si falla, la factura ya está emitida y es válida — el
    anti-duplicado de verdad es el `RefExterna` en Contabilium, no esto."""
    try:
        gsheets.append_armados(_seccion_buzon(), [{
            "timestamp": datetime.now().astimezone().isoformat(),
            "fecha_local": date.today().isoformat(),
            "evento": "facturado",
            "id_orden": str(fila_armado["id_orden"]),
            "numero_orden": str(fila_armado["numero_orden"]),
            "fecha_orden": str(fila_armado.get("fecha_orden") or ""),
            "id_cliente": str(fila_armado.get("id_cliente") or ""),
            "bultos": str(fila_armado.get("bultos") or ""),
            "lineas": str(fila_armado.get("lineas") or ""),
            "unidades": str(fila_armado.get("unidades") or ""),
            "usuario": "facturador",
            "completo": str(fila_armado.get("completo") or ""),
            "verificado": str(fila_armado.get("verificado") or ""),
            "items_json": "",
            "id_comprobante": str(emision.get("id_comprobante") or ""),
            "numero_factura": str(emision.get("numero") or ""),
            "cae": str(emision.get("cae") or ""),
            "observacion": adenda,
        }])
    except Exception as exc:
        st.warning(
            "La factura se emitió bien, pero no pude anotar en el buzón que "
            f"esta orden ya está facturada ({exc}). Va a seguir apareciendo "
            "en la cola: no la factures de nuevo, avisá.",
            icon="⚠",
        )


def _cerrar_en_el_buzon_si_el_deposito_lo_armo(id_orden, numero_orden, emision) -> None:
    """Si esta orden la había armado el depósito, apendar el evento
    `facturado` para que salga de su cola.

    El lote masivo no escribía nada en el buzón, así que todo lo que el
    depósito armaba y se facturaba desde acá quedaba en su cola para siempre
    (58 pedidos el 3/9/2026, de los cuales 55 ya estaban facturados).

    Best effort y silencioso: la factura ya está emitida y es válida, y la
    cola además se cruza contra el estado real de Contabilium.
    """
    try:
        df = _leer_buzon()
        armados = df[
            (df["id_orden"].astype(str) == str(id_orden))
            & (df["evento"] == "armado")
        ]
        if armados.empty:
            return  # no la armó el depósito: no hay nada que cerrar
        _registrar_facturado_en_buzon(armados.iloc[-1], emision, "")
    except Exception:
        pass


def _tabla_pedido_vs_preparado(items: list[dict]) -> pd.DataFrame:
    filas = []
    for it in items:
        pedido = it.get("pedido") or 0
        preparado = it.get("escaneado") or 0
        filas.append({
            "Código": it.get("codigo") or "",
            "Producto": it.get("concepto") or "",
            "Pedido": pedido,
            "Preparado": preparado,
            "Diferencia": preparado - pedido,
            # El depósito elige el motivo de una lista de botones (la definió
            # Gabriel Parodi). Viaja adentro de items_json, no en una columna
            # propia del buzón.
            "Por qué faltó": it.get("motivoTexto") or "",
            # El depósito no lo pudo escanear porque el producto no tiene
            # código de barra en el catálogo. La mercadería salió completa —
            # lo que falta es dar de alta el EAN.
            "Contado a mano": "sí" if it.get("contadoAMano") else "",
        })
    return pd.DataFrame(filas)


def _render_deposito(condicion_venta_nombre, punto_venta_id, inventario_id) -> None:
    st.subheader("Listos para facturar")
    st.caption(
        "Pedidos que el depósito ya preparó y controló. Reemplazan la orden "
        "en papel: cuando aparecen acá, la mercadería ya está armada."
    )

    if gsheets is None or "gsheets_buzon" not in st.secrets:
        st.warning(
            "El buzón del depósito no está configurado en esta instalación "
            "(falta la sección `[gsheets_buzon]` en los Secrets).",
            icon="⚙",
        )
        return

    # Resultado de la última emisión, si venimos de facturar.
    ultima = st.session_state.get("deposito_ultima_emision")
    if ultima:
        st.success(
            f"Factura **{ultima['numero']}** emitida para la orden "
            f"{ultima['numero_orden']} — {ultima['comprador']}. "
            f"CAE {ultima['cae']}.",
            icon="✅",
        )
        if not ultima["orden_cancelada"]:
            st.warning(
                "La factura salió bien, pero no se pudo cancelar la orden de "
                "venta, así que la reserva de stock queda tomada. Detalle: "
                f"{ultima['orden_cancel_error']}",
                icon="⚠",
            )
        col_pdf, col_ok = st.columns([1, 4])
        with col_pdf:
            if ultima["pdf"]:
                st.download_button(
                    "Descargar PDF",
                    data=ultima["pdf"],
                    file_name=f"{ultima['numero']}.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                )
            else:
                st.caption(f"PDF no disponible: {ultima['pdf_error']}")
        with col_ok:
            if st.button("Listo", key="cerrar_ultima_emision"):
                st.session_state.pop("deposito_ultima_emision", None)
                st.rerun()
        st.markdown("---")

    col_a, col_b = st.columns([1, 5], vertical_alignment="center")
    with col_a:
        if st.button("Actualizar", use_container_width=True, key="btn_refresh_buzon"):
            _leer_buzon.clear()
            st.rerun()

    try:
        df_eventos = _leer_buzon()
    except Exception as exc:
        st.error(f"No pude leer el buzón del depósito: {exc}")
        return

    pendientes = facturador.armados_pendientes_de_facturar(df_eventos)

    # El buzón dice qué armó el depósito, NO si ya se facturó: solo aprende de
    # las facturas emitidas desde esta misma pestaña. Todo lo que se factura
    # desde el lote masivo o desde la web de Contabilium quedaba acá para
    # siempre. Así que la cola se cruza contra el estado real.
    ya_facturadas = pd.DataFrame()
    if not pendientes.empty:
        try:
            with st.spinner("Revisando cuáles ya se facturaron..."):
                estados = _estado_real_de_las_ordenes()
        except Exception as exc:
            estados = None
            st.warning(
                f"No pude confirmar contra Contabilium cuáles ya se facturaron "
                f"({exc}). Puede que aparezcan pedidos que ya están facturados.",
                icon="⚠",
            )
        if estados is not None:
            marca = pendientes["id_orden"].astype(str).map(
                lambda i: _ya_esta_facturada(estados.get(i))
            )
            ya_facturadas = pendientes[marca]
            pendientes = pendientes[~marca]

    if not ya_facturadas.empty:
        with st.expander(
            f"{len(ya_facturadas)} pedido(s) que el depósito armó y ya se facturaron"
        ):
            st.caption(
                "Se facturaron desde la pestaña de facturación masiva o desde "
                "Contabilium, así que nunca volvieron al buzón. No hay que "
                "hacer nada con ellos."
            )
            st.dataframe(
                ya_facturadas[["numero_orden", "fecha_local", "usuario"]]
                .rename(columns={
                    "numero_orden": "Orden",
                    "fecha_local": "Armado",
                    "usuario": "Armó",
                }),
                use_container_width=True, hide_index=True,
            )

    if pendientes.empty:
        st.info(
            "No hay pedidos esperando. Cuando el depósito confirme un armado, "
            "aparece acá solo.",
            icon="📭",
        )
        return

    with col_b:
        st.markdown(f"**{len(pendientes)} pedido(s)** esperando factura.")

    for _, fila in pendientes.iterrows():
        id_orden = str(fila["id_orden"])
        completo = str(fila.get("completo") or "").upper() == "SI"

        try:
            datos = _datos_orden(id_orden)
        except Exception as exc:
            st.error(
                f"Orden {fila['numero_orden']}: no pude traerla de Contabilium "
                f"({exc}). Probá de nuevo más tarde."
            )
            continue

        etiqueta = (
            f"Orden {fila['numero_orden']} — {datos['comprador']} — "
            f"{_fmt_uyu(datos['total'])}"
        )
        if not completo:
            etiqueta += "  ·  ⚠ preparado incompleto"

        with st.expander(etiqueta, expanded=len(pendientes) == 1):
            c1, c2, c3 = st.columns(3)
            c1.metric("Bultos", int(fila.get("bultos") or 0))
            c2.metric("Líneas", int(fila.get("lineas") or 0))
            c3.metric("Armado", str(fila.get("fecha_local") or "—"))
            st.caption(
                f"Armado por **{fila.get('usuario') or '—'}**. "
                + (
                    "Verificado contra Contabilium en el momento del armado."
                    if str(fila.get("verificado") or "").upper() == "SI"
                    else "⚠ No se pudo verificar contra Contabilium al armarlo."
                )
            )

            if bool(fila.get("rearmado_post_factura")):
                st.warning(
                    "Esta orden ya se había facturado antes y el depósito "
                    "volvió a armarla. Puede ser un reenvío repetido (normal) "
                    "o un armado nuevo de verdad. Revisala antes de emitir.",
                    icon="⚠",
                )

            items = fila.get("items") or []
            if items:
                st.dataframe(
                    _tabla_pedido_vs_preparado(items),
                    use_container_width=True, hide_index=True,
                )
            else:
                st.caption("El depósito no mandó el detalle de este pedido.")

            # --- Chequeos previos a emitir ---------------------------------
            bloqueos = []
            # El parcial sigue bloqueado ACÁ, y no es por indefinición: el
            # body de la factura se arma con las Cantidad de la ORDEN
            # (facturador.mapear_orden_a_body_crear), y no hay forma de
            # pisarlas con lo que preparó el depósito. Emitir desde este
            # botón facturaría de más, que es justo lo contrario de lo que
            # se quiere. Además el picking expande los combos a sus
            # componentes, así que lo preparado ni siquiera mapea 1 a 1
            # contra las líneas facturables.
            #
            # Lo que sí cambió: el depósito ahora manda POR QUÉ faltó cada
            # línea y quién lo autorizó, así que la corrección a mano se
            # hace con el dato a la vista en vez de a ciegas.
            if not completo:
                autorizado = str(fila.get("observacion") or "").strip()
                bloqueos.append(
                    "El depósito preparó menos de lo pedido. Corregí la orden "
                    "en Contabilium y facturala desde ahí: si se emitiera "
                    "desde acá saldría por las cantidades pedidas, no por las "
                    "despachadas. Arriba está el detalle y por qué faltó cada "
                    "producto."
                    + (f" {autorizado}." if autorizado else "")
                )
            # "Cancelada" NO significa "ya facturada".
            #
            # Una orden queda Cancelada por dos motivos opuestos: porque se
            # emitió la factura (cancelar libera la reserva de stock, lo hace
            # facturar_orden al final) o porque alguien la canceló SIN emitir
            # nada — por ejemplo para destrabar el stock reservado. El estado
            # es el mismo en los dos casos, así que no sirve para decidir.
            #
            # El que sabe de verdad es el comprobante: la orden no guarda el
            # vínculo (IDComprobante se queda en 0 al emitir por API), lo
            # guarda el comprobante en RefExterna.
            cancelada_sin_facturar = False
            if datos["estado"] != "Pendiente":
                if datos["estado"] == "Cancelada":
                    with st.spinner("Revisando si esta orden ya tiene factura..."):
                        comp = _comprobante_de_la_orden(id_orden)
                    if comp:
                        bloqueos.append(
                            f"Esta orden **ya se facturó**: comprobante "
                            f"**{comp.get('Numero')}**"
                            + (f" (CAE {comp.get('Cae')})" if comp.get("Cae") else "")
                            + ". No hay que volver a emitirla."
                        )
                    else:
                        cancelada_sin_facturar = True
                else:
                    bloqueos.append(
                        f"En Contabilium la orden figura como "
                        f"**{datos['estado']}**, no como Pendiente."
                    )
            if (datos["id_comprobante"] or 0) > 0:
                bloqueos.append(
                    "La orden ya tiene un comprobante asociado en Contabilium."
                )
            if _cliente_es_asu(str(fila.get("id_cliente") or "")):
                bloqueos.append(
                    "**Este cliente factura con el formato ASU** (Grupo Disco: "
                    "Disco, Devoto, Geant). Su factura electrónica tiene que "
                    "llevar el **número de orden** y el **GLN de entrega** en "
                    "el XML, y esos datos se cargan a mano en el campo "
                    "**Ref. Externa de la orden de venta**, que la API de "
                    "Contabilium no deja leer. Si se emitiera desde acá, la "
                    "factura saldría sin esos datos y la cadena no la paga.\n\n"
                    "**Esta factura hay que emitirla desde Contabilium.** El "
                    "pedido ya está armado y controlado: el detalle de arriba "
                    "es lo que salió del depósito."
                )

            for problema in datos.get("problemas_iva") or []:
                bloqueos.append(problema)

            if bloqueos:
                for b in bloqueos:
                    st.error(b, icon="🚫")
                continue

            # Cancelada y sin factura: se puede emitir, pero que quede claro
            # que se está facturando una orden cancelada y que eso lo decide
            # una persona, no la app.
            if cancelada_sin_facturar:
                st.warning(
                    "**Esta orden figura como Cancelada en Contabilium y "
                    "todavía no tiene factura.**\n\n"
                    "Suele pasar cuando se la canceló a propósito para liberar "
                    "el stock que ella misma reservaba. En ese caso está bien "
                    "facturarla: la mercadería salió y el cliente la tiene.\n\n"
                    "Pero si el cliente **canceló el pedido de verdad**, no hay "
                    "que emitir nada.",
                    icon="⚠",
                )
                if not st.checkbox(
                    "El pedido se despachó: confirmo que hay que facturarlo",
                    key=f"confirmar_cancelada_{id_orden}",
                ):
                    continue

            # Clientes que exigen sucursal y/o número de orden de compra en
            # la factura. Ese dato no está en ningún sistema: lo tiene quien
            # habló con el cliente, no el depósito.
            exige_adenda = facturador.cliente_exige_adenda(datos["comprador"])

            adenda = st.text_area(
                "Adenda" + (" (obligatoria para este cliente)" if exige_adenda else " (opcional)"),
                key=f"adenda_{id_orden}",
                placeholder="Sucursal, número de orden de compra del cliente…",
                help=(
                    "Se agrega al principio de las observaciones de la factura. "
                    "Es lo que hoy se escribe a mano en la continuación de la "
                    "adenda."
                ),
                height=70,
            )

            if exige_adenda and facturador.es_fin_de_mes():
                st.warning(
                    "**Estamos a fin de mes.** Si esta mercadería se entrega el "
                    "mes que viene, la gran superficie no la recibe con una "
                    "factura de este mes. Verificá la fecha de entrega antes de "
                    "emitir.",
                    icon="📅",
                )

            if exige_adenda and not adenda.strip():
                st.error(
                    f"**{exige_adenda} necesita la sucursal y el número de orden "
                    f"de compra en la factura**, y Contabilium no tiene dónde "
                    f"ponerlos salvo la adenda. Completala arriba para poder "
                    f"emitir. Si no tenés el dato, esta factura la emite quien "
                    f"lo tenga — no se factura desde el depósito.",
                    icon="🚫",
                )
                if str(exige_adenda) == "SODIMAC":
                    st.info(
                        "Acordate que la factura de Sodimac hay que subirla a "
                        "su plataforma **antes** de mandar el envío.",
                        icon="📤",
                    )
                continue

            if st.button(
                "Emitir factura",
                key=f"facturar_{id_orden}",
                type="primary",
            ):
                # El st.stop() NO puede ir adentro del spinner: corta el
                # script sin salir del context manager y el "Emitiendo la
                # factura..." queda girando para siempre arriba del error.
                # Parece que sigue trabajando cuando ya falló.
                emision = None
                error_emision = None
                with st.spinner(f"Emitiendo la factura de la orden {fila['numero_orden']}..."):
                    try:
                        session = _api_session()
                        # El vendedor sale del LISTADO de órdenes: el
                        # detalle no lo trae y el buzón tampoco. Sin esto
                        # Contabilium le asigna el vendedor del API key y la
                        # factura sale a nombre de "OP".
                        session, emision = facturador.facturar_orden(
                            session, int(id_orden),
                            condicion_venta_nombre=condicion_venta_nombre,
                            punto_venta_id=punto_venta_id,
                            inventario_id=inventario_id,
                            adenda=adenda,
                            vendedor_email=_vendedor_de_la_orden(id_orden),
                        )
                    except Exception as exc:
                        error_emision = str(exc)

                if error_emision is not None:
                    st.error(f"No se pudo emitir: {error_emision}")
                    if "stock suficiente" in error_emision.lower():
                        st.caption(
                            "La factura **no se emitió** y no quedó ningún "
                            "borrador colgado: falla al crear el comprobante, "
                            "antes de pedir el CAE."
                        )
                        # Contabilium no dice cuál producto ni cuánto falta:
                        # hay que ir a buscarlo.
                        try:
                            with st.spinner("Buscando cuál producto lo frena..."):
                                sesion_dx = _api_session()
                                sesion_dx, orden_dx = facturador.obtener_orden(
                                    sesion_dx, int(id_orden)
                                )
                                sesion_dx, faltas = facturador.diagnosticar_stock(
                                    sesion_dx, orden_dx,
                                    st.session_state.get("inventario_nombre", "VENTAS"),
                                )
                        except Exception:
                            faltas = []

                        if faltas:
                            st.dataframe(
                                pd.DataFrame([{
                                    "Producto": f["concepto"],
                                    "Pide": f["pedido"],
                                    "Stock": f["stock"],
                                    "Reservado": f["reservado"],
                                    "Libre": f["libre"],
                                    "En otros depósitos": ", ".join(
                                        f"{n}: {c:g}" for n, c in f["otros_depositos"].items()
                                    ) or "—",
                                } for f in faltas]),
                                use_container_width=True, hide_index=True,
                            )
                            if any(f["stock"] >= f["pedido"] for f in faltas):
                                st.info(
                                    "**La mercadería está**, pero figura "
                                    "**reservada**. Contabilium valida contra el "
                                    "stock *libre* (stock − reservado), y **la "
                                    "propia orden reserva lo que pide**: cuando el "
                                    "stock justo alcanza, la reserva de esta misma "
                                    "orden lo deja en cero y rechaza la factura."
                                    "\n\n**Cómo se destraba, en este orden:**\n\n"
                                    "1. Cancelá la orden en Contabilium. Eso "
                                    "libera la reserva.\n"
                                    "2. Volvé acá y tocá **Buscar pendientes** de "
                                    "nuevo.\n"
                                    "3. La orden va a aparecer marcada como "
                                    "*Cancelada*. Tildá la confirmación y emitila "
                                    "normalmente.\n\n"
                                    "El paso 3 existe porque cancelar deja la "
                                    "orden en estado Cancelada: la app te pide "
                                    "confirmar que el pedido igual se despachó, "
                                    "para no facturar algo que el cliente canceló "
                                    "de verdad.",
                                    icon="🔒",
                                )
                    st.stop()

                _registrar_facturado_en_buzon(fila, emision, adenda)

                # Log de auditoría, igual que el run masivo. Best effort.
                try:
                    if "gsheets_facturacion" in st.secrets:
                        gsheets.append_log_facturacion(
                            dict(st.secrets["gsheets_facturacion"]),
                            [{
                                "timestamp": datetime.now().astimezone().isoformat(),
                                "id_orden": id_orden,
                                "numero_orden": fila["numero_orden"],
                                "comprador": datos["comprador"],
                                "total_uyu": datos["total"],
                                "status": "OK",
                                "id_comprobante": emision["id_comprobante"],
                                "numero_factura": emision["numero"],
                                "cae": emision["cae"],
                                "fiscal_url": emision["fiscal_url"],
                                "orden_cancelada": emision.get("orden_cancelada"),
                                "orden_cancel_error": emision.get("orden_cancel_error") or "",
                                "error": "",
                            }],
                        )
                except Exception:
                    pass

                # El PDF es best effort: la factura ya es legal sin él.
                pdf_bytes = None
                pdf_error = ""
                try:
                    session, pdf_bytes = facturador.obtener_pdf(
                        session, int(emision["id_comprobante"])
                    )
                except Exception as exc:
                    pdf_error = str(exc)

                # El resultado se guarda y se pinta arriba de la cola en el
                # rerun siguiente. Si se mostrara acá nomás, desaparecería
                # apenas el usuario toque cualquier cosa — incluido el propio
                # botón de descarga.
                st.session_state["deposito_ultima_emision"] = {
                    "numero_orden": fila["numero_orden"],
                    "comprador": datos["comprador"],
                    "numero": emision["numero"],
                    "cae": emision["cae"],
                    "orden_cancelada": bool(emision.get("orden_cancelada")),
                    "orden_cancel_error": emision.get("orden_cancel_error") or "",
                    "pdf": pdf_bytes,
                    "pdf_error": pdf_error,
                }
                _leer_buzon.clear()
                st.rerun()


# =====================================================================
# Main — pendientes + selección + emisión
# =====================================================================

_col_title, _col_btn = st.columns([5, 1], vertical_alignment="center")
with _col_title:
    st.title("🧾 Facturación Masiva")
    st.caption(
        "Emite facturas electrónicas masivamente desde órdenes de venta pendientes. "
        "El run es secuencial, respeta el throttling UY (15 req/10s) y descarta "
        "automáticamente las órdenes ya facturadas vía API."
    )
with _col_btn:
    if st.button("Tutorial", use_container_width=True, key="btn_tutorial"):
        _tutorial_dialog()

# ---------------------------------------------------------------------
# Selector de sección
#
# Con `st.tabs` el contenido de una tab se derrama a la otra al recargar,
# así que se usa segmented_control + corte explícito del script.
# ---------------------------------------------------------------------
_SEC_MASIVA = "Facturación masiva"
_SEC_DEPOSITO = "Listos para facturar (depósito)"

_seccion = st.segmented_control(
    "Sección",
    options=[_SEC_MASIVA, _SEC_DEPOSITO],
    default=_SEC_MASIVA,
    label_visibility="collapsed",
) or _SEC_MASIVA

if _seccion == _SEC_DEPOSITO:
    _render_deposito(condicion_venta_nombre, punto_venta_id, inventario_id)
    st.stop()


if "last_search" not in st.session_state:
    st.info(
        "Configurá el rango y los parámetros del lote en la sidebar y "
        "presioná **Buscar pendientes**.",
        icon="👈",
    )
    st.stop()


# Pull (cacheado).
fd_iso, fh_iso = st.session_state["last_search"]
# El st.stop() NO puede ir adentro del spinner: corta el script sin salir del
# context manager y "Buscando pendientes..." queda girando para siempre arriba
# del error. Para quien mira, la app sigue buscando cuando ya falló — que es
# exactamente lo que reportó Valeria como "se tranca".
_error_pull = None
with st.spinner(f"Buscando pendientes entre {fd_iso} y {fh_iso}..."):
    try:
        df_pend = _pull_pendientes(fd_iso, fh_iso)
    except Exception as exc:
        _error_pull = str(exc)

if _error_pull is not None:
    st.error(f"Error al pullear pendientes: {_error_pull}")
    st.info(
        "Si el rango de fechas es largo hay que traer todas las órdenes **y** "
        "todos los comprobantes del período, y Contabilium tiene un límite de "
        "pedidos por minuto. Probá con un rango más corto — una o dos semanas.",
        icon="📅",
    )
    st.stop()

# Splitear en 3 buckets.
if df_pend.empty:
    df_facturables = df_pend
    df_ya_api = df_pend
    df_linea_libre = df_pend
    df_asu = df_pend
else:
    df_facturables = df_pend[df_pend["bucket"] == "facturable"].copy()
    df_ya_api = df_pend[df_pend["bucket"] == "ya_via_api"].copy()
    df_linea_libre = df_pend[df_pend["bucket"] == "linea_libre"].copy()
    df_asu = df_pend[df_pend["bucket"] == "formato_asu"].copy()

# ---------------------------------------------------------------------
# Header con métricas
# ---------------------------------------------------------------------
def _fmt_fecha_dmy(iso: str) -> str:
    """YYYY-MM-DD → DD/MM/YYYY."""
    try:
        y, m, d = iso.split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return iso


st.caption(
    f"Rango analizado: **{_fmt_fecha_dmy(fd_iso)} → {_fmt_fecha_dmy(fh_iso)}**"
)
c1, c2, c3 = st.columns(3)
with c1:
    st.metric("Facturables vía API", len(df_facturables))
with c2:
    total_potencial = float(df_facturables["total"].sum()) if not df_facturables.empty else 0.0
    st.metric("Total potencial", f"$ {total_potencial:,.0f} UYU".replace(",", "."))
with c3:
    excluidas = len(df_ya_api) + len(df_linea_libre) + len(df_asu)
    st.metric(
        "Excluidas", excluidas,
        help="Ya facturadas vía API + línea libre + formato ASU (Grupo Disco)",
    )


# ---------------------------------------------------------------------
# Resultado de un run previo (si aplica) — mostrarlo arriba.
# ---------------------------------------------------------------------
if "emision_resultados" in st.session_state:
    st.markdown("---")
    st.subheader("Resultado del último run")
    res = st.session_state["emision_resultados"]
    df_res = pd.DataFrame(res)
    ok = (df_res["status"] == "OK").sum() if not df_res.empty else 0
    fail = (df_res["status"] != "OK").sum() if not df_res.empty else 0
    cc1, cc2, cc3 = st.columns(3)
    with cc1:
        st.metric("Emitidas OK", ok)
    with cc2:
        st.metric("Fallidas", fail)
    with cc3:
        if not df_res.empty:
            csv_bytes = df_res.to_csv(index=False).encode("utf-8")
            st.download_button(
                "Descargar reporte CSV",
                csv_bytes,
                file_name=f"facturacion_{fd_iso}_a_{fh_iso}.csv",
                mime="text/csv",
                use_container_width=True,
            )

    # Dos botones de descarga de PDFs:
    # - ZIP con un PDF por factura (para archivar individualmente).
    # - PDF único combinado con cada factura × 3 (para imprimir todo
    #   de un saque, abriendo en Adobe/Edge y dándole Ctrl+P).
    if st.session_state.get("pdfs_zip_bytes"):
        n_pdfs = st.session_state.get("pdfs_zip_count", 0)
        col_zip, col_combined = st.columns(2)
        with col_zip:
            st.download_button(
                f"📄 ZIP de {n_pdfs} PDFs individuales",
                st.session_state["pdfs_zip_bytes"],
                file_name=f"facturas_{fd_iso}_a_{fh_iso}.zip",
                mime="application/zip",
                use_container_width=True,
                help="Un PDF por factura. Para archivar / mandar por separado.",
            )
        with col_combined:
            if st.session_state.get("pdfs_combined_bytes"):
                st.download_button(
                    f"🖨️ PDF único × 3 ({n_pdfs * 3} hojas)",
                    st.session_state["pdfs_combined_bytes"],
                    file_name=f"facturas_imprimir_{fd_iso}_a_{fh_iso}.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                    help="Todas las facturas combinadas, cada una repetida 3 veces. Abrilo y dale Ctrl+P para imprimir todo de una.",
                )

    st.dataframe(
        df_res,
        use_container_width=True,
        hide_index=True,
        column_config={
            "error": st.column_config.TextColumn("error", width="large"),
            "fiscal_url": st.column_config.LinkColumn(
                "fiscal_url", width="medium", display_text="Ver en DGI",
            ),
            "id_orden": st.column_config.NumberColumn("id_orden", format="%d"),
            "id_comprobante": st.column_config.NumberColumn("id_comp", format="%d"),
            "total": st.column_config.NumberColumn("total", format="%.2f"),
        },
    )

    # Si hay errores, mostrarlos expandidos para que se lean enteros.
    if fail > 0:
        with st.expander(f"Detalle de {fail} errores", expanded=True):
            for _, row in df_res[df_res["status"] != "OK"].iterrows():
                st.markdown(
                    f"**Orden {row['numero_orden']}** ({row['comprador']}, "
                    f"{_fmt_uyu(row['total'])} UYU): {row['error']}"
                )


# ---------------------------------------------------------------------
# Tabla de pendientes con selección.
# ---------------------------------------------------------------------
st.markdown("---")
st.subheader("Órdenes pendientes facturables")

if df_facturables.empty:
    st.success(
        "No hay órdenes pendientes en el rango. "
        "Probá con un rango más amplio si esperabas ver alguna."
    )
else:
    # Preparar DataFrame para edición con checkbox.
    df_display = df_facturables.copy()
    df_display.insert(0, "seleccionar", False)
    df_display["total_fmt"] = df_display["total"].apply(_fmt_uyu)
    df_display = df_display[[
        "seleccionar",
        "numero_orden", "fecha_creacion", "comprador", "rut",
        "vendedor", "moneda", "total_fmt", "id_orden",
    ]].rename(columns={
        "numero_orden": "Nº orden",
        "fecha_creacion": "Fecha",
        "comprador": "Cliente",
        "rut": "RUT",
        "vendedor": "Vendedor",
        "moneda": "Moneda",
        "total_fmt": "Total",
        "id_orden": "ID interno",
    })

    edited = st.data_editor(
        df_display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "seleccionar": st.column_config.CheckboxColumn(
                "✓", default=False, width="small",
            ),
            "ID interno": st.column_config.NumberColumn("ID", format="%d", width="small"),
        },
        disabled=[c for c in df_display.columns if c != "seleccionar"],
        key="data_editor_pendientes",
    )

    seleccionadas_mask = edited["seleccionar"]
    ids_seleccionados = edited.loc[seleccionadas_mask, "ID interno"].tolist()
    n_sel = len(ids_seleccionados)
    total_sel = float(
        df_facturables[df_facturables["id_orden"].isin(ids_seleccionados)]["total"].sum()
    )

    st.markdown(
        f"**{n_sel} órdenes seleccionadas** • Total a facturar: "
        f"**{_fmt_uyu(total_sel)} UYU**"
    )

    if n_sel > 0:
        st.markdown("---")
        st.subheader("Confirmar emisión")
        st.warning(
            f"Vas a emitir **{n_sel} facturas electrónicas** con CAE/CFE en DGI. "
            f"Total: **{_fmt_uyu(total_sel)} UYU**.\n\n"
            f"Configuración aplicada a todas:\n"
            f"- Condición de venta: **{condicion_venta_nombre}**\n"
            f"- Punto de venta: **{pv_label}**\n"
            f"- Depósito: **{inv_label}**\n\n"
            "Esta operación es **fiscal e irreversible** salvo nota de crédito. "
            "El run se ejecuta secuencial (≥0.7s entre requests por throttling UY). "
            "Si falla en el medio, las restantes se reportan y los borradores "
            "huérfanos se limpian automáticamente."
        )
        gate = st.text_input(
            'Para continuar, tipeá exactamente: FACTURAR',
            value="",
            placeholder="FACTURAR",
            key="gate_facturar",
        )
        gate_ok = gate.strip() == "FACTURAR"
        emitir_btn = st.button(
            f"Emitir {n_sel} facturas",
            disabled=not gate_ok,
            type="primary",
            use_container_width=True,
        )
        if emitir_btn and gate_ok:
            session = _api_session()

            # RELEER ANTES DE ESCRIBIR.
            #
            # La lista de pendientes está cacheada 5 minutos, y la pantalla
            # puede quedar abierta mucho más. En el medio alguien pudo
            # facturar desde la web de Contabilium o desde la cola del
            # depósito. Emitir contra una lista vieja es emitir una factura
            # duplicada, y una factura emitida no se deshace.
            #
            # Una sola paginación para todo el lote, sin caché.
            with st.spinner("Verificando que ninguna se haya facturado mientras tanto..."):
                try:
                    session, refs_frescas = facturador.cargar_facturas_via_api(
                        session, fd_iso, date.today().isoformat()
                    )
                except Exception as exc:
                    refs_frescas = None
                    st.warning(
                        f"No se pudo revalidar contra Contabilium ({exc}). "
                        f"Se emite igual, con el chequeo de IDComprobante que "
                        f"hace cada orden.",
                        icon="⚠",
                    )

            ya_facturadas = []
            if refs_frescas is not None:
                ya_facturadas = [i for i in ids_seleccionados if str(i) in refs_frescas]
                if ya_facturadas:
                    ids_seleccionados = [
                        i for i in ids_seleccionados if str(i) not in refs_frescas
                    ]
                    detalle = ", ".join(
                        str(df_facturables[df_facturables["id_orden"] == i]["numero_orden"].iloc[0])
                        for i in ya_facturadas
                    )
                    st.warning(
                        f"**{len(ya_facturadas)} orden(es) ya se habían facturado** "
                        f"mientras esta pantalla estaba abierta y se salteron: "
                        f"{detalle}. Se emiten las {len(ids_seleccionados)} restantes.",
                        icon="🔁",
                    )
                    n_sel = len(ids_seleccionados)

            if not ids_seleccionados:
                st.info(
                    "Todas las órdenes que habías seleccionado ya estaban "
                    "facturadas. No se emitió nada."
                )
                st.stop()

            resultados = []
            pdfs_bajados: list[tuple[str, bytes]] = []  # (filename, bytes) por factura emitida
            progreso = st.progress(0.0, text="Iniciando...")
            placeholder = st.empty()

            for i, id_orden in enumerate(ids_seleccionados, start=1):
                fila = df_facturables[df_facturables["id_orden"] == id_orden].iloc[0]
                placeholder.info(
                    f"[{i}/{n_sel}] Emitiendo orden {fila['numero_orden']} — "
                    f"{fila['comprador']} — {_fmt_uyu(fila['total'])}"
                )
                try:
                    # El detalle GET de la orden NO trae el campo Vendedor;
                    # solo lo trae el listado de search. Por eso pasamos
                    # el email obtenido del df_facturables (que viene del
                    # search) a facturar_orden, así se puede mapear a
                    # IDVendedor correctamente.
                    vendedor_email_orden = str(fila.get("vendedor") or "")
                    session, emision = facturador.facturar_orden(
                        session, int(id_orden),
                        condicion_venta_nombre=condicion_venta_nombre,
                        punto_venta_id=punto_venta_id,
                        inventario_id=inventario_id,
                        vendedor_email=vendedor_email_orden,
                    )
                    _cerrar_en_el_buzon_si_el_deposito_lo_armo(
                        id_orden, fila["numero_orden"], emision
                    )
                    resultados.append({
                        "id_orden": id_orden,
                        "numero_orden": fila["numero_orden"],
                        "comprador": fila["comprador"],
                        "total": fila["total"],
                        "status": "OK",
                        "id_comprobante": emision["id_comprobante"],
                        "numero_factura": emision["numero"],
                        "cae": emision["cae"],
                        "fiscal_url": emision["fiscal_url"],
                        "orden_cancelada": bool(emision.get("orden_cancelada")),
                        "orden_cancel_error": emision.get("orden_cancel_error") or "",
                        "error": "",
                    })
                    # Bajar PDF (best-effort: si falla, no rompe el flujo
                    # — la factura legal ya está emitida).
                    try:
                        session, pdf_bytes = facturador.obtener_pdf(
                            session, int(emision["id_comprobante"])
                        )
                        # Filename: "FAC A-00033662 — RAZON SOCIAL.pdf"
                        comprador_clean = "".join(
                            c if c.isalnum() or c in " -_." else "_"
                            for c in str(fila["comprador"])[:40]
                        ).strip()
                        fname = f"{emision['numero']} - {comprador_clean}.pdf"
                        pdfs_bajados.append((fname, pdf_bytes))
                    except Exception:
                        # PDF no se bajó pero la factura es legal igual.
                        pass
                except Exception as exc:
                    resultados.append({
                        "id_orden": id_orden,
                        "numero_orden": fila["numero_orden"],
                        "comprador": fila["comprador"],
                        "total": fila["total"],
                        "status": "ERROR",
                        "id_comprobante": 0,
                        "numero_factura": "",
                        "cae": "",
                        "fiscal_url": "",
                        "orden_cancelada": False,
                        "orden_cancel_error": "",
                        "error": str(exc)[:300],
                    })

                progreso.progress(i / n_sel, text=f"{i}/{n_sel} procesadas")

            placeholder.empty()
            progreso.empty()
            st.session_state["emision_resultados"] = resultados

            # Empaquetar PDFs en ZIP para descarga.
            if pdfs_bajados:
                zip_buf = io.BytesIO()
                with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for fname, pdf_bytes in pdfs_bajados:
                        zf.writestr(fname, pdf_bytes)
                st.session_state["pdfs_zip_bytes"] = zip_buf.getvalue()
                st.session_state["pdfs_zip_count"] = len(pdfs_bajados)

                # PDF único combinado por triplicado: cada factura aparece
                # 3 veces consecutivas. Optimizado para imprimir todo de
                # un saque (Adobe / Edge → Ctrl+P → 1 click). Resuelve el
                # feedback de Valeria: Windows no tiene "imprimir múltiples
                # PDFs" nativo en el Explorer.
                try:
                    writer = PdfWriter()
                    for _, pdf_bytes in pdfs_bajados:
                        reader = PdfReader(io.BytesIO(pdf_bytes))
                        for _ in range(3):  # triplicado
                            for page in reader.pages:
                                writer.add_page(page)
                    combined_buf = io.BytesIO()
                    writer.write(combined_buf)
                    st.session_state["pdfs_combined_bytes"] = combined_buf.getvalue()
                except Exception:
                    # Si falla el merge, no rompemos — el ZIP individual
                    # ya está disponible como fallback.
                    st.session_state.pop("pdfs_combined_bytes", None)
            else:
                st.session_state.pop("pdfs_zip_bytes", None)
                st.session_state.pop("pdfs_zip_count", None)
                st.session_state.pop("pdfs_combined_bytes", None)

            st.cache_data.clear()  # invalidar cache de pendientes

            # Persistir log en Google Sheet (opcional, best-effort).
            # Si falla no rompe el run — la verdad fiscal vive en el
            # comprobante emitido, no en el log auditable. Lee de
            # `[gsheets_facturacion]` (Sheet propio del facturador, NO
            # el de Comisiones — se separan dominios). Ver tutorial
            # dentro del app para el setup paso a paso.
            if gsheets is not None and "gsheets_facturacion" in st.secrets:
                try:
                    ts = datetime.now().isoformat(timespec="seconds")
                    filas_log = [
                        {
                            "timestamp": ts,
                            "id_orden": r["id_orden"],
                            "numero_orden": r["numero_orden"],
                            "comprador": r["comprador"],
                            "total_uyu": r["total"],
                            "status": r["status"],
                            "id_comprobante": r["id_comprobante"],
                            "numero_factura": r["numero_factura"],
                            "cae": r["cae"],
                            "fiscal_url": r["fiscal_url"],
                            "orden_cancelada": r.get("orden_cancelada", False),
                            "orden_cancel_error": r.get("orden_cancel_error", ""),
                            "error": r["error"],
                        }
                        for r in resultados
                    ]
                    n_log = gsheets.append_log_facturacion(
                        dict(st.secrets["gsheets_facturacion"]),
                        filas_log,
                    )
                    st.success(
                        f"Run completado. {n_sel} órdenes procesadas. "
                        f"Log guardado en Google Sheet ({n_log} filas)."
                    )
                except Exception as exc:
                    st.warning(
                        f"Run completado ({n_sel} órdenes), pero NO pude "
                        f"guardar el log en Sheet: {exc}. "
                        "Los resultados quedan en el reporte de abajo y "
                        "se pueden descargar como CSV. Tip: abrir el "
                        "tutorial desde la sidebar para revisar el setup."
                    )
            else:
                st.success(
                    f"Run completado. {n_sel} órdenes procesadas. "
                    "(Log en Sheet deshabilitado — `[gsheets_facturacion]` "
                    "no configurado en secrets. Abrir el tutorial para setup.)"
                )
            st.rerun()


# ---------------------------------------------------------------------
# Excluidas: ya facturadas via API
# ---------------------------------------------------------------------
if not df_ya_api.empty:
    with st.expander(
        f"Ya facturadas vía API: {len(df_ya_api)} órdenes",
        expanded=False,
    ):
        st.caption(
            "Estas órdenes están en estado Pendiente con IDComprobante=0, "
            "pero ya tienen un comprobante emitido vía API con su RefExterna. "
            "El sistema las descarta automáticamente para evitar doble facturación. "
            "(Caveat conocido: Contabilium no actualiza el IDComprobante de la "
            "orden cuando la factura se emite por API REST. Pendiente que el "
            "soporte lo arregle del lado server.)"
        )
        df_exc_display = df_ya_api[[
            "numero_orden", "comprador", "total_str",
            "id_comprobante_existente",
        ]].rename(columns={
            "numero_orden": "Nº orden",
            "comprador": "Cliente",
            "total_str": "Total",
            "id_comprobante_existente": "Comprobante existente",
        })
        st.dataframe(df_exc_display, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------
# Excluidas: línea libre (no facturables vía API)
# ---------------------------------------------------------------------
if not df_asu.empty:
    with st.expander(
        f"🏬 Se facturan desde Contabilium: {len(df_asu)} órdenes con formato ASU"
    ):
        st.caption(
            "Grupo Disco (Disco, Devoto, Geant). Su factura electrónica tiene "
            "que llevar el número de orden y el GLN de entrega en el XML, y "
            "esos datos se cargan a mano en el campo **Ref. Externa de la orden "
            "de venta**, que la API de Contabilium no deja leer. Emitidas desde "
            "acá saldrían sin esos datos y la cadena no las paga."
        )
        st.dataframe(
            df_asu[["numero_orden", "comprador", "total"]].rename(columns={
                "numero_orden": "Orden", "comprador": "Cliente", "total": "Total",
            }),
            use_container_width=True, hide_index=True,
        )

if not df_linea_libre.empty:
    with st.expander(
        f"⚠ No facturables vía API: {len(df_linea_libre)} órdenes con línea libre",
        expanded=False,
    ):
        st.caption(
            "Estas órdenes tienen al menos un item sin `IdConcepto` del "
            "catálogo (línea libre con descripción suelta). La API REST de "
            "Contabilium NO acepta líneas libres en `comprobantes/crear` — "
            "responde HTTP 500 NullReferenceException. Hay que facturarlas "
            "**manualmente desde la UI Web**, o convertir el item suelto a "
            "un concepto del catálogo antes de re-procesar."
        )
        df_ll_display = df_linea_libre[[
            "numero_orden", "fecha_creacion", "comprador", "total_str",
        ]].rename(columns={
            "numero_orden": "Nº orden",
            "fecha_creacion": "Fecha",
            "comprador": "Cliente",
            "total_str": "Total",
        })
        st.dataframe(df_ll_display, use_container_width=True, hide_index=True)
