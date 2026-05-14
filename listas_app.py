"""
listas_app.py — App de Streamlit para validar la lista de precios UY
vigente en Contabilium contra una lista de referencia de Argentina.

Entry point independiente del dashboard principal (app.py), de
Comisiones (comisiones_app.py) y del Facturador (facturador_app.py).
Se deploya en Streamlit Cloud como un cuarto app del mismo repo:
misma codebase, URL distinta, secrets propios. Toda la lógica vive en
`listas.py`; este archivo es solo la UI.

Auth: password adicional `listas_password` en secrets. Reusa los
secrets `contabilium_client_id` / `contabilium_client_secret` ya
configurados para los otros apps.

Flujo:
  1. Login.
  2. Lista UY se sincroniza al entrar (cache 1h, botón para refrescar).
  3. Subir xlsx AR (export Lista_Marketing del sistema interno de
     Suprabond AR).
  4. Ingresar tipos de cambio manualmente (ARS/USD, UYU/USD).
  5. Toggle USD/UYU para la moneda de comparación.
  6. Tabla de cruce por SKU con delta % UY vs AR. Filtros combinables
     por Rubro UY y DescripcionGrupo AR.
  7. Sección "Análisis por ancla": elegir SKU ancla y SKUs a comparar
     contra él; ver ratios en cada lista y desvío de la estructura
     relativa.
"""

from __future__ import annotations

import hmac

import pandas as pd
import streamlit as st

import api_loader
import fx
import gsheets
import listas
import theme


st.set_page_config(
    page_title="Listas de Precios — GSU",
    page_icon="🏷️",
    layout="wide",
    initial_sidebar_state="expanded",
)
theme.apply_theme()


# =====================================================================
# Auth gate
# =====================================================================

def _check_password() -> bool:
    """Login con `listas_password`. Key session: `auth_listas`."""
    if st.session_state.get("auth_listas", False):
        return True

    left, center, right = st.columns([1, 2, 1])
    with center:
        st.markdown(
            "<h1 style='margin-bottom:0.25rem;'>Listas de Precios</h1>",
            unsafe_allow_html=True,
        )
        st.caption(
            "Validá la lista vigente en Contabilium UY contra una lista de "
            "referencia de Argentina. Acceso restringido."
        )
        with st.form("login_listas", clear_on_submit=False):
            pwd = st.text_input(
                "Contraseña",
                type="password",
                autocomplete="current-password",
                placeholder="••••••••",
            )
            submit = st.form_submit_button("Ingresar", use_container_width=True)
        if submit:
            stored = st.secrets.get("listas_password")
            if stored is None:
                st.error(
                    "La contraseña no está configurada en secrets. "
                    "Avisar a Mariano."
                )
                return False
            if hmac.compare_digest(stored, pwd):
                st.session_state.auth_listas = True
                st.rerun()
            else:
                st.error("Contraseña incorrecta.")
    return False


if not _check_password():
    st.stop()


# =====================================================================
# Caches: API session + lista UY
# =====================================================================

@st.cache_resource
def _api_session():
    """Token OAuth cacheado por process. ApiSession dura ~24h."""
    return api_loader.obtener_token(
        st.secrets["contabilium_client_id"],
        st.secrets["contabilium_client_secret"],
    )


@st.cache_data(ttl=3600, show_spinner="Sincronizando lista UY desde Contabilium...")
def _lista_uy_cached() -> pd.DataFrame:
    """Lista UY cacheada 1h. Botón en sidebar para forzar refresh."""
    session = _api_session()
    _, df = listas.load_lista_uy(session)
    return df


def _gsheets_section() -> dict:
    """Sección de secrets para Google Sheets. Reusa la misma config que
    el módulo de comisiones (`[gsheets]`): mismo spreadsheet, hoja
    nueva `equivalencias_uy_ar`."""
    return dict(st.secrets["gsheets"])


@st.cache_data(ttl=300, show_spinner="Cargando equivalencias UY↔AR...")
def _equivalencias_cached() -> pd.DataFrame:
    """Tabla de equivalencias cacheada 5min. Se limpia tras add/delete."""
    return gsheets.read_equivalencias_listas(_gsheets_section())


# =====================================================================
# Sidebar — configuración
# =====================================================================

st.sidebar.header("Configuración")

