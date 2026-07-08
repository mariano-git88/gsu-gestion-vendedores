"""
test_rendicion_cheque.py — Opción 2 del Nº de cheque (decisión Valeria 2026-07-08).

El Nº de cheque NO es obligatorio en la planilla: Valeria lo confirma al
ejecutar, con el cheque a la vista. Esto verifica que:
  1. El simulador NO manda a REVISAR una fila con cheque sin número (si el
     monto cuadra, queda OK y ejecutable).
  2. El ejecutor (write path) RECHAZA imputar un cheque sin número — red de
     seguridad para que nunca se escriba un cheque no referenciable.

Se corre sin API ni pytest:  python3 tests/test_rendicion_cheque.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rendicion
import rendicion_ejecutor


def _indice_una_factura(numero, total):
    comp = {"id": 999, "numero": numero, "total": total,
            "id_cliente": 1, "razon_social": "CLIENTE", "tipo": "FAC"}
    return {"fuerte": {rendicion._norm_factura(numero): [comp]}, "sufijo": {}}


def test_simulador_no_bloquea_cheque_sin_numero():
    """Cheque sin número + monto que cuadra (90%) → OK y ejecutable, con nota."""
    idx = _indice_una_factura("A-00033576", 7026.44)
    fila = rendicion.FilaCobranza(
        fila_excel=6, fecha=None, nro_recibo="1", nro_cliente="6489",
        facturas=["A-00033576"], descuento_flag=True, nro_cheque="",
        observaciones="", efectivo=0.0, cheque=6324.0, total_recibo=6324.0,
    )
    res = rendicion.analizar_fila(fila, idx)
    assert res.estado == rendicion.ESTADO_OK, (res.estado, res.motivos)
    assert res.es_ejecutable, "debería poder ejecutarse (el nº se ingresa al confirmar)"
    assert any("se ingresa al confirmar" in m for m in res.motivos), res.motivos
    # y params_ejecucion trae el nro_cheque vacío (la UI lo completa)
    assert res.params_ejecucion()["nro_cheque"] == ""


def _plan(cobro_cheque, nro_cheque):
    return rendicion_ejecutor.PlanEjecucion(
        id_factura=1, numero_factura="A-1", neto_factura=100.0,
        total_con_iva=122.0, saldo_actual=122.0, aplica_nc=False,
        nc_neto=0.0, nc_con_iva=0.0, cobro_efectivo=0.0,
        cobro_cheque=cobro_cheque, nro_cheque=nro_cheque, body_nc=None,
        body_cobro={"Id": 1, "Pagos": []},
    )


def test_ejecutor_rechaza_cheque_sin_numero():
    """ejecutar(dry_run=False) con cheque y sin número → NO escribe, error claro."""
    plan = _plan(cobro_cheque=6324.0, nro_cheque="")
    # session=None es seguro: el guard retorna ANTES de tocar la red.
    _, res = rendicion_ejecutor.ejecutar(None, plan, dry_run=False)
    assert res.ok is False
    assert res.error and "cheque" in res.error.lower(), res.error


def test_ejecutor_dryrun_no_escribe_igual():
    """dry_run siempre es preview (ok=True), aunque falte el número."""
    plan = _plan(cobro_cheque=6324.0, nro_cheque="")
    _, res = rendicion_ejecutor.ejecutar(None, plan, dry_run=True)
    assert res.ok is True and res.dry_run is True


def test_ejecutor_bloquea_nc_temporal():
    """Freno de seguridad 2026-07-08: la imputación con NC no se ejecuta (queda
    en pausa hasta corregir la estructura del recibo). No debe crear NC."""
    plan = rendicion_ejecutor.PlanEjecucion(
        id_factura=1, numero_factura="A-1", neto_factura=100.0,
        total_con_iva=122.0, saldo_actual=122.0, aplica_nc=True,
        nc_neto=10.0, nc_con_iva=12.2, cobro_efectivo=109.8,
        cobro_cheque=0.0, nro_cheque="", body_nc={"x": 1},
        body_cobro={"Id": 1, "Pagos": []},
    )
    _, res = rendicion_ejecutor.ejecutar(None, plan, dry_run=False)
    assert res.ok is False
    assert res.id_nc is None, "no debe haber creado NC"
    assert "pausa" in (res.error or "").lower(), res.error


def test_extraer_id_casing_contabilium():
    """anularComprobante devuelve `idComprobante` (minúscula) — 1er test real 2026-07-08."""
    assert rendicion_ejecutor._extraer_id({"idComprobante": 2496173, "errores": ""}) == 2496173
    assert rendicion_ejecutor._extraer_id({"Id": 5}) == 5
    assert rendicion_ejecutor._extraer_id({"ID": 7}) == 7
    assert rendicion_ejecutor._extraer_id({"IdComprobante": 9}) == 9
    assert rendicion_ejecutor._extraer_id({"errores": "x"}) is None
    assert rendicion_ejecutor._extraer_id(None) is None


def test_valor_errores():
    """errores vacío no dispara; errores con texto sí se detecta."""
    assert not rendicion_ejecutor._valor({"errores": ""}, "errores", "Errores")
    assert rendicion_ejecutor._valor({"errores": "falló"}, "errores", "Errores") == "falló"
    assert rendicion_ejecutor._valor({"Errores": "x"}, "errores", "Errores") == "x"


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
