"""
test_cuotas.py — Blinda la división de facturas en cuotas (`cuotas.py`).

Dividir una factura no puede cambiar lo que el cliente debe: la suma de las
cuotas pendientes tiene que dar siempre el saldo del ERP. Si eso se rompe,
la deudora de un cliente queda desbalanceada y el error aparece lejos.

Se corre sin API ni pytest:  python3 tests/test_cuotas.py
(desde la raíz del repo). Datos 100% sintéticos.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cuotas as CU  # noqa: E402
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
    return pd.DataFrame([
        {
            "id": r["id"], "id_cliente": r.get("cli", 1),
            "razon_social": "FERRETERIA TEST", "tipo": r.get("tipo", "FAC"),
            "numero": f"A-{r['id']:08d}",
            "emision": pd.Timestamp(r["emision"]),
            "vencimiento_erp": pd.Timestamp(r["emision"]) + pd.Timedelta(days=30),
            "cond_venta": r.get("cond", "30/60/90 Cuenta Corriente"),
            "total": r.get("total", 3000.0), "saldo": r.get("saldo", 3000.0),
            "moneda": 794, "tc": 1.0, "id_vendedor": 1,
        }
        for r in rows
    ])


# =====================================================================
print("\n1. Los plazos salen del nombre de la condición")
_is("30/60/90", CU.plazos_de_condicion("30/60/90 Cuenta Corriente"), [30, 60, 90])
_is("30/60", CU.plazos_de_condicion("30/60 Cuenta Corriente"), [30, 60])
_is("60/90", CU.plazos_de_condicion("60/90 Cuenta Corriente"), [60, 90])
_is("una condición simple NO se divide",
    CU.plazos_de_condicion("60 Cuenta Corriente"), [])
_is("Contado tampoco", CU.plazos_de_condicion("Contado"), [])
_is("ni una condición vacía", CU.plazos_de_condicion(None), [])

print("\n2. El reparto de importes cierra exacto")
# Tres cuotas de 1.000,33 suman 3.000,99 y no 3.001: la diferencia va a la
# última, o la factura deja de cerrar por un centavo que nadie encuentra.
r = CU.repartir_importe(3001.0, 3)
_eq("la suma da el total", sum(r), 3001.0)
_is("y la última absorbe el redondeo", r, [1000.33, 1000.33, 1000.34])
_eq("un importe que divide justo", sum(CU.repartir_importe(3000.0, 3)), 3000.0)
_is("una sola parte devuelve el total entero",
    CU.repartir_importe(1234.56, 1), [1234.56])
_is("cero partes no rompe", CU.repartir_importe(100.0, 0), [])

print("\n3. La sugerencia cuenta los días desde la emisión")
s = CU.sugerir(10, "30/60/90 Cuenta Corriente", "2026-06-01", 3000.0)
_is("son tres cuotas", len(s), 3)
_is("la 1 vence a los 30", str(s.loc[0, "vencimiento"].date()), "2026-07-01")
_is("la 2 a los 60", str(s.loc[1, "vencimiento"].date()), "2026-07-31")
_is("la 3 a los 90", str(s.loc[2, "vencimiento"].date()), "2026-08-30")
_eq("y suman el total", s["importe"].sum(), 3000.0)
_true("una condición simple no sugiere nada",
      CU.sugerir(11, "60 Cuenta Corriente", "2026-06-01", 1000.0).empty)

print("\n4. El saldo se aplica de la cuota más vieja a la más nueva")
c = CU.sugerir(20, "30/60/90 Cuenta Corriente", "2026-06-01", 3000.0)
e = CU.estado(c, saldo_erp=1500.0)
_eq("la cuota 1 quedó cubierta", e.loc[0, "pendiente"], 0.0)
_eq("la 2 debe la mitad", e.loc[1, "pendiente"], 500.0)
_eq("la 3 debe todo", e.loc[2, "pendiente"], 1000.0)
_eq("y el total pendiente es el saldo del ERP", e["pendiente"].sum(), 1500.0)

print("\n5. Dividir una factura NUNCA cambia lo que el cliente debe")
for saldo in (0.0, 1.0, 999.99, 1500.0, 2999.5, 3000.0):
    e = CU.estado(c, saldo_erp=saldo)
    _eq(f"saldo {saldo:,.2f} → las cuotas suman lo mismo",
        e["pendiente"].sum(), saldo)

print("\n6. Casos raros de saldo")
_eq("saldo mayor que las cuotas: el excedente va a la última",
    CU.estado(c, saldo_erp=5000.0)["pendiente"].sum(), 5000.0)
_eq("saldo negativo se trata como cero",
    CU.estado(c, saldo_erp=-100.0)["pendiente"].sum(), 0.0)
_true("sin cuotas devuelve vacío", CU.estado(pd.DataFrame(), 100.0).empty)

print("\n7. La validación frena lo que rompería la cuenta")
ok = CU.sugerir(30, "30/60 Cuenta Corriente", "2026-06-01", 2000.0)
_is("una división correcta no tiene problemas", CU.validar(ok, 2000.0), [])
malo = ok.copy()
malo.loc[0, "importe"] = 500.0
_true("si no cierran con el total, avisa", len(CU.validar(malo, 2000.0)) == 1)
desordenado = ok.copy()
desordenado.loc[0, "vencimiento"] = pd.Timestamp("2027-01-01")
_true("si los vencimientos no van en orden, avisa",
      len(CU.validar(desordenado, 2000.0)) == 1)
cero = ok.copy()
cero.loc[0, "importe"] = 0.0
_true("una cuota en cero se rechaza", len(CU.validar(cero, 1000.0)) >= 1)
_true("sin cuotas también avisa", len(CU.validar(pd.DataFrame(), 100.0)) == 1)

print("\n8. Al leer la planilla gana la última carga de cada factura")
plan = pd.DataFrame([
    # carga vieja: dos cuotas
    {"timestamp": "2026-08-01T10:00:00", "id_comprobante": "50",
     "nro_cuota": "1", "vencimiento": "2026-09-01", "importe": "500", "anulado": ""},
    {"timestamp": "2026-08-01T10:00:00", "id_comprobante": "50",
     "nro_cuota": "2", "vencimiento": "2026-10-01", "importe": "500", "anulado": ""},
    # corrección: tres cuotas
    {"timestamp": "2026-08-20T09:00:00", "id_comprobante": "50",
     "nro_cuota": "1", "vencimiento": "2026-09-01", "importe": "300", "anulado": ""},
    {"timestamp": "2026-08-20T09:00:00", "id_comprobante": "50",
     "nro_cuota": "2", "vencimiento": "2026-10-01", "importe": "300", "anulado": ""},
    {"timestamp": "2026-08-20T09:00:00", "id_comprobante": "50",
     "nro_cuota": "3", "vencimiento": "2026-11-01", "importe": "400", "anulado": ""},
    # otra factura, anulada: vuelve a mostrarse entera
    {"timestamp": "2026-08-21T09:00:00", "id_comprobante": "51",
     "nro_cuota": "1", "vencimiento": "2026-09-01", "importe": "100", "anulado": "SI"},
])
idx = CU.indexar(plan)
_is("la factura corregida queda con 3 cuotas", len(idx[50]), 3)
_eq("con los importes nuevos", idx[50]["importe"].sum(), 1000.0)
_true("la anulada no aparece", 51 not in idx)
_true("una planilla vacía no rompe", CU.indexar(pd.DataFrame()) == {})

print("\n9. En la deudora, cada cuota cae en SU tramo de antigüedad")
# Sin dividir, una factura a 30/60/90 usa el vencimiento promedio (60 días) y
# toda la plata cae en un solo tramo. Dividida, cada cuota cae donde le toca.
comp9 = _comp([{"id": 60, "cli": 9, "emision": "2026-05-01", "total": 3000.0,
                "saldo": 3000.0}])
sin = D.resumen_por_cliente(comp9, None, None, hoy=HOY).set_index("id_cliente")
_eq("sin dividir: todo junto en un tramo", sin.loc[9, "b_61_90"], 3000.0)

idx9 = {60: CU.sugerir(60, "30/60/90 Cuenta Corriente", "2026-05-01", 3000.0)}
con = D.resumen_por_cliente(comp9, None, None, hoy=HOY,
                            cuotas_idx=idx9).set_index("id_cliente")
_eq("dividida: la cuota de mayo cae en +90", con.loc[9, "b_90_mas"], 1000.0)
_eq("la de junio en 61-90", con.loc[9, "b_61_90"], 1000.0)
_eq("la de julio en 31-60", con.loc[9, "b_31_60"], 1000.0)
_eq("y la deuda total no cambió", con.loc[9, "deuda_total"], 3000.0)
_eq("el vencido tampoco", con.loc[9, "vencido"], sin.loc[9, "vencido"])

print("\n10. El extracto muestra una línea por cuota")
mov = D.armar_movimientos(comp9, None, hoy=HOY, cuotas_idx=idx9)
fac = mov[mov["tipo_mov"] == D.TIPO_FACTURA]
_is("tres líneas", len(fac), 3)
_is("numeradas", list(fac["cuota"]), ["1/3", "2/3", "3/3"])
_is("en orden de vencimiento",
    list(fac["vencimiento"].dt.strftime("%Y-%m-%d")),
    ["2026-05-31", "2026-06-30", "2026-07-30"])
_eq("y los importes suman la factura", fac["debe"].sum(), 3000.0)

print("\n11. Con una cuota ya cobrada, esa línea no arrastra atraso")
idx11 = {61: CU.sugerir(61, "30/60/90 Cuenta Corriente", "2026-05-01", 3000.0)}
comp11 = _comp([{"id": 61, "cli": 11, "emision": "2026-05-01", "total": 3000.0,
                 "saldo": 2000.0}])
mov11 = D.armar_movimientos(comp11, None, hoy=HOY, cuotas_idx=idx11)
f11 = mov11[mov11["tipo_mov"] == D.TIPO_FACTURA].reset_index(drop=True)
_eq("la cuota 1 quedó en cero", f11.loc[0, "saldo_pendiente"], 0.0)
_true("y sin días de atraso", pd.isna(f11.loc[0, "dias_vencido"]))
_eq("las otras dos siguen abiertas",
    f11.loc[1, "saldo_pendiente"] + f11.loc[2, "saldo_pendiente"], 2000.0)

print("\n12. Una factura sin cuotas cargadas sigue igual que antes")
comp12 = _comp([{"id": 70, "cli": 12, "emision": "2026-05-01", "total": 3000.0,
                 "saldo": 3000.0}])
a = D.armar_movimientos(comp12, None, hoy=HOY)
b = D.armar_movimientos(comp12, None, hoy=HOY, cuotas_idx={999: pd.DataFrame()})
_is("mismo extracto con y sin índice de cuotas ajeno", len(a), len(b))
_is("y sigue siendo una sola línea", len(a), 1)


# =====================================================================
print("\n" + "=" * 60)
if _fallos:
    print(f"FALLARON {len(_fallos)} casos:")
    for f in _fallos:
        print(f"  - {f}")
    sys.exit(1)
print("TODO OK")
