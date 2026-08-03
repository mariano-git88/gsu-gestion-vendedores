"""
Tests del cruce por documento en Televentas.

Contexto (bug real, agosto 2026): una lista importada de 335 clientes
mostraba solo 252 leads. Causa: el CRM escribía en Google Sheets con
`value_input_option="USER_ENTERED"`, que hace que Sheets interprete un RUT
todo-dígitos como NÚMERO y le coma el cero de la izquierda —
"012345680017" volvía "12345680017" y no cruzaba contra ningún lead. En la
base de UY eso afecta a 177 de 1.117 clientes (15,8%).

El mismo bug dejaba las gestiones huérfanas: un cliente con RUT que empieza
con 0 quedaba "Sin gestionar" para siempre aunque se lo hubiera llamado.

Estos tests son puros (no tocan red ni Sheets).
"""

import json
import sys
from pathlib import Path

import pandas as pd
import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gsheets  # noqa: E402
import gspread  # noqa: E402
import televentas_crm  # noqa: E402
import televentas_data  # noqa: E402


# =====================================================================
# Rate limit de Google Sheets (429)
# =====================================================================

def _api_error(status_code, texto="Quota exceeded for quota metric 'Read requests'"):
    """APIError real de gspread, armado con la respuesta que manda Google.

    Se construye con un Response de verdad y no con un mock: la forma de
    esta excepción cambió entre versiones de gspread, y un fixture inventado
    haría pasar el test sin probar lo que pasa en producción.
    """
    resp = requests.Response()
    resp.status_code = status_code
    resp._content = json.dumps({
        "error": {"code": status_code, "message": texto, "status": "RESOURCE_EXHAUSTED"}
    }).encode()
    resp.headers["Content-Type"] = "application/json"
    return gspread.exceptions.APIError(resp)


def test_reconoce_el_429_de_cuota():
    assert gsheets.es_rate_limit(_api_error(429))
    assert not gsheets.es_rate_limit(_api_error(403, "The caller does not have permission"))
    assert not gsheets.es_rate_limit(ValueError("cualquier cosa"))


def test_lectura_reintenta_y_termina_saliendo_bien():
    intentos = []
    dormidas = []

    def lectura():
        intentos.append(1)
        if len(intentos) < 3:
            raise _api_error(429)
        return "datos"

    out = gsheets.reintentar_lectura(
        lectura, esperas=(1, 2, 3), dormir=dormidas.append)

    assert out == "datos"
    assert len(intentos) == 3
    assert dormidas == [1, 2]      # esperó entre intentos, no después del ok


def test_agotados_los_reintentos_avisa_que_es_transitorio():
    dormidas = []

    def lectura():
        raise _api_error(429)

    with pytest.raises(gsheets.RateLimitError) as exc:
        gsheets.reintentar_lectura(lectura, esperas=(1, 2), dormir=dormidas.append)

    assert "transitorio" in str(exc.value).lower()
    assert dormidas == [1, 2]


def test_un_error_que_no_es_cuota_no_se_reintenta():
    """Un 403 de permisos no mejora esperando: tiene que fallar al toque."""
    intentos = []

    def lectura():
        intentos.append(1)
        raise _api_error(403, "The caller does not have permission")

    with pytest.raises(gspread.exceptions.APIError):
        gsheets.reintentar_lectura(lectura, dormir=lambda _s: None)

    assert len(intentos) == 1


# =====================================================================
# clave_documento
# =====================================================================

@pytest.mark.parametrize("crudo, esperado", [
    ("012345680017", "12345680017"),   # RUT mutilado por Sheets == el real
    ("12345680017", "12345680017"),
    ("218514740014", "218514740014"),  # sin cero inicial: intacto
    ("  012345680017  ", "12345680017"),
    ("", ""),
    (None, ""),
])
def test_clave_documento_normaliza_cero_inicial(crudo, esperado):
    assert televentas_data.clave_documento(crudo) == esperado


def test_clave_documento_no_colapsa_documento_de_ceros():
    """Un documento de puros ceros no puede quedar en cadena vacía: "" es
    justo el valor que marca «cliente sin documento»."""
    assert televentas_data.clave_documento("000") == "000"


# =====================================================================
# filtrar_leads por lista importada
# =====================================================================

def _leads(docs, telefonos=None):
    telefonos = telefonos or ["099111222"] * len(docs)
    return pd.DataFrame({
        "documento": docs,
        "telefono": telefonos,
        "razon_social": [f"CLIENTE {i}" for i in range(len(docs))],
        "nombre_fantasia": [f"C{i}" for i in range(len(docs))],
        "codigo": [f"0{i}000-C" for i in range(len(docs))],
    })


def test_lista_importada_cruza_aunque_el_sheet_haya_comido_el_cero():
    """El caso del bug: la Sheet devuelve el RUT sin el cero inicial."""
    leads = _leads(["012345680017", "218514740014", "090137500012"])
    # Lo que devuelve la Sheet para esa lista (mutilado):
    docs_sheet = {"12345680017", "218514740014", "90137500012"}

    out = televentas_data.filtrar_leads(leads, documentos=docs_sheet)

    assert len(out) == 3
    # Los documentos que salen son los REALES (los de Contabilium), no los
    # mutilados: la app los usa para registrar actividad y armar la ficha.
    assert set(out["documento"]) == {"012345680017", "218514740014", "090137500012"}


