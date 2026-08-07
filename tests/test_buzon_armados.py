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
