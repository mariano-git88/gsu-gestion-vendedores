"""
test_rendicion_factura.py — Resolución de Nº de factura contra el índice.

Blinda el fix de la colisión FAC/NC reportada por Valeria (2026-07-07): en
Contabilium una factura (FAC) y una nota de crédito (NCF) pueden compartir el
mismo `Numero` ("A-000...") porque numeran por secuencias separadas. El
simulador debe SIEMPRE tomar la factura, no la NC.

Se corre sin API ni pytest:  python3 tests/test_rendicion_factura.py
(desde la raíz del repo). Datos 100% sintéticos.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rendicion


def _comp(numero, tipo, id_, total=1000.0, id_cliente=1):
    return {
        "id": id_, "numero": numero, "total": total,
        "id_cliente": id_cliente, "razon_social": f"CLIENTE {id_cliente}",
        "tipo": tipo,
    }


def _indice(comps):
    """Arma el índice como lo haría construir_indice_facturas (fuerte=listas)."""
    fuerte, sufijo = {}, {}
    for c in comps:
        fuerte.setdefault(rendicion._norm_factura(c["numero"]), []).append(c)
        suf = rendicion._sufijo_num(c["numero"])
        if suf:
            sufijo.setdefault(suf, []).append(c)
    return {"fuerte": fuerte, "sufijo": sufijo}


def test_colision_prefiere_factura():
    """FAC y NC con el mismo número → devuelve la FAC (bug de Valeria)."""
    idx = _indice([
        _comp("A-00033352", "FAC", 2219462, total=7720.65),
        _comp("A-00033352", "NCF", 2485364, total=-1216.69),
    ])
    comp, nota = rendicion._buscar_factura("A-00033352", idx)
    assert comp is not None and comp["tipo"] == "FAC" and comp["id"] == 2219462, comp
    assert nota == "", nota


def test_colision_orden_inverso():
    """El resultado no depende del orden de paginación (NC primero)."""
    idx = _indice([
        _comp("A-00033352", "NCF", 2485364),
        _comp("A-00033352", "FAC", 2219462),
    ])
    comp, _ = rendicion._buscar_factura("A-00033352", idx)
    assert comp["tipo"] == "FAC", comp


def test_solo_nc_marca_revisar():
    """Si el número SOLO matchea una NC (el vendedor tipeó mal), se devuelve
    la NC para que analizar_fila la mande a REVISAR con 'es una NC'."""
    idx = _indice([_comp("A-00099999", "NCF", 111)])
    comp, _ = rendicion._buscar_factura("A-00099999", idx)
    assert comp is not None and comp["tipo"] == "NCF", comp
    # y el análisis debe marcarla REVISAR
    fila = rendicion.FilaCobranza(
        fila_excel=2, fecha=None, nro_recibo="1", nro_cliente="1",
        facturas=["A-00099999"], descuento_flag=None, nro_cheque="",
        observaciones="", efectivo=100.0, cheque=0.0, total_recibo=100.0,
    )
    res = rendicion.analizar_fila(fila, idx)
    assert res.estado == rendicion.ESTADO_REVISAR
    assert any("Nota de Cr" in m for m in res.motivos), res.motivos


def test_factura_simple():
    """Sin colisión, la factura resuelve normal."""
    idx = _indice([_comp("A-00033576", "FAC", 2255270)])
    comp, nota = rendicion._buscar_factura("A-00033576", idx)
    assert comp["id"] == 2255270 and nota == ""


def test_no_encontrada():
    idx = _indice([_comp("A-00000001", "FAC", 1)])
    comp, nota = rendicion._buscar_factura("A-99999999", idx)
    assert comp is None and "no encontrada" in nota


def test_sufijo_prefiere_factura():
    """Respaldo por sufijo: si la serie difiere de formato pero coincide el
    número, también prefiere la FAC sobre la NC."""
    idx = _indice([
        _comp("B-33352", "NCF", 900),   # normaliza distinto → cae a sufijo
        _comp("B-33352", "FAC", 901),
    ])
    # buscamos con el formato 'A-00033352': el fuerte no matchea, el sufijo sí
    comp, nota = rendicion._buscar_factura("A-00033352", idx)
    assert comp is not None and comp["tipo"] == "FAC", (comp, nota)
    assert "serie difiere" in nota, nota


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