def test_lista_importada_no_arrastra_clientes_de_mas():
    leads = _leads(["012345680017", "218514740014"])
    out = televentas_data.filtrar_leads(leads, documentos={"12345680017"})
    assert list(out["documento"]) == ["012345680017"]


def test_filtro_telefono_es_independiente_del_cruce():
    """Los dos motivos de pérdida se cuentan por separado (es lo que
    muestra el desglose de la app)."""
    leads = _leads(["012345680017", "090137500012"], telefonos=["099111222", ""])
    docs = {"12345680017", "90137500012"}

    assert len(televentas_data.filtrar_leads(leads, documentos=docs)) == 2
    assert len(televentas_data.filtrar_leads(
        leads, documentos=docs, con_telefono=True)) == 1


# =====================================================================
# matchear_seleccion (Excel de Ernesto — también come el cero)
# =====================================================================

def test_match_de_planilla_con_rut_sin_cero():
    leads = _leads(["012345680017", "218514740014"])
    subido = pd.DataFrame({"RUT": ["12345680017", "218514740014"]})

    matched, faltan = televentas_data.matchear_seleccion(subido, leads)

    assert faltan == []
    assert set(matched["documento"]) == {"012345680017", "218514740014"}


def test_match_reporta_lo_que_realmente_no_existe():
    leads = _leads(["012345680017"])
    subido = pd.DataFrame({"RUT": ["12345680017", "999999999999"]})

    matched, faltan = televentas_data.matchear_seleccion(subido, leads)

    assert len(matched) == 1
    assert faltan == ["999999999999"]


# =====================================================================
# documentos_de_importacion
# =====================================================================

def test_lista_no_cuenta_dos_veces_al_mismo_cliente():
    """Antes de reparar la Sheet, un cliente puede estar escrito de las dos
    formas (mutilado por USER_ENTERED y entero por RAW). Es UNO solo."""
    df_imp = pd.DataFrame({
        "nombre": ["Lista Ernesto", "Lista Ernesto", "Lista Ernesto", "Otra"],
        "documento": ["12345680017", "012345680017", "218514740014", "999"],
    })
    docs = televentas_crm.documentos_de_importacion(df_imp, "Lista Ernesto")

    assert len(docs) == 2
    leads = _leads(["012345680017", "218514740014"])
    assert len(televentas_data.filtrar_leads(leads, documentos=docs)) == 2


def test_lista_inexistente_devuelve_vacio():
    df_imp = pd.DataFrame({"nombre": ["A"], "documento": ["012345680017"]})
    assert televentas_crm.documentos_de_importacion(df_imp, "B") == set()


# =====================================================================
# Comentario y problema de pago del vendedor de calle
# =====================================================================

def test_marca_positiva_acepta_lo_que_tipea_el_vendedor():
    # La planilla real trae el propio nombre de la columna como valor.
    for v in ("Problema de pago", "x", "X", "1", "SI", "sí", "true"):
        assert televentas_data.es_marca_positiva(v), v


def test_marca_vacia_no_es_problema_de_pago():
    for v in ("", "   ", None, "0", "no", "NO", "nan", "-"):
        assert not televentas_data.es_marca_positiva(v), repr(v)


def test_planilla_arrastra_comentario_y_marca_de_pago():
    leads = _leads(["012345680017", "218514740014", "090137500012"])
    subido = pd.DataFrame({
        "RUT": ["012345680017", "218514740014", "090137500012"],
        "Comentario": ["1 año para pagar boleta", None, "cerro"],
        "Problema de pago": ["Problema de pago", None, None],
    })

    matched, faltan = televentas_data.matchear_seleccion(subido, leads)

    assert faltan == []
    por_doc = matched.set_index("documento")
    assert por_doc.loc["012345680017", "comentario"] == "1 año para pagar boleta"
    assert bool(por_doc.loc["012345680017", "problema_pago"]) is True
    # Sin comentario ni marca: vacío y False, no NaN.
    assert por_doc.loc["218514740014", "comentario"] == ""
    assert bool(por_doc.loc["218514740014", "problema_pago"]) is False
    # Comentario sin marca de pago: se guarda el comentario nomás.
    assert por_doc.loc["090137500012", "comentario"] == "cerro"
    assert bool(por_doc.loc["090137500012", "problema_pago"]) is False


def test_planilla_sin_esas_columnas_no_rompe():
    leads = _leads(["012345680017"])
    subido = pd.DataFrame({"RUT": ["012345680017"]})

    matched, _ = televentas_data.matchear_seleccion(subido, leads)

    assert matched.iloc[0]["comentario"] == ""
    assert bool(matched.iloc[0]["problema_pago"]) is False