uploaded = st.sidebar.file_uploader(
    "Lista AR (.xlsx)",
    type=["xlsx"],
    help=(
        "Export Lista_Marketing del sistema interno de Suprabond AR. "
        "Columnas requeridas: Producto_id, Marca_Id, DescripcionGrupo, "
        "Descripcion, ListaPrecio."
    ),
)

st.sidebar.markdown("**Tipos de cambio**")

# Defaults solo si no hay nada en session_state. Cuando el botón
# "Traer online" pisa estos valores, el next-run los lee desde
# session_state vía la key del number_input.
if "fx_ars_usd" not in st.session_state:
    st.session_state["fx_ars_usd"] = 1420.0
if "fx_uyu_usd" not in st.session_state:
    st.session_state["fx_uyu_usd"] = 40.0

if st.sidebar.button("🔄 Traer cotizaciones online", use_container_width=True):
    errores = []
    try:
        blue = fx.obtener_blue_ars_usd()
        st.session_state["fx_ars_usd"] = blue["venta"]
        st.session_state["fx_blue_meta"] = blue
    except fx.FxError as e:
        errores.append(f"Blue: {e}")
    try:
        uyu = fx.obtener_uyu_usd()
        st.session_state["fx_uyu_usd"] = uyu["valor"]
        st.session_state["fx_uyu_meta"] = uyu
    except fx.FxError as e:
        errores.append(f"UYU: {e}")
    if errores:
        for err in errores:
            st.sidebar.error(err)
    else:
        st.rerun()

# Captions con detalle de la última cotización fetcheada.
if "fx_blue_meta" in st.session_state:
    b = st.session_state["fx_blue_meta"]
    st.sidebar.caption(
        f"Blue: compra **{b['compra']:.0f}** / venta **{b['venta']:.0f}** "
        f"· {b['fecha'][:10]} · _{b['fuente']}_"
    )
if "fx_uyu_meta" in st.session_state:
    u = st.session_state["fx_uyu_meta"]
    st.sidebar.caption(
        f"UYU/USD interbancario: **{u['valor']:.2f}** · "
        f"{u['fecha'][:16]} · _{u['fuente']}_"
    )

fx_ars_usd = st.sidebar.number_input(
    "ARS por 1 USD",
    min_value=1.0,
    step=10.0,
    key="fx_ars_usd",
    help=(
        "Cotización ARS/USD a aplicar. Al traer online se rellena con la "
        "**venta del Blue** (es el valor que da menos USD por el mismo "
        "precio ARS, escenario más conservador para evaluar precios UY)."
    ),
)
fx_uyu_usd = st.sidebar.number_input(
    "UYU por 1 USD",
    min_value=1.0,
    step=0.5,
    key="fx_uyu_usd",
    help="Cotización UYU/USD. Al traer online usa open.er-api.com.",
)
moneda_cmp = st.sidebar.radio(
    "Comparar en",
    options=["USD", "UYU"],
    horizontal=True,
)

st.sidebar.markdown("---")
if st.sidebar.button("Refrescar lista UY", use_container_width=True):
    _lista_uy_cached.clear()
    st.rerun()


# =====================================================================
# Cuerpo principal
# =====================================================================

st.title("Listas de precios — UY vs AR")
st.caption(
    "Compará la lista vigente en Contabilium UY (PrecioFinal neto sin IVA "
    "22%) contra la lista mayorista de Suprabond AR (ListaPrecio neto "
    "sin IVA 21%). Conversión a USD/UYU con tipos de cambio manuales."
)

try:
    df_uy = _lista_uy_cached()
except Exception as e:
    st.error(f"No pude traer la lista UY desde Contabilium: {e}")
    st.stop()

if uploaded is None:
    st.info(
        f"Subí el xlsx AR en la barra lateral para arrancar.\n\n"
        f"Lista UY ya cargada: **{len(df_uy)} productos** "
        f"({df_uy['rubro'].nunique()} rubros, {df_uy['sub_rubro'].nunique()} subrubros)."
    )
    st.stop()

try:
    df_ar = listas.parse_xlsx_ar(uploaded)
except Exception as e:
    st.error(f"Error parseando el xlsx AR: {e}")
    st.stop()

try:
    df_equivs = _equivalencias_cached()
except Exception as e:
    st.warning(
        f"No pude leer las equivalencias del Sheet — sigo sin ellas. "
        f"Detalle: {e}"
    )
    df_equivs = pd.DataFrame(columns=gsheets.EQUIVALENCIAS_LISTAS_COLUMNS)

