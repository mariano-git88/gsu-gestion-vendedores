"""
test_signo_nota_credito.py — El signo de las notas de crédito.

Blinda el bug del 2026-09-11: la cuenta UY emite `NCTK` (NC de eTicket),
que no estaba en `api_loader.TIPOS_NEGATIVOS` ni en
`rendicion.TIPOS_NOTA_CREDITO`. Como los `Items` de una NC vienen con
`Cantidad` y `PrecioUnitario` POSITIVOS, esas notas sumaban como venta
en vez de restar, y en rendición se podían tomar como factura cobrable.

La defensa tiene dos señales y alcanza con una: el `TipoFc`, y el total
(`ImporteTotalNeto`), que viene firmado. Estos tests cubren las dos.

Se corre sin API ni pytest:  python3 tests/test_signo_nota_credito.py
(desde la raíz del repo). Datos 100% sintéticos.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_loader
import rendicion


def _header(tipo, neto):
    """Header como lo devuelve /api/comprobantes/search (montos UY string)."""
    return {"TipoFc": tipo, "ImporteTotalNeto": neto}


# ---------------------------------------------------------------- signo

def test_factura_suma():
    assert api_loader._signo_comprobante(_header("FAC", "12.200,00"), "FAC") == 1.0


def test_eticket_suma():
    """El eTicket es una venta: suma igual que la factura."""
    assert api_loader._signo_comprobante(_header("TIK", "1.220,00"), "TIK") == 1.0


def test_nc_eticket_resta():
    """El bug: NCTK sumaba porque no estaba en la lista de tipos."""
    assert api_loader._signo_comprobante(_header("NCTK", "-7.245,00"), "NCTK") == -1.0


def test_nc_factura_resta():
    assert api_loader._signo_comprobante(_header("NCF", "-1.216,69"), "NCF") == -1.0


def test_tipo_desconocido_con_total_negativo_resta():
    """Un TipoFc nuevo que no conocemos: manda el total firmado."""
    assert api_loader._signo_comprobante(_header("NCXX", "-500,00"), "NCXX") == -1.0


def test_tipo_conocido_manda_aunque_el_total_no_ayude():
    """Si el total viniera en cero o sin firmar, el tipo sigue mandando."""
    assert api_loader._signo_comprobante(_header("NCF", "0,00"), "NCF") == -1.0


def test_nota_de_debito_suma():
    """Las ND no son NC: suman como factura."""
    assert api_loader._signo_comprobante(_header("NDF", "3.660,00"), "NDF") == 1.0


# ------------------------------------------------------- rendición: NC

def _comp(tipo, total):
    return {"id": 1, "numero": "A-00033352", "total": total,
            "id_cliente": 1, "razon_social": "CLIENTE", "tipo": tipo}


def test_rendicion_nctk_no_es_factura_cobrable():
    """No se cobra contra una NC: la de eTicket tampoco."""
    assert rendicion._es_nota_credito(_comp("NCTK", -7245.0))
    assert not rendicion._es_factura(_comp("NCTK", -7245.0))


def test_rendicion_factura_es_cobrable():
    assert not rendicion._es_nota_credito(_comp("FAC", 7720.65))
    assert rendicion._es_factura(_comp("FAC", 7720.65))


def test_rendicion_eticket_es_cobrable():
    assert rendicion._es_factura(_comp("TIK", 1220.0))


def test_rendicion_tipo_desconocido_con_total_negativo():
    assert rendicion._es_nota_credito(_comp("NCXX", -500.0))


def test_rendicion_comprobante_vacio_no_es_nc():
    assert not rendicion._es_nota_credito(None)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fallos = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            fallos += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - fallos}/{len(tests)} tests OK")
    sys.exit(1 if fallos else 0)
