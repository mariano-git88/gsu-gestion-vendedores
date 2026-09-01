"""
test_deudora.py — Blinda el estado de cuenta corriente (`deudora.py`).

Esta pantalla la mira un vendedor parado adelante del cliente. Un número mal
acá no es un reporte feo: es una discusión perdida en el mostrador. Los casos
son los que ya se rompieron una vez en `credito.py` (el signo de las notas de
crédito, el vencimiento del ERP, los DataFrames vacíos sin columnas) más los
propios de la deudora.

Se corre sin API ni pytest:  python3 tests/test_deudora.py
(desde la raíz del repo). Datos 100% sintéticos.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import credito as C  # noqa: E402
import deudora as D  # noqa: E402

_fallos = []
HOY = pd.Timestamp("2026-08-31")


def _is(label, got, exp):
    ok = got == exp
    print(f"  [{'OK' if ok else 'FALLA'}] {label}: got={got!r} exp={exp!r}")
    if not ok:
        _fallos.append(label)


def _eq(label, got, exp, tol=1e-6):
    ok = abs(float(got) - float(exp)) < tol
    print(f"  [{'OK' if ok else 'FALLA'}] {label}: got={got:,.2f} exp={exp:,.2f}")
    if not ok:
        _fallos.append(label)


def _true(label, cond):
    print(f"  [{'OK' if cond else 'FALLA'}] {label}")
    if not cond:
        _fallos.append(label)


def _comp(rows):
    """df de comprobantes con el schema de `credito_api.parse_comprobantes`."""
    return pd.DataFrame(
        [
            {
                "id": r["id"],
                "id_cliente": r.get("cli", 1),
                "razon_social": r.get("nombre", "FERRETERIA TEST"),
                "tipo": r.get("tipo", "FAC"),
                "numero": r.get("numero", f"A-{r['id']:08d}"),
                "emision": pd.Timestamp(r["emision"]),
                "vencimiento_erp": pd.Timestamp(r["emision"])
                + pd.Timedelta(days=30),
                "cond_venta": r.get("cond", "30 Cuenta Corriente"),
                "total": r.get("total", 1000.0),
                "saldo": r.get("saldo", 0.0),
                "moneda": 794,
                "tc": 1.0,
                "id_vendedor": r.get("vend", 1),
            }
            for r in rows
        ]
    )


def _pagos(rows):
    return pd.DataFrame(
        [
            {
                "id_recibo": 500 + i,
                "fecha_pago": pd.Timestamp(r["fecha"]),
                "id_cliente": r.get("cli", 1),
                "id_comprobante": r["comp"],
                "importe": r["importe"],
            }
            for i, r in enumerate(rows, start=1)
        ]
    )


def _clientes(rows):
    """df del maestro con el schema de `credito_api.parse_clientes`."""
    return pd.DataFrame(
        [
            {
                "id_cliente": r["cli"],
                "razon_social": r.get("nombre", "FERRETERIA TEST"),
                "documento": r.get("doc", "218826790017"),
                "tipo_doc": "RUT",
                "id_vendedor": r.get("vend"),
                "ciudad": r.get("ciudad", "Montevideo"),
                "email": "",
                "telefono": "099111222",
            }
            for r in rows
        ]
    )


# =====================================================================
print("\n1. El extracto arma debe y haber en el lado correcto")
comp = _comp([
    {"id": 1, "emision": "2026-07-01", "total": 10_000.0, "saldo": 10_000.0},
    {"id": 2, "emision": "2026-07-10", "total": -1_000.0, "saldo": 1_000.0,
     "tipo": "NCF"},
])
mov = D.armar_movimientos(comp, _pagos([{"fecha": "2026-07-15", "comp": 1,
                                         "importe": 4_000.0}]), hoy=HOY)
_is("hay 3 movimientos", len(mov), 3)
_eq("la factura carga en el debe", mov.loc[0, "debe"], 10_000.0)
_eq("y no toca el haber", mov.loc[0, "haber"], 0.0)
_is("la nota de crédito es del tipo correcto",
    mov.loc[1, "tipo_mov"], D.TIPO_NOTA_CREDITO)
_eq("la NC carga en el haber, en positivo", mov.loc[1, "haber"], 1_000.0)
_eq("el recibo carga en el haber", mov.loc[2, "haber"], 4_000.0)
_is("y queda identificado contra su factura", mov.loc[2, "comprobante"],
    "A-00000001")

print("\n2. Una NC con el total en POSITIVO tampoco se suma a la deuda")
# ImporteTotalNeto viene negativo en las NC... casi siempre. El extracto no
# puede depender del signo que traiga el ERP (errors.md 2026-08-13).
comp2 = _comp([{"id": 3, "emision": "2026-07-10", "total": 1_000.0,
                "saldo": 1_000.0, "tipo": "NCF"}])
mov2 = D.armar_movimientos(comp2, None, hoy=HOY)
_eq("la NC de signo invertido igual va al haber", mov2.loc[0, "haber"], 1_000.0)
_eq("y no ensucia el debe", mov2.loc[0, "debe"], 0.0)

print("\n3. Los días de atraso salen de la condición, no del campo del ERP")
comp3 = _comp([
    {"id": 10, "cli": 1, "emision": "2026-06-01", "cond": "30 Cuenta Corriente",
     "total": 5_000.0, "saldo": 5_000.0},
    {"id": 11, "cli": 1, "emision": "2026-06-01", "cond": "90 Cuenta Corriente",
     "total": 5_000.0, "saldo": 5_000.0},
])
mov3 = D.armar_movimientos(comp3, None, hoy=HOY).set_index("id_comprobante")
_is("a 30 días vence el 1-jul",
    str(mov3.loc[10, "vencimiento"].date()), "2026-07-01")
_is("a 90 días vence el 30-ago (el ERP diría 1-jul)",
    str(mov3.loc[11, "vencimiento"].date()), "2026-08-30")
_is("la de 30 días arrastra 61 de atraso", int(mov3.loc[10, "dias_vencido"]), 61)
_is("la de 90 días recién vence: 1 día", int(mov3.loc[11, "dias_vencido"]), 1)

print("\n4. Una factura ya cobrada no arrastra atraso")
comp4 = _comp([{"id": 20, "emision": "2026-01-01", "total": 900.0, "saldo": 0.0}])
mov4 = D.armar_movimientos(comp4, None, hoy=HOY)
_true("dias_vencido queda vacío en la cobrada",
      pd.isna(mov4.loc[0, "dias_vencido"]))
_eq("y su saldo pendiente es cero", mov4.loc[0, "saldo_pendiente"], 0.0)

print("\n5. Dentro del mismo día, primero la factura y después el pago")
comp5 = _comp([{"id": 30, "emision": "2026-07-05", "total": 2_000.0,
                "saldo": 0.0}])
mov5 = D.armar_movimientos(
    comp5, _pagos([{"fecha": "2026-07-05", "comp": 30, "importe": 2_000.0}]),
    hoy=HOY)
_is("la factura va primero", mov5.loc[0, "tipo_mov"], D.TIPO_FACTURA)
_is("el recibo después", mov5.loc[1, "tipo_mov"], D.TIPO_RECIBO)

print("\n6. El pago a cuenta se muestra como tal, no como factura fantasma")
mov6 = D.armar_movimientos(
    _comp([{"id": 40, "emision": "2026-07-01", "total": 3_000.0,
            "saldo": 3_000.0}]),
    _pagos([{"fecha": "2026-07-20", "comp": 0, "importe": 500.0}]),
    hoy=HOY)
rec = mov6[mov6["tipo_mov"] == D.TIPO_RECIBO]
_is("aparece rotulado", rec.iloc[0]["comprobante"], "Pago a cuenta")

print("\n7. El resumen resta el crédito a favor en vez de sumarlo")
# El bug de los $2,5M: el Saldo de la NC viene positivo y se sumaba a la
# exposición, así que un crédito A FAVOR del cliente le inflaba la deuda.
comp7 = _comp([
    {"id": 50, "cli": 7, "emision": "2026-07-01", "total": 10_000.0,
     "saldo": 10_000.0},
    {"id": 51, "cli": 7, "emision": "2026-07-05", "total": -2_000.0,
     "saldo": 2_000.0, "tipo": "NCF"},
])
res7 = D.resumen_por_cliente(comp7, None, None, hoy=HOY).set_index("id_cliente")
_eq("crédito a favor detectado", res7.loc[7, "credito_a_favor"], 2_000.0)
_eq("deuda = 10.000 − 2.000", res7.loc[7, "deuda_total"], 8_000.0)

print("\n7b. El vencido neto nunca supera a la deuda ni se va abajo de cero")
# Con NC sin aplicar, el vencido bruto puede pasar a la deuda total: leído
# en una tabla, "debe 138k, vencido 141k" no tiene sentido para nadie.
comp7b = _comp([
    {"id": 55, "cli": 71, "emision": "2026-01-01", "total": 5_000.0,
     "saldo": 5_000.0},                                    # vencida hace rato
    {"id": 56, "cli": 71, "emision": "2026-07-05", "total": -9_000.0,
     "saldo": 9_000.0, "tipo": "NCF"},                     # crédito mayor
])
r7b = D.resumen_por_cliente(comp7b, None, None, hoy=HOY).set_index("id_cliente")
_eq("el vencido bruto sigue siendo comparable con el scoring",
    r7b.loc[71, "vencido"], 5_000.0)
_eq("el neto se planta en cero, no en negativo",
    r7b.loc[71, "vencido_neto"], 0.0)
_eq("y la deuda queda a favor del cliente",
    r7b.loc[71, "deuda_total"], -4_000.0)

print("\n8. Y con la NC de signo invertido, el neteo es el mismo")
comp8 = _comp([
    {"id": 60, "cli": 8, "emision": "2026-07-01", "total": 10_000.0,
     "saldo": 10_000.0},
    {"id": 61, "cli": 8, "emision": "2026-07-05", "total": -2_000.0,
     "saldo": -2_000.0, "tipo": "NCF"},
])
res8 = D.resumen_por_cliente(comp8, None, None, hoy=HOY).set_index("id_cliente")
_eq("la NC con saldo negativo también resta",
    res8.loc[8, "deuda_total"], 8_000.0)

print("\n9. Un cliente que SOLO tiene crédito a favor no desaparece")
comp9 = _comp([{"id": 70, "cli": 9, "emision": "2026-07-05", "total": -500.0,
                "saldo": 500.0, "tipo": "NCF", "nombre": "BARRACA CREDITO"}])
res9 = D.resumen_por_cliente(comp9, None, None, hoy=HOY)
_is("aparece en el listado", len(res9), 1)
_eq("con saldo negativo", res9.loc[0, "deuda_total"], -500.0)
_is("y con su nombre, que sale del comprobante",
    res9.loc[0, "razon_social"], "BARRACA CREDITO")

print("\n10. Los tramos de antigüedad cortan donde dicen")
comp10 = _comp([
    {"id": 80, "cli": 10, "emision": "2026-08-15", "total": 100.0,
     "saldo": 100.0},   # vence 14-sep → al día
    {"id": 81, "cli": 10, "emision": "2026-07-15", "total": 200.0,
     "saldo": 200.0},   # vence 14-ago → 17 días
    {"id": 82, "cli": 10, "emision": "2026-06-01", "total": 400.0,
     "saldo": 400.0},   # vence 1-jul → 61 días
    {"id": 83, "cli": 10, "emision": "2026-01-01", "total": 800.0,
     "saldo": 800.0},   # vence 31-ene → 212 días
])
r10 = D.resumen_por_cliente(comp10, None, None, hoy=HOY).set_index("id_cliente")
_eq("al día", r10.loc[10, "al_dia"], 100.0)
_eq("1 a 30", r10.loc[10, "b_1_30"], 200.0)
_eq("61 a 90", r10.loc[10, "b_61_90"], 400.0)
_eq("más de 90", r10.loc[10, "b_90_mas"], 800.0)
_eq("el vencido excluye lo que está al día", r10.loc[10, "vencido"], 1_400.0)
_eq("la deuda total incluye todo", r10.loc[10, "deuda_total"], 1_500.0)
_is("el peor tramo es el más viejo", r10.loc[10, "peor_tramo"], "b_90_mas")

print("\n11. Las grandes superficies cuentan desde el cierre del mes")
comp11 = _comp([{"id": 90, "cli": 555, "emision": "2026-06-10",
                 "cond": "60 Cuenta Corriente", "total": 1_000.0,
                 "saldo": 1_000.0, "nombre": "GRAN SUPERFICIE TEST"}])
m_norm = D.armar_movimientos(comp11, None, hoy=HOY,
                             clientes_fin_de_mes=frozenset())
m_fdm = D.armar_movimientos(comp11, None, hoy=HOY,
                            clientes_fin_de_mes=frozenset({555}))
_is("sin la regla vence el 9-ago",
    str(m_norm.loc[0, "vencimiento"].date()), "2026-08-09")
_is("con la regla vence el 29-ago (cierre de junio + 60)",
    str(m_fdm.loc[0, "vencimiento"].date()), "2026-08-29")

print("\n12. El vendedor sale del maestro de clientes")
VEND = {1: "nicolas@suprabond.com.uy", 2: "gabriel@suprabond.com.uy"}
cli12 = D.agregar_vendedor(
    _clientes([
        {"cli": 100, "vend": 1},
        {"cli": 101, "vend": None},          # sin asignar
        {"cli": 102, "vend": 9},             # id que no está mapeado
        {"cli": 103, "vend": 7},             # cuenta operativa
    ]),
    VEND, excluidos=frozenset({7}),
).set_index("id_cliente")
_is("mapeado", cli12.loc[100, "vendedor"], "nicolas@suprabond.com.uy")
_is("sin vendedor queda etiquetado", cli12.loc[101, "vendedor"], D.SIN_VENDEDOR)
_is("un id sin mapear queda VISIBLE, no se pierde",
    cli12.loc[102, "vendedor"], "ID_9")
_is("la cuenta operativa no es cartera de nadie",
    cli12.loc[103, "vendedor"], D.SIN_VENDEDOR)

print("\n13. Los clientes sin vendedor siguen apareciendo en la deudora")
# Al 18-ago eran 148 clientes con $1,7M vencido. Si se filtran, esa plata
# no aparece en la pantalla de nadie.
comp13 = _comp([
    {"id": 110, "cli": 100, "emision": "2026-01-01", "total": 5_000.0,
     "saldo": 5_000.0},
    {"id": 111, "cli": 101, "emision": "2026-01-01", "total": 9_000.0,
     "saldo": 9_000.0},
])
cli13 = D.agregar_vendedor(
    _clientes([{"cli": 100, "vend": 1}, {"cli": 101, "vend": None}]), VEND)
res13 = D.resumen_por_cliente(comp13, None, cli13, hoy=HOY)
tot13 = D.totales_por_vendedor(res13).set_index("vendedor")
_is("están los dos grupos", len(tot13), 2)
_eq("y la plata sin dueño está contada",
    tot13.loc[D.SIN_VENDEDOR, "vencido"], 9_000.0)
_eq("el % vencido se calcula sobre la deuda del vendedor",
    tot13.loc["nicolas@suprabond.com.uy", "%_vencido"], 100.0)

print("\n14. El documento se conserva como string")
cli14 = _clientes([{"cli": 120, "vend": 1, "doc": "0012345"}])
res14 = D.resumen_por_cliente(
    _comp([{"id": 130, "cli": 120, "emision": "2026-07-01", "total": 100.0,
            "saldo": 100.0}]),
    None, D.agregar_vendedor(cli14, VEND), hoy=HOY)
_is("los ceros a la izquierda sobreviven",
    res14.loc[0, "documento"], "0012345")

print("\n15. Sin datos no explota, y devuelve las columnas igual")
# Un DataFrame vacío SIN columnas es una bomba de tiempo: el KeyError
# aparece lejos de donde se originó (errors.md).
vacio = pd.DataFrame()
mov15 = D.armar_movimientos(vacio, None, hoy=HOY)
res15 = D.resumen_por_cliente(vacio, None, None, hoy=HOY)
tot15 = D.totales_por_vendedor(res15)
_is("movimientos trae el schema completo", list(mov15.columns), D.COLS_MOV)
_is("el resumen también", list(res15.columns), D.COLS_RESUMEN)
_is("y los totales por vendedor", len(tot15), 0)
_true("los tramos están declarados en los totales",
      all(c in tot15.columns for c in D.COLS_BUCKET))

print("\n16. Un cliente sin ninguna factura abierta no rompe el resumen")
comp16 = _comp([{"id": 140, "cli": 16, "emision": "2026-01-01", "total": 100.0,
                 "saldo": 0.0}])
res16 = D.resumen_por_cliente(comp16, None, None, hoy=HOY)
_is("no aparece nadie con deuda", len(res16), 0)
_is("pero el schema se mantiene", list(res16.columns), D.COLS_RESUMEN)

print("\n17. El extracto filtrado deja solo lo que hay que ir a cobrar")
comp17 = _comp([
    {"id": 150, "cli": 17, "emision": "2026-01-01", "total": 100.0,
     "saldo": 100.0},
    {"id": 151, "cli": 17, "emision": "2026-02-01", "total": 200.0,
     "saldo": 0.0},
    {"id": 152, "cli": 99, "emision": "2026-02-01", "total": 300.0,
     "saldo": 300.0},
])
mov17 = D.armar_movimientos(comp17, None, hoy=HOY)
_is("el extracto completo del cliente trae sus dos facturas",
    len(D.extracto_de_cliente(mov17, 17)), 2)
abiertas17 = D.extracto_de_cliente(mov17, 17, solo_abiertas=True)
_is("filtrado deja una sola", len(abiertas17), 1)
_is("y es la que tiene saldo", int(abiertas17.loc[0, "id_comprobante"]), 150)

print("\n18. Última compra y último pago")
comp18 = _comp([
    {"id": 160, "cli": 18, "emision": "2026-05-01", "total": 100.0,
     "saldo": 100.0},
    {"id": 161, "cli": 18, "emision": "2026-08-01", "total": 200.0,
     "saldo": 200.0},
])
res18 = D.resumen_por_cliente(
    comp18,
    _pagos([{"fecha": "2026-06-15", "cli": 18, "comp": 160, "importe": 50.0},
            {"fecha": "2026-08-20", "cli": 18, "comp": 161, "importe": 30.0}]),
    None, hoy=HOY).set_index("id_cliente")
_is("la última compra es la factura más nueva",
    str(res18.loc[18, "ultima_compra"].date()), "2026-08-01")
_is("el último pago es el recibo más nuevo",
    str(res18.loc[18, "ultimo_pago"].date()), "2026-08-20")

print("\n18b. Una ficha repetida no duplica al cliente en la deudora")
# El maestro se arma juntando /api/clientes/search con /api/proveedores/search
# porque hay clientes cargados como proveedores que el primero no devuelve
# (SODIMAC entre ellos, con $1,44M). Si esa unión dejara un id repetido, el
# cliente aparecería dos veces y su deuda se contaría doble.
cli18b = pd.concat([
    _clientes([{"cli": 180, "vend": 1, "doc": "111"}]),
    _clientes([{"cli": 180, "vend": 1, "doc": "111"}]),   # la misma ficha
], ignore_index=True)
res18b = D.resumen_por_cliente(
    _comp([{"id": 165, "cli": 180, "emision": "2026-07-01", "total": 400.0,
            "saldo": 400.0}]),
    None, D.agregar_vendedor(cli18b, VEND), hoy=HOY)
_is("el cliente aparece una sola vez", len(res18b), 1)
_eq("y su deuda no se cuenta dos veces", res18b.loc[0, "deuda_total"], 400.0)

print("\n19. La deudora usa el mismo vencimiento que el scoring")
# Si estas dos difieren, el mismo cliente tiene dos antigüedades distintas
# según qué pantalla se abra. Ya nos pasó entre metrics.py y credito.py.
comp19 = _comp([{"id": 170, "cli": 19, "emision": "2026-05-01",
                 "cond": "60 Cuenta Corriente", "total": 1_000.0,
                 "saldo": 1_000.0}])
h19 = C.armar_historial(comp19, _pagos([]), hoy=HOY)
m19 = D.armar_movimientos(comp19, None, hoy=HOY)
_is("mismo vencimiento que credito.armar_historial",
    str(m19.loc[0, "vencimiento"].date()),
    str(h19.loc[0, "vencimiento"].date()))


# =====================================================================
print("\n" + "=" * 60)
if _fallos:
    print(f"FALLARON {len(_fallos)} casos:")
    for f in _fallos:
        print(f"  - {f}")
    sys.exit(1)
print("TODO OK")
