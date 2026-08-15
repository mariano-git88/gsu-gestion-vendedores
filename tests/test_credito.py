"""
test_credito.py — Blinda el scoring crediticio (`credito.py`).

Este score decide a qué cliente se le ofrece más plazo y por cuánta plata.
Los casos de acá son los que ya se rompieron una vez o los que, si se rompen,
no se notan a simple vista: el vencimiento derivado de la condición, el
desborde al ponderar fechas, las colas de rendición y el veto material.

Se corre sin API ni pytest:  python3 tests/test_credito.py
(desde la raíz del repo). Datos 100% sintéticos.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import credito as C  # noqa: E402

_fallos = []
HOY = pd.Timestamp("2026-08-13")


def _is(label, got, exp):
    ok = got == exp
    print(f"  [{'OK' if ok else 'FALLA'}] {label}: got={got!r} exp={exp!r}")
    if not ok:
        _fallos.append(label)


def _eq(label, got, exp, tol=1e-6):
    ok = abs(float(got) - float(exp)) < tol
    print(f"  [{'OK' if ok else 'FALLA'}] {label}: got={got:,.4f} exp={exp:,.4f}")
    if not ok:
        _fallos.append(label)


def _true(label, cond):
    print(f"  [{'OK' if cond else 'FALLA'}] {label}")
    if not cond:
        _fallos.append(label)


def _comp(rows):
    """Arma un df de comprobantes con el schema de `credito_api`."""
    return pd.DataFrame(
        [
            {
                "id": r.get("id"),
                "id_cliente": r.get("cli", 1),
                "razon_social": r.get("nombre", "CLIENTE TEST"),
                "tipo": r.get("tipo", "FAC"),
                "numero": f"A-{r.get('id'):08d}",
                "emision": pd.Timestamp(r["emision"]),
                "vencimiento_erp": pd.Timestamp(r["emision"])
                + pd.Timedelta(days=30),
                "cond_venta": r.get("cond", "30 Cuenta Corriente"),
                "total": r.get("total", 1000.0),
                "saldo": r.get("saldo", 0.0),
                "moneda": 794,
                "tc": 1.0,
                "id_vendedor": 1,
            }
            for r in rows
        ]
    )


def _pagos(rows):
    return pd.DataFrame(
        [
            {
                "id_recibo": i,
                "fecha_pago": pd.Timestamp(r["fecha"]),
                "id_cliente": r.get("cli", 1),
                "id_comprobante": r["comp"],
                "importe": r["importe"],
            }
            for i, r in enumerate(rows, start=1)
        ]
    )


# =====================================================================
print("\n1. El vencimiento sale de la CONDICIÓN, no del campo del ERP")
# Contabilium llena FechaVencimiento con emisión + 30 SIEMPRE, aunque la
# condición sea 90. Si el score leyera ese campo, un cliente a 90 días
# aparecería con 60 días de atraso inventados.
comp = _comp(
    [
        {"id": 1, "emision": "2026-01-01", "cond": "30 Cuenta Corriente"},
        {"id": 2, "emision": "2026-01-01", "cond": "60 Cuenta Corriente"},
        {"id": 3, "emision": "2026-01-01", "cond": "90 Cuenta Corriente"},
        {"id": 4, "emision": "2026-01-01", "cond": "Contado"},
        {"id": 5, "emision": "2026-01-01", "cond": "30/60/90 Cuenta Corriente"},
    ]
)
h = C.armar_historial(comp, _pagos([]), hoy=HOY)
_is("30 CC → 30 días", int(h.loc[h.id == 1, "plazo_pactado"].iloc[0]), 30)
_is("60 CC → 60 días", int(h.loc[h.id == 2, "plazo_pactado"].iloc[0]), 60)
_is("90 CC → 90 días", int(h.loc[h.id == 3, "plazo_pactado"].iloc[0]), 90)
_is("Contado → 0 días", int(h.loc[h.id == 4, "plazo_pactado"].iloc[0]), 0)
_is("30/60/90 → 60 (promedio de cuotas)",
    int(h.loc[h.id == 5, "plazo_pactado"].iloc[0]), 60)
_is("el vencimiento del ERP se ignora",
    str(h.loc[h.id == 3, "vencimiento"].iloc[0].date()), "2026-04-01")
_is("una condición desconocida cae al default de 30",
    C.plazo_pactado("50/70 Algo Nuevo"), 30)


# =====================================================================
print("\n2. La fecha de pago ponderada NO desborda")
# Ponderar sobre el epoch en nanosegundos (1,7e18) por un importe (1e4)
# desborda int64 y devuelve fechas de 1965. Pasó de verdad: el atraso
# promedio dio -20.324 días.
comp = _comp([{"id": 10, "emision": "2026-01-01", "total": 1_000_000.0}])
pagos = _pagos(
    [
        {"comp": 10, "fecha": "2026-02-01", "importe": 500_000.0},
        {"comp": 10, "fecha": "2026-03-03", "importe": 500_000.0},
    ]
)
h = C.armar_historial(comp, pagos, hoy=HOY)
fp = h["fecha_pago_pond"].iloc[0]
_is("la fecha ponderada cae entre los dos pagos",
    str(fp.date()), "2026-02-16")
_true("el atraso queda en un rango humano", -400 < h["dpd"].iloc[0] < 400)
_eq("atraso = 16-feb menos 31-ene", h["dpd"].iloc[0], 16)

# Con importes muy dispares la fecha se corre hacia el pago grande.
pagos = _pagos(
    [
        {"comp": 10, "fecha": "2026-02-01", "importe": 10.0},
        {"comp": 10, "fecha": "2026-04-01", "importe": 999_990.0},
    ]
)
h2 = C.armar_historial(comp, pagos, hoy=HOY)
_true("la ponderación sigue al monto, no al calendario",
      h2["fecha_pago_pond"].iloc[0] > pd.Timestamp("2026-03-30"))


# =====================================================================
print("\n3. Estados: cobrada / parcial / abierta / sin_recibo")
comp = _comp(
    [
        {"id": 20, "emision": "2026-01-01", "total": 1000.0, "saldo": 0.0},
        {"id": 21, "emision": "2026-01-01", "total": 1000.0, "saldo": 300.0},
        {"id": 22, "emision": "2026-01-01", "total": 1000.0, "saldo": 1000.0},
        {"id": 23, "emision": "2026-01-01", "total": 1000.0, "saldo": 0.0},
    ]
)
pagos = _pagos(
    [
        {"comp": 20, "fecha": "2026-02-01", "importe": 1000.0},
        {"comp": 21, "fecha": "2026-02-01", "importe": 700.0},
    ]
)
h = C.armar_historial(comp, pagos, hoy=HOY)
est = dict(zip(h["id"], h["estado"]))
_is("saldada con recibo → cobrada", est[20], "cobrada")
_is("saldo parcial con pago → parcial", est[21], "parcial")
_is("saldo entero sin pago → abierta", est[22], "abierta")
_is("saldada sin recibo → sin_recibo", est[23], "sin_recibo")
_true("sin_recibo NO aporta atraso (se cobró, no sabemos cuándo)",
      pd.isna(h.loc[h.id == 23, "dpd"].iloc[0]))
_true("una factura abierta no cuenta como pago puntual",
      pd.isna(h.loc[h.id == 22, "dpd"].iloc[0]))
# Emitida el 1-ene-2026 a 30 días → venció el 31-ene; al 13-ago van 194 días.
_eq("la abierta acumula días de vencida",
    h.loc[h.id == 22, "dpd_corriente"].iloc[0], 194)

# Las notas de crédito no son facturas y no entran al historial.
comp_nc = _comp([{"id": 24, "emision": "2026-01-01", "tipo": "NCF",
                  "total": -500.0}])
h_nc = C.armar_historial(pd.concat([comp, comp_nc]), pagos, hoy=HOY)
_is("las NC quedan fuera del historial de facturas", len(h_nc), 4)


# =====================================================================
print("\n4. Colas de rendición: un resto chico y viejo no es deuda")
# La NC del 10% de la rendición que nunca se imputa deja facturas viejas con
# un resto de ~10%. Contarlas como mora manda a rojo a gente que paga antes
# del vencimiento.
comp = _comp(
    [
        # resto del 10% en una factura de hace un año → cola
        {"id": 30, "emision": "2025-06-01", "total": 10_000.0, "saldo": 1_000.0},
        # resto del 60% en una factura de hace un año → deuda de verdad
        {"id": 31, "emision": "2025-06-01", "total": 10_000.0, "saldo": 6_000.0},
        # resto del 10% pero reciente → todavía no es cola, es plazo normal
        {"id": 32, "emision": "2026-08-01", "total": 10_000.0, "saldo": 1_000.0},
    ]
)
pagos = _pagos(
    [
        {"comp": 30, "fecha": "2025-07-01", "importe": 9_000.0},
        {"comp": 31, "fecha": "2025-07-01", "importe": 4_000.0},
        {"comp": 32, "fecha": "2026-08-05", "importe": 9_000.0},
    ]
)
h = C.armar_historial(comp, pagos, hoy=HOY)
viva = C.marcar_residuos(h[h["estado"].isin(["abierta", "parcial"])])
res = dict(zip(viva["id"], viva["es_residuo"]))
_is("resto del 10% y viejo → cola", bool(res[30]), True)
_is("resto del 60% y viejo → deuda", bool(res[31]), False)
_is("resto del 10% pero reciente → no es cola", bool(res[32]), False)

apagado = C.marcar_residuos(
    h[h["estado"].isin(["abierta", "parcial"])], ratio_max=-1.0
)
_is("con ratio_max=-1 no queda ninguna cola",
    int(apagado["es_residuo"].sum()), 0)
_is("apagarlo no deja el módulo mutado", C.RESIDUO_RATIO_MAX, 0.25)


# =====================================================================
print("\n5. El veto exige un monto MATERIAL")
# Con el umbral en $1 se vetaba al 44% de la cartera por restos de centavos.
def _cliente(nombre, cli, saldo_viejo, compra_mensual):
    """Cliente que compra todos los meses y arrastra una deuda vieja."""
    filas, pg = [], []
    idc = cli * 1000
    for m in range(1, 13):
        idc += 1
        filas.append({"id": idc, "cli": cli, "nombre": nombre,
                      "emision": f"2025-{m:02d}-01", "total": compra_mensual,
                      "saldo": 0.0})
        pg.append({"comp": idc, "cli": cli,
                   "fecha": f"2025-{m:02d}-25", "importe": compra_mensual})
    # la deuda vieja: una factura abierta de hace 300 días
    filas.append({"id": idc + 500, "cli": cli, "nombre": nombre,
                  "emision": "2025-09-01", "total": saldo_viejo,
                  "saldo": saldo_viejo})
    return filas, pg


f1, p1 = _cliente("PAGA BIEN CON RESTO CHICO", 1, 500.0, 100_000.0)
f2, p2 = _cliente("DEBE EN SERIO", 2, 200_000.0, 100_000.0)
h = C.armar_historial(_comp(f1 + f2), _pagos(p1 + p2), hoy=HOY)
feat = C.features_por_cliente(h, None, hoy=HOY)
sc = C.scorear(feat, C.ConfigScore())
por_cli = dict(zip(sc["id_cliente"], sc["banda"]))
_true("el resto chico NO veta", por_cli[1] != "E")
_is("la deuda material SÍ veta", por_cli[2], "E")


# =====================================================================
print("\n6. DSO: no se deja engañar por el orden de imputación")
# El caso real: un cliente cuyas facturas cerradas se pagan puntuales pero
# que arrastra una deuda vieja enorme. El atraso por factura dice "impecable";
# el DSO dice la verdad.
filas, pg = _cliente("ENGAÑOSO", 3, 1_200_000.0, 100_000.0)
h = C.armar_historial(_comp(filas), _pagos(pg), hoy=HOY)
feat = C.features_por_cliente(h, None, hoy=HOY)
r = feat[feat["id_cliente"] == 3].iloc[0]
_true("el atraso por factura lo muestra puntual", r["dpd_pond"] < 10)
_true("el DSO lo delata (más de 200 días)", r["dso"] > 200)
_true("el exceso sobre el plazo pactado es grande", r["exceso_dso"] > 150)
_true("hay capital inmovilizado medido", r["capital_excedido"] > 0)


# =====================================================================
print("\n7. Sin historia no se opina: banda S/D, no banda mala")
comp = _comp([{"id": 40, "cli": 9, "nombre": "NUEVO", "emision": "2026-07-01",
               "total": 5000.0, "saldo": 0.0}])
pagos = _pagos([{"comp": 40, "cli": 9, "fecha": "2026-07-20",
                 "importe": 5000.0}])
h = C.armar_historial(comp, pagos, hoy=HOY)
sc = C.scorear(C.features_por_cliente(h, None, hoy=HOY), C.ConfigScore())
_is("un cliente con 1 factura queda S/D", sc["banda"].iloc[0], "S/D")
pol = C.politica(sc, C.ConfigScore())
_is("S/D no recibe límite", float(pol["limite_sugerido"].iloc[0]), 0.0)
_is("S/D no recibe plazo", int(pol["plazo_sugerido"].iloc[0]), 0)


# =====================================================================
print("\n8. Límite y tasa")
cfg = C.ConfigScore()
filas, pg = _cliente("IMPECABLE", 4, 0.0, 100_000.0)
filas = [f for f in filas if f["total"] > 0]
h = C.armar_historial(_comp(filas), _pagos(pg), hoy=HOY)
feat = C.features_por_cliente(h, None, hoy=HOY)
pol = C.politica(C.scorear(feat, cfg), cfg)
r = pol.iloc[0]
banda = r["banda"]
if banda in cfg.plazo_por_banda and banda not in ("E", "S/D"):
    plazo = cfg.plazo_por_banda[banda]
    esperado = min(
        r["compra_mensual"] * (plazo / 30) * cfg.factor_limite[banda],
        r["compra_mensual"] * cfg.tope_meses_compra,
    )
    _eq(f"límite = exposición natural del plazo (banda {banda})",
        r["limite_sugerido"], round(esperado, 0), tol=1.0)
    _eq("tasa = costo de fondos + spread + prima",
        r["tasa_anual"],
        cfg.costo_fondos_anual + cfg.spread_anual + cfg.prima_riesgo[banda],
        tol=1e-4)
    _eq("el recargo cobra solo los días EXTRA sobre 30",
        r["recargo_pct"],
        round(r["tasa_anual"] * max(plazo - 30, 0) / 365 * 100, 2),
        tol=0.01)
    _true("el límite nunca supera el tope de meses de compra",
          r["limite_sugerido"] <= r["compra_mensual"] * cfg.tope_meses_compra + 1)

# El tope tiene que morder cuando el plazo es largo.
cfg2 = C.ConfigScore()
cfg2.tope_meses_compra = 1.0
pol2 = C.politica(C.scorear(feat, cfg2), cfg2)
_true("bajar el tope baja el límite",
      pol2["limite_sugerido"].iloc[0] <= pol["limite_sugerido"].iloc[0])


# =====================================================================
print("\n9. El score no se sale de escala")
_true("score dentro de [0, 100]",
      bool(((pol["score"] >= 0) & (pol["score"] <= 100)).all()))
_true("los pesos suman 100",
      abs(
          cfg.peso_dso + cfg.peso_dpd + cfg.peso_p90 + cfg.peso_puntualidad
          + cfg.peso_antiguedad + cfg.peso_volumen + cfg.peso_situacion - 100
      ) < 1e-9)
_true("ningún límite negativo", bool((pol["limite_sugerido"] >= 0).all()))

# Vacío no rompe.
vacio = C.armar_historial(_comp([]), _pagos([]), hoy=HOY)
_true("historial vacío no explota", len(vacio) == 0)


# =====================================================================
print("\n10. Una NC sin aplicar RESTA de la exposición (trampa de signo)")
# En Contabilium la NC trae `total` negativo pero `Saldo` POSITIVO, y no
# siempre: hay NC con el saldo en negativo. Sumar el campo crudo inflaba la
# exposición en vez de netearla, y con eso el DSO, el exceso y el capital
# excedido. Caso real: SODIMAC quedaba en 99 días de DSO en vez de 67.
comp = _comp(
    [
        {"id": 1, "cli": 1, "emision": "2026-06-01", "cond": "60 Cuenta Corriente",
         "total": 100_000.0, "saldo": 100_000.0},
        # misma NC, las dos convenciones de signo que devuelve el ERP
        {"id": 2, "cli": 1, "tipo": "NCF", "emision": "2026-06-02",
         "total": -30_000.0, "saldo": 30_000.0},
        {"id": 3, "cli": 1, "tipo": "NCF", "emision": "2026-06-03",
         "total": -20_000.0, "saldo": -20_000.0},
    ]
)
rnc = C.resumen_notas_credito(comp, hoy=HOY)
_eq("el crédito sin aplicar se normaliza en negativo, sea cual sea el signo",
    rnc.loc[1, "saldo_nc"], -50_000.0)
_eq("las NC del período también se normalizan", rnc.loc[1, "nc_12m"], -50_000.0)

h10 = C.armar_historial(comp, _pagos([]), hoy=HOY)
f10 = C.features_por_cliente(h10, None, hoy=HOY, resumen_nc=rnc).set_index(
    "id_cliente"
)
_eq("exposición = saldo de facturas MENOS el crédito sin aplicar",
    f10.loc[1, "exposicion_neta"], 50_000.0)
_true("la exposición nunca sale mayor que el saldo de las facturas",
      bool((f10["exposicion_neta"] <= f10["saldo_vivo"] + 1e-6).all()))


# =====================================================================
print("\n11. Días sin pagar: una NC no es un pago")
comp11 = _comp(
    [{"id": 1, "cli": 1, "emision": "2026-01-05", "total": 50_000.0,
      "saldo": 50_000.0}]
)
formas = pd.DataFrame(
    [
        {"id_recibo": 1, "id_cliente": 1, "fecha_pago": pd.Timestamp("2026-02-10"),
         "forma": "Transferencia", "importe": 10_000.0, "es_nota_credito": False},
        {"id_recibo": 2, "id_cliente": 1, "fecha_pago": pd.Timestamp("2026-08-01"),
         "forma": "Nota de credito", "importe": 5_000.0, "es_nota_credito": True},
    ]
)
f11 = C.features_por_cliente(
    C.armar_historial(comp11, _pagos([]), hoy=HOY), formas, hoy=HOY
).set_index("id_cliente")
_is("el último pago es la transferencia, no la NC posterior",
    f11.loc[1, "ultimo_pago"], pd.Timestamp("2026-02-10"))
_eq("días sin pagar se cuentan desde ese pago real",
    f11.loc[1, "dias_sin_pago"], (HOY - pd.Timestamp("2026-02-10")).days)

f11b = C.features_por_cliente(
    C.armar_historial(comp11, _pagos([]), hoy=HOY), None, hoy=HOY
).set_index("id_cliente")
_true("sin formas de pago, las columnas existen igual",
      "dias_sin_pago" in f11b.columns and "ultimo_pago" in f11b.columns)


# =====================================================================
print("\n" + "=" * 60)
if _fallos:
    print(f"FALLARON {len(_fallos)} casos:")
    for f in _fallos:
        print(f"  - {f}")
    sys.exit(1)
print("TODO OK")