equivs_map = (
    dict(zip(df_equivs["sku_ar"].astype(str), df_equivs["sku_uy"].astype(str)))
    if not df_equivs.empty else {}
)

df_cruzado = listas.cruzar_listas(df_uy, df_ar, equivalencias=equivs_map)
df_cmp = listas.convertir_a_moneda(df_cruzado, fx_ars_usd, fx_uyu_usd, moneda=moneda_cmp)


# --- Cobertura
ambas = int((df_cmp["presencia"] == "ambas").sum())
solo_uy = int((df_cmp["presencia"] == "solo_uy").sum())
solo_ar = int((df_cmp["presencia"] == "solo_ar").sum())
c1, c2, c3 = st.columns(3)
c1.metric("En ambas listas", ambas)
c2.metric("Solo UY", solo_uy)
c3.metric("Solo AR", solo_ar)


# --- Tabla de cruce
st.subheader("Cruce por SKU")

modo = st.radio(
    "Mostrar",
    options=["Solo presentes en ambas listas", "Todo el universo"],
    horizontal=True,
    key="modo_cruce",
)
df_tabla = df_cmp.copy() if modo == "Todo el universo" else df_cmp[df_cmp["presencia"] == "ambas"].copy()

col_a, col_b = st.columns(2)
rubros_disp = sorted(df_tabla["rubro"].dropna().unique())
rubros_sel = col_a.multiselect("Rubro UY", rubros_disp, default=[], key="filtro_rubro")
cats_disp = sorted(df_tabla["categoria_ar"].dropna().unique())
cats_sel = col_b.multiselect("DescripcionGrupo AR", cats_disp, default=[], key="filtro_cat")
if rubros_sel:
    df_tabla = df_tabla[df_tabla["rubro"].isin(rubros_sel)]
if cats_sel:
    df_tabla = df_tabla[df_tabla["categoria_ar"].isin(cats_sel)]

cols_show = [
    "sku", "presencia", "nombre_uy", "rubro", "sub_rubro",
    "categoria_ar", "nombre_ar", "marca",
    "precio_uy_cmp", "precio_ar_cmp", "precio_ar_uyu_equiv", "delta_pct",
]
cols_show = [c for c in cols_show if c in df_tabla.columns]
df_tabla_disp = df_tabla[cols_show].rename(columns={
    "precio_uy_cmp": f"precio UY ({moneda_cmp})",
    "precio_ar_cmp": f"precio AR ({moneda_cmp})",
    "precio_ar_uyu_equiv": "precio AR en UYU",
    "delta_pct": "Δ % (UY vs AR)",
})

st.dataframe(
    df_tabla_disp,
    use_container_width=True,
    hide_index=True,
    column_config={
        f"precio UY ({moneda_cmp})": st.column_config.NumberColumn(format="%.2f"),
        f"precio AR ({moneda_cmp})": st.column_config.NumberColumn(format="%.2f"),
        "precio AR en UYU": st.column_config.NumberColumn(
            format="%.2f",
            help="Precio AR llevado a UYU vía USD (ARS ÷ fx_ars_usd × fx_uyu_usd). "
                 "Lo que debería costar en UYU según la lista AR.",
        ),
        "Δ % (UY vs AR)": st.column_config.NumberColumn(format="%.1f %%"),
    },
)

st.download_button(
    "Descargar tabla (CSV)",
    data=df_tabla_disp.to_csv(index=False).encode("utf-8"),
    file_name=f"listas_uy_vs_ar_{moneda_cmp.lower()}.csv",
    mime="text/csv",
)


# --- Equivalencias SKU UY ↔ SKU AR
st.markdown("---")
st.subheader("Equivalencias SKU UY ↔ SKU AR")
st.caption(
    "Cuando un mismo producto tiene códigos distintos en cada lista, "
    "podés vincularlos manualmente acá. Las equivalencias se guardan en "
    "Google Sheets (hoja `equivalencias_uy_ar`) y se aplican en todos "
    "los cruces posteriores."
)

