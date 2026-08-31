"""
Tests del buzón de armados (Picking del depósito → facturador).

El buzón es un log append-only en Google Sheets: la app de Picking apenda
un evento `armado` y el facturador apenda `facturado`. El estado de una
orden es su ÚLTIMO evento.

Se hizo append-only a propósito: reintentar una escritura en Sheets duplica
filas (ver `feedback_gsheets_reintento_solo_lecturas`), y la PC del depósito
va a reintentar cada vez que se le corte internet. Con eventos, un duplicado
es inofensivo — pero solo si la derivación del estado lo descarta bien. Eso
es lo que verifican estos tests.

Son puros: no tocan red ni Sheets.
"""

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import facturador  # noqa: E402
import gsheets  # noqa: E402


def _evento(**kw):
    """Fila del buzón con todas las columnas, para no repetirlas en cada test."""
    fila = {c: "" for c in gsheets.ARMADOS_COLUMNS}
    fila.update(kw)
    return fila


def _df(*eventos):
    return pd.DataFrame(list(eventos), columns=gsheets.ARMADOS_COLUMNS)


# ---------------------------------------------------------------------
# Derivación del estado
# ---------------------------------------------------------------------

def test_una_orden_armada_queda_pendiente():
    df = _df(_evento(
        timestamp="2026-08-07T18:00:00Z", evento="armado",
        id_orden="2311651", numero_orden="00012036", bultos="3",
    ))
    pend = facturador.armados_pendientes_de_facturar(df)
    assert len(pend) == 1
    assert pend.iloc[0]["numero_orden"] == "00012036"
    assert pend.iloc[0]["bultos"] == "3"


def test_orden_facturada_no_aparece_como_pendiente():
    df = _df(
        _evento(timestamp="2026-08-07T18:00:00Z", evento="armado", id_orden="2311651"),
        _evento(timestamp="2026-08-07T19:30:00Z", evento="facturado",
                id_orden="2311651", id_comprobante="2589858", cae="123456"),
    )
    assert facturador.armados_pendientes_de_facturar(df).empty
    estado = facturador.derivar_estado_armados(df)
    assert estado.iloc[0]["estado"] == "facturado"
    assert estado.iloc[0]["id_comprobante"] == "2589858"


def test_armado_reenviado_no_duplica_la_orden():
    """La PC del depósito reintenta el envío si se le cortó internet, así que
    el mismo armado puede quedar apendado dos o tres veces. Tiene que
    aparecer UNA sola vez en la cola."""
    df = _df(
        _evento(timestamp="2026-08-07T18:00:00Z", evento="armado",
                id_orden="2311651", bultos="3"),
        _evento(timestamp="2026-08-07T18:05:00Z", evento="armado",
                id_orden="2311651", bultos="3"),
        _evento(timestamp="2026-08-07T18:11:00Z", evento="armado",
                id_orden="2311651", bultos="3"),
    )
    pend = facturador.armados_pendientes_de_facturar(df)
    assert len(pend) == 1


def test_un_reenvio_tardio_no_resucita_una_orden_ya_facturada():
    """Caso feo: el depósito reintenta un armado que ya se facturó. Como el
    reintento llega DESPUÉS, sería el último evento y la orden volvería a la
    cola de Valeria — con riesgo de facturarla dos veces.

    Se resuelve mirando si la orden tiene algún evento `facturado`, no solo
    el último."""
    df = _df(
        _evento(timestamp="2026-08-07T18:00:00Z", evento="armado", id_orden="2311651"),
        _evento(timestamp="2026-08-07T19:30:00Z", evento="facturado",
                id_orden="2311651", id_comprobante="2589858"),
        _evento(timestamp="2026-08-07T20:00:00Z", evento="armado", id_orden="2311651"),
    )
    assert facturador.armados_pendientes_de_facturar(df).empty


def test_un_armado_posterior_a_la_factura_queda_marcado():
    """No alcanza con dejarla fuera de la cola: puede ser un rearmado de
    verdad y alguien tiene que poder verlo."""
    df = _df(
        _evento(timestamp="2026-08-07T18:00:00Z", evento="armado", id_orden="1"),
        _evento(timestamp="2026-08-07T19:30:00Z", evento="facturado", id_orden="1"),
        _evento(timestamp="2026-08-07T20:00:00Z", evento="armado", id_orden="1"),
        _evento(timestamp="2026-08-07T18:00:00Z", evento="armado", id_orden="2"),
        _evento(timestamp="2026-08-07T19:30:00Z", evento="facturado", id_orden="2"),
    )
    estado = facturador.derivar_estado_armados(df).set_index("id_orden")
    assert bool(estado.loc["1", "rearmado_post_factura"]) is True
    assert bool(estado.loc["2", "rearmado_post_factura"]) is False


