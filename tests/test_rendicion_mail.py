"""
test_rendicion_mail.py — El recibo con NC se manda por mail al cliente.

Reclamo de Valeria (2026-11-20): a un cliente de Mario le llegaba por mail la
NC pero no el recibo. Causa: la app cortaba en `guardar`, y en el web el mail
lo dispara el navegador DESPUÉS (generarPDF → EnviarCobranzaAutomaticamente).
Esto verifica que:
  1. Se replica la secuencia del navegador, con el MISMO sessionId.
  2. La opción `hdnEnvioCR` se lee del HTML (1 / 0 / no está).
  3. Si el envío falla, la cobranza sigue OK (solo queda un aviso).
  4. Si la imputación quedó mal, NO se le manda nada al cliente.

Se corre sin API ni pytest:  python3 tests/test_rendicion_mail.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_loader
import rendicion_ejecutor as E
import rendicion_web as W


class _Resp:
    def __init__(self, status, text):
        self.status_code, self.text = status, text


def test_secuencia_del_navegador_mismo_session_id():
    llamadas = []

    def fake_post(cookie, path, body):
        llamadas.append((path, body))
        if path.endswith("generarPDF"):
            return {"d": "RC-00013344.pdf"}
        return {"d": None}

    orig = W._post
    W._post = fake_post
    try:
        pasos = W.enviar_recibo_por_mail(
            "c", session_id="SID-1", id_recibo=555, id_persona=77,
            fecha_ddmmyyyy="20/11/2026", nro_recibo="00013344")
    finally:
        W._post = orig
    assert [p for p, _ in llamadas] == [
        "/cobranzase.aspx/generarPDF",
        "/cobranzase.aspx/EnviarCobranzaAutomaticamente",
    ], llamadas
    pdf = llamadas[0][1]
    assert pdf["sessionId"] == "SID-1" and pdf["id"] == 555 and pdf["idPersona"] == 77
    assert pdf["nroComprobante"] == "00013344" and pdf["tipo"] == "RC"
    assert llamadas[1][1] == {"idCobranza": 555, "nombre": "RC-00013344.pdf"}
    assert "pedido" in pasos[-1]


def test_lee_hdnEnvioCR():
    casos = [
        ('<input type="hidden" name="x" id="hdnEnvioCR" value="1" />', True),
        ('<input value="0" id="hdnEnvioCR" type="hidden">', False),
        ('<input type="hidden" id="hdnOtro" value="1">', None),
    ]
    orig = W.requests.get
    try:
        for html, esperado in casos:
            W.requests.get = lambda *a, **k: _Resp(200, html)
            assert W.envio_automatico_activado("c") is esperado, html
    finally:
        W.requests.get = orig


def _plan():
    return E.PlanEjecucion(
        id_factura=1, numero_factura="A-1", neto_factura=1000.0,
        total_con_iva=1220.0, saldo_actual=1220.0, aplica_nc=True,
        nc_neto=100.0, nc_con_iva=122.0, cobro_efectivo=1098.0,
        cobro_cheque=0.0, nro_cheque="", body_nc={"x": 1}, body_cobro=None,
    )


def _correr(saldo_factura, falla_envio=False, activado=True):
    """Corre ejecutar() con la red mockeada. Devuelve (res, envios)."""
    envios = []
    origs = (E._crear_nc, api_loader.api_get, W.crear_recibo_con_nc, E._saldo,
             W.envio_automatico_activado, W.enviar_recibo_por_mail)
    E._crear_nc = lambda s, b: (s, {"idComprobante": 9})
    api_loader.api_get = lambda s, u: (s, {"Numero": "A-9", "Saldo": "122,00"})
    W.crear_recibo_con_nc = lambda cookie, **k: {
        "ok": True, "id_recibo": 555, "nro_recibo": "00013344", "pasos": [],
        "session_id": "SID-1", "fecha": "20/11/2026", "id_persona": 77}
    E._saldo = lambda s, i: (s, saldo_factura if i == 1 else 0.0)
    W.envio_automatico_activado = lambda c: activado

    def fake_enviar(cookie, **k):
        envios.append(k)
        if falla_envio:
            raise W.WebError("generarPDF: HTTP 500")
        return ["Envío por mail al cliente pedido a Contabilium"]

    W.enviar_recibo_por_mail = fake_enviar
    try:
        _, res = E.ejecutar(None, _plan(), dry_run=False, cookie="c",
                            fecha_ddmmyyyy="20/11/2026")
    finally:
        (E._crear_nc, api_loader.api_get, W.crear_recibo_con_nc, E._saldo,
         W.envio_automatico_activado, W.enviar_recibo_por_mail) = origs
    return res, envios


def test_recibo_ok_se_manda():
    res, envios = _correr(saldo_factura=0.0)
    assert res.ok and not res.aviso_mail, (res.error, res.aviso_mail)
    assert len(envios) == 1 and envios[0]["session_id"] == "SID-1"
    assert envios[0]["id_recibo"] == 555


def test_falla_el_envio_la_cobranza_sigue_ok():
    res, envios = _correr(saldo_factura=0.0, falla_envio=True)
    assert res.ok and res.error is None, res.error
    assert res.aviso_mail and "NO volver a ejecutar" in res.aviso_mail
    assert "NC creada" not in res.aviso_mail


def test_imputacion_mal_no_se_manda():
    res, envios = _correr(saldo_factura=500.0)
    assert not res.ok and envios == [], envios


def test_envio_automatico_apagado_no_se_manda():
    res, envios = _correr(saldo_factura=0.0, activado=False)
    assert res.ok and envios == [] and not res.aviso_mail


def test_opcion_ilegible_avisa_y_no_manda():
    res, envios = _correr(saldo_factura=0.0, activado=None)
    assert res.ok and envios == [] and "a mano" in (res.aviso_mail or "")


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