with st.expander(f"Equivalencias actuales ({len(df_equivs)})", expanded=False):
    if df_equivs.empty:
        st.caption("Todavía no hay equivalencias cargadas.")
    else:
        for i, row in df_equivs.reset_index(drop=True).iterrows():
            c1, c2, c3, c4 = st.columns([3, 3, 4, 1])
            c1.text(f"UY: {row['sku_uy']}")
            c2.text(f"AR: {row['sku_ar']}")
            c3.caption(f"{row.get('fecha', '')} · {row.get('nota', '')}")
            if c4.button("🗑️", key=f"del_eq_{i}", help="Eliminar equivalencia"):
                try:
                    gsheets.delete_equivalencia_lista(
                        _gsheets_section(), row["sku_uy"], row["sku_ar"]
                    )
                    _equivalencias_cached.clear()
                    st.success(f"Borrada: {row['sku_uy']} ↔ {row['sku_ar']}")
                    st.rerun()
                except Exception as e:
                    st.error(f"No pude borrar: {e}")

df_solo_uy = df_cmp[df_cmp["presencia"] == "solo_uy"][["sku", "nombre_uy", "rubro"]].dropna(subset=["sku"])
df_solo_ar = df_cmp[df_cmp["presencia"] == "solo_ar"][["sku", "nombre_ar", "categoria_ar"]].dropna(subset=["sku"])

with st.expander("➕ Agregar equivalencia (manual)", expanded=False):
    if df_solo_uy.empty or df_solo_ar.empty:
        st.caption(
            "No hay SKUs sin matchear en alguna de las listas — todo "
            "cruzó automáticamente."
        )
    else:
        col_uy, col_ar = st.columns(2)
        with col_uy:
            opciones_uy = df_solo_uy.sort_values("sku")["sku"].tolist()
            def _lbl_uy(s):
                fila = df_solo_uy[df_solo_uy["sku"] == s].iloc[0]
                return f"{s} — {fila['nombre_uy']}"
            sku_uy_new = st.selectbox(
                "SKU solo en UY",
                options=opciones_uy,
                format_func=_lbl_uy,
                key="new_eq_uy",
            )
        with col_ar:
            opciones_ar = df_solo_ar.sort_values("sku")["sku"].tolist()
            def _lbl_ar(s):
                fila = df_solo_ar[df_solo_ar["sku"] == s].iloc[0]
                return f"{s} — {fila['nombre_ar']}"
            sku_ar_new = st.selectbox(
                "SKU solo en AR",
                options=opciones_ar,
                format_func=_lbl_ar,
                key="new_eq_ar",
            )
        nota_new = st.text_input("Nota (opcional)", key="new_eq_nota")
        if st.button("Guardar equivalencia", key="save_eq", type="primary"):
            try:
                res = gsheets.add_equivalencia_lista(
                    _gsheets_section(), sku_uy_new, sku_ar_new, nota=nota_new
                )
                _equivalencias_cached.clear()
                if res.get("agregada"):
                    st.success(f"Guardada: {sku_uy_new} ↔ {sku_ar_new}")
                else:
                    st.info(f"Esta equivalencia ya existía.")
                st.rerun()
            except gsheets.GsheetsError as e:
                st.error(str(e))
            except Exception as e:
                st.error(f"Error inesperado: {e}")