def test_ordenes_distintas_no_se_pisan():
    df = _df(
        _evento(timestamp="2026-08-07T18:00:00Z", evento="armado",
                id_orden="2311651", numero_orden="00012036"),
        _evento(timestamp="2026-08-07T18:10:00Z", evento="armado",
                id_orden="2311652", numero_orden="00012037"),
        _evento(timestamp="2026-08-07T19:00:00Z", evento="facturado",
                id_orden="2311651"),
    )
    pend = facturador.armados_pendientes_de_facturar(df)
    assert list(pend["numero_orden"]) == ["00012037"]


def test_las_mas_viejas_primero():
    df = _df(
        _evento(timestamp="2026-08-07T18:00:00Z", evento="armado",
                id_orden="3", numero_orden="00000003"),
        _evento(timestamp="2026-08-05T09:00:00Z", evento="armado",
                id_orden="1", numero_orden="00000001"),
        _evento(timestamp="2026-08-06T14:00:00Z", evento="armado",
                id_orden="2", numero_orden="00000002"),
    )
    pend = facturador.armados_pendientes_de_facturar(df)
    assert list(pend["numero_orden"]) == ["00000001", "00000002", "00000003"]


# ---------------------------------------------------------------------
# El detalle de items
# ---------------------------------------------------------------------

def test_items_se_deserializan():
    items = [
        {"codigo": "MTJ 400", "concepto": "ADHESIVO", "pedido": 6, "escaneado": 6, "combo": False},
        {"codigo": "CDB H 30 D", "concepto": "CANDADO", "pedido": 4, "escaneado": 3, "combo": False},
    ]
    df = _df(_evento(timestamp="2026-08-07T18:00:00Z", evento="armado",
                     id_orden="2311651", items_json=json.dumps(items)))
    pend = facturador.armados_pendientes_de_facturar(df)
    assert pend.iloc[0]["items"][1]["escaneado"] == 3


def test_items_rotos_no_tumban_la_pantalla():
    """Si la celda se truncó (el límite de Sheets son 50.000 caracteres), la
    orden tiene que seguir apareciendo, sin detalle."""
    df = _df(_evento(timestamp="2026-08-07T18:00:00Z", evento="armado",
                     id_orden="2311651", numero_orden="00012036",
                     items_json='[{"codigo": "MTJ 400", "pedi'))
    pend = facturador.armados_pendientes_de_facturar(df)
    assert len(pend) == 1
    assert pend.iloc[0]["items"] == []


def test_sin_items_no_rompe():
    df = _df(_evento(timestamp="2026-08-07T18:00:00Z", evento="armado", id_orden="1"))
    assert facturador.armados_pendientes_de_facturar(df).iloc[0]["items"] == []


# ---------------------------------------------------------------------
# Bordes
# ---------------------------------------------------------------------

def test_buzon_vacio():
    df = pd.DataFrame({c: pd.Series(dtype="object") for c in gsheets.ARMADOS_COLUMNS})
    assert facturador.armados_pendientes_de_facturar(df).empty


def test_filas_sin_id_orden_se_descartan():
    df = _df(
        _evento(timestamp="2026-08-07T18:00:00Z", evento="armado", id_orden=""),
        _evento(timestamp="2026-08-07T18:01:00Z", evento="armado", id_orden="2311651"),
    )
    assert len(facturador.armados_pendientes_de_facturar(df)) == 1


def test_el_numero_de_orden_conserva_los_ceros():
    """Si en algún momento el buzón se escribe con USER_ENTERED en vez de RAW,
    Sheets convierte "00012036" en 12036 y el cruce contra Contabilium se
    rompe en silencio. Este test fija la expectativa del lado de la lectura."""
    df = _df(_evento(timestamp="2026-08-07T18:00:00Z", evento="armado",
                     id_orden="2311651", numero_orden="00012036"))
    pend = facturador.armados_pendientes_de_facturar(df)
    assert pend.iloc[0]["numero_orden"].startswith("000")


# ---------------------------------------------------------------------
# La adenda en las observaciones de la factura
# ---------------------------------------------------------------------

# Las Observaciones de las órdenes de GSU arrancan siempre con este machete.
_MACHETE = (
    "Cuentas bancarias habilitadas para cobros: BROU 110954910-00001 || "
    "ITAU 9818174 || BBVA 25540491 || SANTANDER Sucursal: 0073 Cuenta: "
    "1391763. Consultas a pedidos@suprabond.com.uy o 093 900 536"
)


def test_sin_adenda_las_observaciones_quedan_igual():
    assert facturador._observaciones_con_adenda(_MACHETE, None) == _MACHETE
    assert facturador._observaciones_con_adenda(_MACHETE, "  ") == _MACHETE


def test_la_adenda_va_primero():
    """Va adelante para que sea lo primero que se lee en la factura, y para
    que si hay que recortar se pierda el machete y no el dato."""
    out = facturador._observaciones_con_adenda(_MACHETE, "SODIMAC Sucursal Carrasco")
    assert out.startswith("SODIMAC Sucursal Carrasco")
    assert "BROU" in out


