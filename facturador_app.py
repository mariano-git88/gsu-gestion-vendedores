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
import io
from calendar import monthrange
from datetime import date

import pandas as pd
import streamlit as st

import api_loader
import facturador
import theme


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

    # 2. Pull de facturas via API en el mismo rango (anti-duplicado).
    session, refs_facturadas = facturador.cargar_facturas_via_api(
        session, fecha_desde_iso, fecha_hasta_iso
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
            except Exception:
                # Si falla el detalle, asumimos facturable y dejamos
                # que el run lo capture como error real.
                tiene_linea_libre = False

        if ya_facturada_via_api:
            bucket = "ya_via_api"
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
# Main — pendientes + selección + emisión
# =====================================================================

st.title("🧾 Facturación Masiva")
st.caption(
    "Emite facturas electrónicas masivamente desde órdenes de venta pendientes. "
    "El run es secuencial, respeta el throttling UY (15 req/10s) y descarta "
    "automáticamente las órdenes ya facturadas vía API."
)

if "last_search" not in st.session_state:
    st.info(
        "Configurá el rango y los parámetros del lote en la sidebar y "
        "presioná **Buscar pendientes**.",
        icon="👈",
    )
    st.stop()


# Pull (cacheado).
fd_iso, fh_iso = st.session_state["last_search"]
with st.spinner(f"Buscando pendientes entre {fd_iso} y {fh_iso}..."):
    try:
        df_pend = _pull_pendientes(fd_iso, fh_iso)
    except Exception as exc:
        st.error(f"Error al pullear pendientes: {exc}")
        st.stop()

# Splitear en 3 buckets.
if df_pend.empty:
    df_facturables = df_pend
    df_ya_api = df_pend
    df_linea_libre = df_pend
else:
    df_facturables = df_pend[df_pend["bucket"] == "facturable"].copy()
    df_ya_api = df_pend[df_pend["bucket"] == "ya_via_api"].copy()
    df_linea_libre = df_pend[df_pend["bucket"] == "linea_libre"].copy()

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
    excluidas = len(df_ya_api) + len(df_linea_libre)
    st.metric("Excluidas", excluidas, help="Ya facturadas vía API + línea libre")


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
            resultados = []
            progreso = st.progress(0.0, text="Iniciando...")
            placeholder = st.empty()

            for i, id_orden in enumerate(ids_seleccionados, start=1):
                fila = df_facturables[df_facturables["id_orden"] == id_orden].iloc[0]
                placeholder.info(
                    f"[{i}/{n_sel}] Emitiendo orden {fila['numero_orden']} — "
                    f"{fila['comprador']} — {_fmt_uyu(fila['total'])}"
                )
                try:
                    session, emision = facturador.facturar_orden(
                        session, int(id_orden),
                        condicion_venta_nombre=condicion_venta_nombre,
                        punto_venta_id=punto_venta_id,
                        inventario_id=inventario_id,
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
                        "error": "",
                    })
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
                        "error": str(exc)[:300],
                    })

                progreso.progress(i / n_sel, text=f"{i}/{n_sel} procesadas")

            placeholder.empty()
            progreso.empty()
            st.session_state["emision_resultados"] = resultados
            st.cache_data.clear()  # invalidar cache de pendientes
            st.success(f"Run completado. {n_sel} órdenes procesadas.")
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