with st.expander("🔎 Sugerencias automáticas (fuzzy match)", expanded=False):
    st.caption(
        "Buscamos candidatos AR para cada SKU sólo UY usando un score "
        "combinado: 70% similitud de descripción + 30% similitud de SKU. "
        "Revisá las sugerencias antes de aceptarlas — el score es una guía, "
        "no una verdad absoluta."
    )
    if df_solo_uy.empty or df_solo_ar.empty:
        st.caption("No hay SKUs sueltos en alguna de las listas.")
    else:
        col_t, col_n = st.columns(2)
        threshold = col_t.slider(
            "Score mínimo",
            min_value=0, max_value=100, value=50, step=5,
            key="sug_threshold",
            help="Sugerencias con score combinado menor a este valor no aparecen.",
        )
        top_n_sug = col_n.slider(
            "Sugerencias por SKU UY",
            min_value=1, max_value=5, value=3,
            key="sug_topn",
        )
        if st.button("Calcular sugerencias", key="run_fuzzy"):
            with st.spinner(f"Comparando {len(df_solo_uy)} × {len(df_solo_ar)} pares..."):
                st.session_state["sug_df"] = listas.sugerir_matches(
                    df_solo_uy, df_solo_ar, top_n=top_n_sug, threshold=threshold,
                )

        sug_df = st.session_state.get("sug_df")
        if sug_df is not None and not sug_df.empty:
            st.caption(
                f"**{len(sug_df)} sugerencia(s) encontradas** "
                f"para {sug_df['sku_uy'].nunique()} SKU(s) UY. "
                f"Marcá la columna *Aceptar* en las que quieras guardar."
            )
            sug_edit = sug_df.copy()
            sug_edit.insert(0, "Aceptar", False)
            edited = st.data_editor(
                sug_edit,
                use_container_width=True,
                hide_index=True,
                disabled=[
                    "sku_uy", "nombre_uy", "sku_ar", "nombre_ar",
                    "score_nombre", "score_sku", "score_total", "rank",
                ],
                column_config={
                    "Aceptar": st.column_config.CheckboxColumn(default=False),
                    "score_nombre": st.column_config.NumberColumn(format="%.0f"),
                    "score_sku": st.column_config.NumberColumn(format="%.0f"),
                    "score_total": st.column_config.NumberColumn(format="%.0f"),
                },
                key="sug_editor",
            )
            marcadas = edited[edited["Aceptar"] == True]
            if not marcadas.empty:
                if st.button(
                    f"Guardar {len(marcadas)} equivalencia(s) marcada(s)",
                    type="primary",
                    key="save_sug",
                ):
                    filas_in = [
                        {
                            "sku_uy": r["sku_uy"],
                            "sku_ar": r["sku_ar"],
                            "nota": f"fuzzy match (score {int(r['score_total'])})",
                        }
                        for _, r in marcadas.iterrows()
                    ]
                    try:
                        res = gsheets.bulk_add_equivalencias_listas(
                            _gsheets_section(), filas_in
                        )
                        _equivalencias_cached.clear()
                        st.success(
                            f"Agregadas: {res['agregadas']} · "
                            f"Duplicadas: {res['duplicadas']} · "
                            f"Conflictos: {len(res['conflictos'])}"
                        )
                        if res["conflictos"]:
                            st.dataframe(
                                pd.DataFrame(res["conflictos"]),
                                use_container_width=True,
                                hide_index=True,
                            )
                        st.session_state.pop("sug_df", None)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error guardando: {e}")
        elif sug_df is not None:
            st.info(
                "Ninguna sugerencia supera el threshold. Bajá el score "
                "mínimo o revisá manualmente."
            )

with st.expander("📥 Import masivo desde xlsx", expanded=False):
    st.caption(
        "Subí un xlsx con columnas `sku_uy` y `sku_ar` (la columna `nota` "
        "es opcional). Modo **append**: las equivalencias existentes no "
        "se borran. Las filas que ya existen se saltean y los conflictos "
        "(SKU ya vinculado a otra equivalencia) se reportan."
    )
    uploaded_eq = st.file_uploader(
        "Archivo xlsx",
        type=["xlsx"],
        key="upload_eq_bulk",
    )
    if uploaded_eq is not None:
        try:
            df_import = listas.parse_xlsx_equivalencias(uploaded_eq)
        except ValueError as e:
            st.error(str(e))
            df_import = None
        except Exception as e:
            st.error(f"Error leyendo el archivo: {e}")
            df_import = None

        if df_import is not None:
            st.caption(f"**Preview**: {len(df_import)} fila(s) válida(s) en el archivo.")
            st.dataframe(df_import, use_container_width=True, hide_index=True)
            if st.button(
                f"Importar {len(df_import)} equivalencia(s)",
                type="primary",
                key="confirm_bulk",
            ):
                with st.spinner("Guardando en Google Sheets..."):
                    try:
                        res = gsheets.bulk_add_equivalencias_listas(
                            _gsheets_section(),
                            df_import.to_dict("records"),
                        )
                        _equivalencias_cached.clear()
                        st.success(
                            f"Agregadas: {res['agregadas']} · "
                            f"Duplicadas (skip): {res['duplicadas']} · "
                            f"Conflictos: {len(res['conflictos'])}"
                        )
                        if res["conflictos"]:
                            st.markdown("**Conflictos:**")
                            st.dataframe(
                                pd.DataFrame(res["conflictos"]),
                                use_container_width=True,
                                hide_index=True,
                            )
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error guardando: {e}")


# --- Análisis por ancla
st.markdown("---")
st.subheader("Análisis por SKU ancla")
st.caption(
    "Elegí un SKU ancla y otros para comparar. Los ratios muestran cómo "
    "cada SKU se relaciona con el ancla en cada lista. Si la estructura "
    "relativa de precios es la misma, los ratios deberían ser parecidos "
    "en UY y AR — desvíos grandes indican que el precio relativo "
    "uruguayo está descalibrado vs la referencia argentina."
)

