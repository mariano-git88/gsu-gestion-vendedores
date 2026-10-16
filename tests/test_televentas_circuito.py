"""
Tests del circuito de llamado a inactivos de Televentas.

Contexto (oct-2026): Ernesto y Gustavo pasaron un diagrama para estandarizar
cómo se trabaja un cliente inactivo — llamada, 2º intento a las 24 h,
WhatsApp, cinco esperas de respuesta (24/48/72/24/24 h), derivación al
vendedor con 15 días para responder, llamada de control a los 15 días si
atendió, y una pausa para el que no reactiva.

`televentas_crm.siguiente_paso` traduce eso a "qué toca y para cuándo" a
partir de las gestiones registradas. Estos tests son puros (no tocan Sheets).
"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import televentas_crm as crm  # noqa: E402


def _gestiones(*filas, documento="212345670017"):
    """Arma gestiones de un cliente: (timestamp, resultado[, proximo])."""
    out = []
    for f in filas:
        ts, res = f[0], f[1]
        out.append({"timestamp": ts, "documento": documento, "resultado": res,
                    "proximo_seguimiento": f[2] if len(f) > 2 else "",
                    "monto_pedido": 0, "nota": ""})
    return pd.DataFrame(out)


def test_sin_gestiones_arranca_por_llamar():
    paso = crm.siguiente_paso(pd.DataFrame())
    assert paso == {"accion": crm.PASO_INICIAL, "fecha": ""}


def test_no_atendio_una_vez_pide_segundo_intento_al_dia_siguiente():
    paso = crm.siguiente_paso(_gestiones(("2026-10-16 10:00", "No atendió")))
    assert "2º intento" in paso["accion"]
    assert paso["fecha"] == "2026-10-17"


def test_no_atendio_dos_veces_pasa_a_whatsapp():
    paso = crm.siguiente_paso(_gestiones(
        ("2026-10-16 10:00", "No atendió"),
        ("2026-10-17 10:00", "No atendió")))
    assert "WhatsApp" in paso["accion"]
    assert paso["fecha"] == "2026-10-18"


def test_las_cinco_esperas_de_whatsapp_siguen_el_diagrama():
    """24 h, 48 h, 72 h, 24 h, 24 h — y a la quinta sin respuesta, derivar."""
    filas = [("2026-10-16 10:00", "No atendió"), ("2026-10-17 10:00", "No atendió")]
    dia = date(2026, 10, 18)
    esperadas = []
    for n, espera in enumerate(crm.ESPERAS_WHATSAPP, start=1):
        filas.append((f"{dia.isoformat()} 10:00", "WhatsApp enviado"))
        paso = crm.siguiente_paso(_gestiones(*filas))
        dia = dia + pd.Timedelta(days=espera)
        esperadas.append((n, paso["fecha"], dia.isoformat()))
        if n < len(crm.ESPERAS_WHATSAPP):
            assert f"{n + 1}º de 5" in paso["accion"]
        else:
            assert "Derivar" in paso["accion"]
    assert [e[1] for e in esperadas] == [e[2] for e in esperadas]


def test_una_respuesta_corta_la_racha_de_whatsapp():
    """Si respondió en el medio, la cuenta de reenvíos vuelve a empezar."""
    paso = crm.siguiente_paso(_gestiones(
        ("2026-10-01 10:00", "WhatsApp enviado"),
        ("2026-10-02 10:00", "WhatsApp enviado"),
        ("2026-10-03 10:00", "Volver a llamar"),
        ("2026-10-04 10:00", "WhatsApp enviado")))
    assert "2º de 5" in paso["accion"]


def test_si_atendio_con_o_sin_pedido_va_control_a_15_dias():
    for res in ("Pedido cargado", "Contactado - no necesita ahora",
                "Contactado - interesado"):
        paso = crm.siguiente_paso(_gestiones(("2026-10-16 10:00", res)))
        assert "control" in paso["accion"].lower(), res
        assert paso["fecha"] == "2026-10-31", res


def test_derivado_al_vendedor_espera_su_respuesta_15_dias():
    for res in ("Contactado - pide visita", "Sin respuesta - derivado a vendedor"):
        paso = crm.siguiente_paso(_gestiones(("2026-10-16 10:00", res)))
        assert "vendedor" in paso["accion"], res
        assert paso["fecha"] == "2026-10-31", res


def test_la_fecha_manual_gana_sobre_la_calculada():
    paso = crm.siguiente_paso(_gestiones(("2026-10-16 10:00", "No atendió", "2026-10-20")))
    assert "2º intento" in paso["accion"]
    assert paso["fecha"] == "2026-10-20"


def test_los_resultados_que_cierran_no_proponen_nada():
    for res in ("Contactado - no interesado", "No molestar / baja",
                "Número equivocado / no existe"):
        assert crm.siguiente_paso(_gestiones(("2026-10-16 10:00", res))) == \
            {"accion": "", "fecha": ""}, res


def test_el_orden_de_las_filas_no_importa():
    a = _gestiones(("2026-10-16 10:00", "No atendió"), ("2026-10-17 10:00", "No atendió"))
    assert crm.siguiente_paso(a) == crm.siguiente_paso(a.iloc[::-1])


def test_pausado_vuelve_a_pendiente_cuando_termina_la_pausa():
    hasta = crm.fecha_pausa(90, date(2026, 10, 16))
    assert hasta == "2027-01-14"
    act = _gestiones(("2026-10-16 10:00", "No reactiva - pausar", hasta))
    clave = act["documento"].map(crm.televentas_data.clave_documento).iloc[0]

    durante = crm.estado_actual_por_lead(act, hoy=date(2026, 12, 1))
    assert durante.loc[clave, "estado"] == "Pausado"
    assert durante.loc[clave, "proxima_fecha"] == hasta

    despues = crm.estado_actual_por_lead(act, hoy=date(2027, 1, 14))
    assert despues.loc[clave, "estado"] == "Pendiente"


def test_todo_resultado_tiene_estado_y_los_contactos_empiezan_con_contactado():
    """El tablero de Actividad cuenta contactos efectivos por el prefijo."""
    assert set(crm.RESULTADOS) == set(crm._ESTADO_POR_RESULTADO)
    for res in ("Contactado - no necesita ahora", "Contactado - pide visita"):
        assert res.startswith("Contactado")


def test_gestiones_de_cruza_aunque_la_sheet_se_haya_comido_el_cero():
    act = _gestiones(("2026-10-16 10:00", "No atendió"), documento="12345680017")
    assert len(crm.gestiones_de(act, "012345680017")) == 1