def test_notas_por_lead_consolida_todas_las_listas():
    """El comentario y la marca son del CLIENTE, no de la lista: tienen que
    verse aunque la operadora esté trabajando otra lista."""
    df_imp = pd.DataFrame([
        {"nombre": "Lista A", "documento": "012345680017", "codigo": "", "razon_social": "",
         "fecha_carga": "2026-07-01 10:00", "agente": "", "comentario": "paga a 90 dias",
         "problema_pago": "1"},
        {"nombre": "Lista B", "documento": "218514740014", "codigo": "", "razon_social": "",
         "fecha_carga": "2026-08-01 10:00", "agente": "", "comentario": "", "problema_pago": ""},
    ])
    notas = televentas_crm.notas_por_lead(df_imp)

    # Solo entra quien tiene algo que decir.
    assert list(notas.index) == ["12345680017"]
    assert bool(notas.loc["12345680017", "problema_pago"]) is True
    assert notas.loc["12345680017", "nota_lista"] == "Lista A"


def test_notas_cruzan_aunque_el_rut_este_mutilado():
    df_imp = pd.DataFrame([
        {"nombre": "L", "documento": "12345680017", "codigo": "", "razon_social": "",
         "fecha_carga": "2026-07-01 10:00", "agente": "", "comentario": "ojo",
         "problema_pago": "1"},
    ])
    notas = televentas_crm.notas_por_lead(df_imp)

    leads = _leads(["012345680017"])
    leads["_clave"] = leads["documento"].map(televentas_data.clave_documento)
    out = leads.merge(notas, how="left", left_on="_clave", right_index=True)

    assert out.iloc[0]["comentario"] == "ojo"
    assert bool(out.iloc[0]["problema_pago"]) is True


def test_reimportar_actualiza_el_comentario_y_no_borra_la_marca():
    """Append-only: la re-importación no pisa filas. Gana el último valor
    NO vacío de cada campo, así una lista sin la columna no borra la alerta."""
    df_imp = pd.DataFrame([
        {"nombre": "L", "documento": "012345680017", "codigo": "", "razon_social": "",
         "fecha_carga": "2026-07-01 10:00", "agente": "", "comentario": "viejo",
         "problema_pago": "1"},
        {"nombre": "L", "documento": "012345680017", "codigo": "", "razon_social": "",
         "fecha_carga": "2026-08-03 09:00", "agente": "", "comentario": "nuevo",
         "problema_pago": ""},
    ])
    notas = televentas_crm.notas_por_lead(df_imp)

    assert notas.loc["12345680017", "comentario"] == "nuevo"
    assert bool(notas.loc["12345680017", "problema_pago"]) is True


def test_notas_vacias_devuelven_schema_correcto():
    vacio = televentas_crm.notas_por_lead(pd.DataFrame())
    assert list(vacio.columns) == televentas_crm.NOTAS_COLS
    assert vacio.empty


# =====================================================================
# estado_actual_por_lead — que la gestión vuelva al lead correcto
# =====================================================================

def test_la_gestion_vuelve_al_lead_aunque_el_sheet_haya_comido_el_cero():
    """Era el daño más caro: el cliente quedaba «Sin gestionar» para
    siempre y su seguimiento nunca aparecía."""
    act = pd.DataFrame([{
        "timestamp": "2026-08-01 10:00",
        "documento": "12345680017",          # mutilado por la Sheet
        "razon_social": "LA BARRAQUITA",
        "agente": "Vale", "canal": "Llamada",
        "resultado": "Volver a llamar", "nota": "",
        "proximo_seguimiento": "2026-08-05",
        "monto_pedido": 0.0, "nro_orden": "",
    }])
    estado = televentas_crm.estado_actual_por_lead(act)

    leads = _leads(["012345680017", "218514740014"])   # RUT real, con cero
    leads["_clave"] = leads["documento"].map(televentas_data.clave_documento)
    out = leads.merge(estado, how="left", left_on="_clave", right_index=True)

    fila = out[out["documento"] == "012345680017"].iloc[0]
    assert fila["estado"] == "En seguimiento"
    assert fila["proximo_seguimiento"] == "2026-08-05"
    # El otro cliente sigue sin gestión.
    assert pd.isna(out[out["documento"] == "218514740014"].iloc[0]["estado"])


def test_estado_toma_la_ultima_gestion():
    act = pd.DataFrame([
        {"timestamp": "2026-08-01 10:00", "documento": "012345680017",
         "razon_social": "X", "agente": "V", "canal": "Llamada",
         "resultado": "No atendió", "nota": "", "proximo_seguimiento": "",
         "monto_pedido": 0.0, "nro_orden": ""},
        {"timestamp": "2026-08-02 11:00", "documento": "12345680017",
         "razon_social": "X", "agente": "V", "canal": "Llamada",
         "resultado": "Pedido cargado", "nota": "", "proximo_seguimiento": "",
         "monto_pedido": 5000.0, "nro_orden": "A-1"},
    ])
    estado = televentas_crm.estado_actual_por_lead(act)

    # Las dos filas son el MISMO cliente pese al RUT distinto: una sola
    # entrada, con el historial completo.
    assert len(estado) == 1
    fila = estado.iloc[0]
    assert fila["estado"] == "Compró"
    assert fila["num_contactos"] == 2
    assert fila["monto_generado"] == 5000.0