df_ambas = df_cmp[df_cmp["presencia"] == "ambas"].copy()
if df_ambas.empty:
    st.warning("No hay SKUs presentes en ambas listas. Revisá que los códigos crucen.")
    st.stop()

col_r, col_c = st.columns(2)
rubros_anc = col_r.multiselect(
    "Filtrar por Rubro UY",
    sorted(df_ambas["rubro"].dropna().unique()),
    key="ancla_rubros",
)
cats_anc = col_c.multiselect(
    "Filtrar por DescripcionGrupo AR",
    sorted(df_ambas["categoria_ar"].dropna().unique()),
    key="ancla_cats",
)

df_universo = df_ambas.copy()
if rubros_anc:
    df_universo = df_universo[df_universo["rubro"].isin(rubros_anc)]
if cats_anc:
    df_universo = df_universo[df_universo["categoria_ar"].isin(cats_anc)]

if df_universo.empty:
    st.warning("No hay SKUs con los filtros actuales.")
    st.stop()

# Solo SKUs con precio positivo en ambas listas pueden ser ancla.
df_universo_ancla = df_universo[
    df_universo["precio_uy_cmp"].notna()
    & df_universo["precio_ar_cmp"].notna()
    & (df_universo["precio_uy_cmp"] > 0)
    & (df_universo["precio_ar_cmp"] > 0)
].copy()

if df_universo_ancla.empty:
    st.warning(
        "Ningún SKU en el universo filtrado tiene precio positivo en ambas "
        "listas. Ajustá los filtros."
    )
    st.stop()

sku_keys = df_universo_ancla["sku"].tolist()


def _label(sku: str) -> str:
    fila = df_universo_ancla[df_universo_ancla["sku"] == sku].iloc[0]
    nombre = fila["nombre_uy"] or fila.get("nombre_ar") or ""
    return f"{sku} — {nombre}"


sku_ancla = st.selectbox(
    "SKU ancla",
    options=sku_keys,
    format_func=_label,
    key="sku_ancla",
)

skus_default = [s for s in sku_keys if s != sku_ancla]
skus_cmp = st.multiselect(
    "SKUs a comparar contra el ancla",
    options=skus_default,
    default=skus_default,
    format_func=_label,
    key="skus_a_comparar",
)

try:
    df_ratios = listas.calcular_ratios_ancla(
        df_universo_ancla, sku_ancla, [sku_ancla] + skus_cmp
    )
except ValueError as e:
    st.error(str(e))
    st.stop()

df_ratios_disp = df_ratios.rename(columns={
    "precio_uyu": "precio (UYU)",
    "precio_uyu_teorico": "precio teórico (UYU)",
    "precio_uy_cmp": f"precio UY ({moneda_cmp})",
    "precio_ar_cmp": f"precio AR ({moneda_cmp})",
    "ratio_uy": "ratio UY",
    "ratio_ar": "ratio AR",
    "delta_ratio": "Δ ratio (UY − AR)",
    "delta_ratio_pct": "Δ ratio % (UY/AR − 1)",
})

st.dataframe(
    df_ratios_disp,
    use_container_width=True,
    hide_index=True,
    column_config={
        "precio (UYU)": st.column_config.NumberColumn(format="%.2f"),
        "precio teórico (UYU)": st.column_config.NumberColumn(format="%.2f"),
        f"precio UY ({moneda_cmp})": st.column_config.NumberColumn(format="%.2f"),
        f"precio AR ({moneda_cmp})": st.column_config.NumberColumn(format="%.2f"),
        "ratio UY": st.column_config.NumberColumn(format="%.3f"),
        "ratio AR": st.column_config.NumberColumn(format="%.3f"),
        "Δ ratio (UY − AR)": st.column_config.NumberColumn(format="%.3f"),
        "Δ ratio % (UY/AR − 1)": st.column_config.NumberColumn(format="%.1f %%"),
    },
)

st.download_button(
    "Descargar ratios (CSV)",
    data=df_ratios_disp.to_csv(index=False).encode("utf-8"),
    file_name=f"ratios_ancla_{sku_ancla.replace(' ', '_')}_{moneda_cmp.lower()}.csv",
    mime="text/csv",
)