def test_una_adenda_larga_no_se_recorta():
    """El caso que importa: si la suma pasa el tope, lo que se corta es el
    machete de cuentas bancarias, nunca la sucursal ni la orden de compra."""
    adenda = "OC 4500123456 - SODIMAC Sucursal Nuevocentro - " + ("detalle " * 40)
    out = facturador._observaciones_con_adenda(_MACHETE, adenda)
    assert len(out) <= facturador.OBSERVACIONES_MAX
    assert out.startswith(adenda.strip()[:60])
    assert adenda.strip() in out


def test_orden_sin_observaciones():
    assert facturador._observaciones_con_adenda("", "OC 12345") == "OC 12345"


def test_adenda_mas_larga_que_el_tope_se_corta_al_tope():
    out = facturador._observaciones_con_adenda(_MACHETE, "X" * 700)
    assert len(out) == facturador.OBSERVACIONES_MAX


def test_el_body_de_la_factura_lleva_la_adenda():
    orden = {
        "ID": 2311651,
        "IDCliente": 415839,
        "Observaciones": _MACHETE,
        "Items": [{
            "IdConcepto": 123, "Cantidad": 2, "Concepto": "ADHESIVO",
            "PrecioUnitario": "1.234,56", "Iva": 22, "Bonificacion": 0,
            "IDMoneda": 794,
        }],
    }
    body = facturador.mapear_orden_a_body_crear(
        orden, condicion_venta_nombre="30 Cuenta Corriente",
        punto_venta_id=1, inventario_id=1,
        adenda="OC 88-2026 SODIMAC Carrasco",
    )
    assert body["Observaciones"].startswith("OC 88-2026 SODIMAC Carrasco")
    assert body["RefExterna"] == "2311651"


# ---------------------------------------------------------------------
# El IVA no se adivina
#
# Valeria Falero (2026-08-31) contó que uno de los errores que caza a ojo
# antes de facturar son "productos sin el IVA". El código hacía justo lo
# contrario de ayudarla: `float(it.get("Iva") or 22)` emitía con 22 igual,
# en silencio. Si la facturación se mueve al depósito ese ojo no está.
# ---------------------------------------------------------------------

def _orden_con_iva(iva):
    return {
        "ID": 2311651,
        "IDCliente": 415839,
        "Observaciones": _MACHETE,
        "Items": [{
            "IdConcepto": 123, "Cantidad": 2, "Concepto": "ADHESIVO SB-100",
            "PrecioUnitario": "1.234,56", "Iva": iva, "Bonificacion": 0,
            "IDMoneda": 794,
        }],
    }


def _mapear(orden):
    return facturador.mapear_orden_a_body_crear(
        orden, condicion_venta_nombre="30 Cuenta Corriente",
        punto_venta_id=1, inventario_id=1,
    )


def test_iva_22_pasa():
    assert _mapear(_orden_con_iva(22))["Items"][0]["Iva"] == 22.0


def test_iva_10_pasa():
    assert _mapear(_orden_con_iva(10))["Items"][0]["Iva"] == 10.0


def test_producto_sin_iva_frena_la_factura():
    """El caso de Valeria: antes salía con 22 y nadie se enteraba."""
    for vacio in (None, "", "   "):
        with pytest.raises(facturador.IvaNoConfiableError) as e:
            _mapear(_orden_con_iva(vacio))
        assert "ADHESIVO SB-100" in str(e.value)
        assert "no se puede" in str(e.value).lower()


def test_iva_cero_frena_en_vez_de_convertirse_en_22():
    """`0 or 22` da 22: un exento se facturaba con IVA. Ahora frena."""
    with pytest.raises(facturador.IvaNoConfiableError) as e:
        _mapear(_orden_con_iva(0))
    assert "0%" in str(e.value)


def test_iva_con_tasa_rara_frena():
    with pytest.raises(facturador.IvaNoConfiableError):
        _mapear(_orden_con_iva(21))


def test_iva_ilegible_frena():
    with pytest.raises(facturador.IvaNoConfiableError):
        _mapear(_orden_con_iva("veintidós"))


def test_el_error_de_iva_es_un_no_facturable():
    """El facturador ya atrapa OrdenNoFacturableError para saltear la orden
    y seguir con el lote; el error de IVA tiene que entrar por ahí."""
    assert issubclass(
        facturador.IvaNoConfiableError, facturador.OrdenNoFacturableError
    )


def test_revisar_iva_lista_los_problemas_sin_romper():
    """La cola del depósito los muestra como bloqueo ANTES de apretar Emitir."""
    orden = _orden_con_iva(22)
    orden["Items"].append({
        "IdConcepto": 456, "Cantidad": 1, "Concepto": "SELLADOR SB-200",
        "PrecioUnitario": "500,00", "Iva": None, "Bonificacion": 0,
    })
    problemas = facturador.revisar_iva_de_la_orden(orden)
    assert len(problemas) == 1
    assert "SELLADOR SB-200" in problemas[0]


def test_revisar_iva_no_dice_nada_si_esta_todo_bien():
    assert facturador.revisar_iva_de_la_orden(_orden_con_iva(22)) == []
