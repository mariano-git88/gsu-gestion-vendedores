"""
test_comisiones_v12.py — Blinda la fórmula de compensación v1.2 (tramos +
bono trimestral + sueldo fijo + retro por tramos). Plata real: este test es
la red de seguridad del invariante para futuros cambios en commissions.py.
Ver _learning/formula_compensacion_v1.2.md.

Se corre sin API ni pytest:  python3 tests/test_comisiones_v12.py
(desde la raíz del repo). Datos 100% sintéticos.
"""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openpyxl import Workbook

import comisiones_ajuste as A
import commissions as C
import pandas as pd

_fallos = []


def _eq(label, got, exp, tol=1e-6):
    ok = abs(got - exp) < tol
    print(f"  [{'OK' if ok else 'FALLA'}] {label}: got={got:,.4f} exp={exp:,.4f}")
    if not ok:
        _fallos.append(label)


def _is(label, got, exp):
    ok = got == exp
    print(f"  [{'OK' if ok else 'FALLA'}] {label}: got={got} exp={exp}")
    if not ok:
        _fallos.append(label)


def test_tramos_venta():
    print("Comisión venta v1.2 (umbral 600k, tier 1.5M, 2,35%/5%):")
    _eq("vn=500k", C.comision_venta(500_000), 0)
    _eq("vn=600k (en umbral)", C.comision_venta(600_000), 0)
    _eq("vn=1.0M", C.comision_venta(1_000_000), 9_400)
    _eq("vn=1.5M", C.comision_venta(1_500_000), 21_150)
    _eq("vn=1.6M (solo 100k al 5%)", C.comision_venta(1_600_000), 26_150)
    _eq("vn=2.0M (tier alto)", C.comision_venta(2_000_000), 46_150)


def test_tramos_cobranza():
    print("Comisión cobranza v1.2 (umbral 700k, tier 1.5M, 3%/4%):")
    _eq("co=700k (en umbral)", C.comision_cobranza(700_000), 0)
    _eq("co=1.0M", C.comision_cobranza(1_000_000), 9_000)
    _eq("co=1.5M", C.comision_cobranza(1_500_000), 24_000)
    _eq("co=2.0M (tier alto)", C.comision_cobranza(2_000_000), 44_000)


def test_dual_run_v1():
    print("Dual-run: esquema v1 (legacy plano):")
    _eq("venta v1 1.0M", C.comision_venta(1_000_000, "v1"), 23_500)
    _eq("cobranza v1 1.0M", C.comision_cobranza(1_000_000, "v1"), 30_000)


def test_clasificar_pilar():
    print("Clasificación de pilar (umbral venta 600k):")
    _is("3 plenos → A", C.clasificar_pilar([700_000, 800_000, 900_000], 600_000), "A")
    _is("1 pleno + avg≥50% → B", C.clasificar_pilar([700_000, 500_000, 500_000], 600_000), "B")
    _is("0 plenos → —", C.clasificar_pilar([500_000, 500_000, 500_000], 600_000), "—")
    _is("avg=50% pero 0 plenos → —", C.clasificar_pilar([300_000, 300_000, 300_000], 600_000), "—")
    _is("1 mes fuerte → B", C.clasificar_pilar([200_000, 200_000, 1_000_000], 600_000), "B")


def test_bono_y_licencia():
    print("Bono trimestral (Cat A/A, 1M los 3 meses):")
    b = C.compute_bono_trimestral(
        {"V": {"ventas_netas": [1_000_000] * 3, "cobranzas": [1_000_000] * 3}}
    )["V"]
    _is("cat_venta A", b["cat_venta"], "A")
    _is("cat_cobranza A", b["cat_cobranza"], "A")
    _eq("com_venta_trim", b["com_venta_trim"], 28_200)
    _eq("com_cobranza_trim", b["com_cobranza_trim"], 27_000)
    _eq("bono_venta", b["bono_venta"], 2_820)
    _eq("bono_cobranza", b["bono_cobranza"], 4_050)
    _eq("bono_total", b["bono_total"], 6_870)

    print("Licencia: mes 2 (idx 1) se sustituye por promedio de los otros:")
    b2 = C.compute_bono_trimestral(
        {"V": {"ventas_netas": [1_000_000, 0, 1_000_000],
               "cobranzas": [1_000_000, 0, 1_000_000], "licencia_meses": {1}}}
    )["V"]
    _is("cat_venta con licencia A", b2["cat_venta"], "A")
    _eq("com_venta_trim con licencia", b2["com_venta_trim"], 28_200)


def test_compute_commissions_fijo_total():
    print("compute_commissions incluye sueldo fijo y compensación total:")
    r = C.compute_commissions(
        {"netas": {"X": 1_000_000}, "brutas": {"X": 1_220_000}},
        {"por_vend": {"X": 1_000_000}},
    )[0]
    _eq("comision_neta", r["comision_neta"], 18_400)
    _eq("sueldo_fijo", r["sueldo_fijo"], 49_855)
    _eq("compensacion_total", r["compensacion_total"], 68_255)


def _mk_cobranzas(rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Cobranzas"
    ws.append(["Numero", "Codigo", "Razon Social", "Importe Total Neto", "Fecha", "Moneda"])
    for r in rows:
        ws.append(r)
    b = io.BytesIO()
    wb.save(b)
    b.seek(0)
    return b


def test_retro_tramos():
    print("Retro por tramos (cruce de umbral y de tier alto):")
    orig = _mk_cobranzas([
        ["N1", "C1", "Cli 1", 650_000, "2026-06-10", "UYU"],   # bajo umbral
        ["N2", "C2", "Cli 2", 1_450_000, "2026-06-11", "UYU"],  # tramo medio
    ])
    upd = _mk_cobranzas([
        ["N1", "C1", "Cli 1", 750_000, "2026-06-10", "UYU"],    # cruza 700k
        ["N2", "C2", "Cli 2", 1_550_000, "2026-06-11", "UYU"],  # cruza 1.5M
    ])
    mapa = {"C1": "V1", "C2": "V2"}
    aj = C.compute_retroactive_adjustment(orig, upd, mapa)["ajuste_comision_por_vendedor"]
    _eq("V1 cruza umbral → 50k×3%", aj["V1"], 1_500)
    _eq("V2 cruza tier alto → 50k×4%+50k×3%", aj["V2"], 3_500)
    # Dual-run: esquema v1 = delta × 3% plano
    aj1 = C.compute_retroactive_adjustment(orig, upd, mapa, esquema="v1")["ajuste_comision_por_vendedor"]
    _eq("V1 v1 plano", aj1["V1"], 3_000)
    _eq("V2 v1 plano", aj1["V2"], 3_000)


def _sheet(filas):
    """DataFrame con el shape del snapshot de cobranzas de M-1."""
    return pd.DataFrame(
        [{"numero": n, "periodo_cobranza": "2026-07", "vendedor": v,
          "rut_cliente": r, "razon_social": "Cli", "fecha_cobranza": "01/07/2026",
          "importe": float(i)} for n, v, r, i in filas]
    )


def test_ajuste_api_por_tramos():
    """El ajuste retroactivo del camino API es el marginal sobre la base de
    M-1, no delta x 3% plano. Es el mismo invariante que test_retro_tramos
    blinda para el camino xlsx: se habia perdido al portar a la API."""
    print("Ajuste retroactivo (camino API) por tramos:")
    # Snapshot de M-1: tres vendedores en tres tramos distintos.
    sheet = _sheet([
        ("S1", "BAJO", "R1", 500_000),      # bajo el umbral de 700k
        ("S2", "MEDIO", "R2", 900_000),     # tramo del 3%
        ("S3", "ALTO", "R3", 1_450_000),    # a punto de cruzar 1,5M
    ])
    base = A.base_cobranzas_desde_sheet(sheet)
    _eq("base BAJO", base["BAJO"], 500_000)
    _eq("base ALTO", base["ALTO"], 1_450_000)

    # Una cobranza tardia de 100k para cada uno.
    api = [
        {"Numero": "S1", "NroDocumento": "R1", "RazonSocial": "Cli",
         "ImporteTotal": "500.000,00", "Fecha": "01/07/2026"},
        {"Numero": "S2", "NroDocumento": "R2", "RazonSocial": "Cli",
         "ImporteTotal": "900.000,00", "Fecha": "01/07/2026"},
        {"Numero": "S3", "NroDocumento": "R3", "RazonSocial": "Cli",
         "ImporteTotal": "1.450.000,00", "Fecha": "01/07/2026"},
        {"Numero": "T1", "NroDocumento": "R1", "RazonSocial": "Cli",
         "ImporteTotal": "100.000,00", "Fecha": "20/07/2026"},
        {"Numero": "T2", "NroDocumento": "R2", "RazonSocial": "Cli",
         "ImporteTotal": "100.000,00", "Fecha": "20/07/2026"},
        {"Numero": "T3", "NroDocumento": "R3", "RazonSocial": "Cli",
         "ImporteTotal": "100.000,00", "Fecha": "20/07/2026"},
    ]
    mapa = {"R1": "BAJO", "R2": "MEDIO", "R3": "ALTO"}
    dif = A.detectar_diferencias(api, sheet)
    _eq("tardias detectadas", len(dif["tardias"]), 3)
    aj = A.calcular_ajuste(
        dif, mapa, base_cobranzas_por_vendedor=base
    )["ajuste_comision_por_vendedor"]

    # 500k -> 600k: sigue bajo el umbral, no genera comision.
    _eq("bajo umbral -> 0 (con 3% plano daba 3.000)", aj["BAJO"], 0)
    # 900k -> 1M: todo dentro del tramo medio.
    _eq("tramo medio -> 100k x 3%", aj["MEDIO"], 3_000)
    # 1,45M -> 1,55M: 50k al 3% + 50k al 4%.
    _eq("cruza 1,5M -> 50k x 3% + 50k x 4%", aj["ALTO"], 3_500)


def test_ajuste_api_cruza_umbral_y_anuladas():
    """Cruce del umbral de 700k hacia arriba, y anulada que resta."""
    print("Ajuste retroactivo (camino API): cruce de umbral y anulada:")
    sheet = _sheet([
        ("S1", "SUBE", "R1", 650_000),
        ("S2", "ANULA", "R2", 1_000_000),
    ])
    base = A.base_cobranzas_desde_sheet(sheet)
    api = [
        {"Numero": "S1", "NroDocumento": "R1", "RazonSocial": "Cli",
         "ImporteTotal": "650.000,00", "Fecha": "01/07/2026"},
        {"Numero": "T1", "NroDocumento": "R1", "RazonSocial": "Cli",
         "ImporteTotal": "100.000,00", "Fecha": "20/07/2026"},
    ]
    mapa = {"R1": "SUBE", "R2": "ANULA"}
    dif = A.detectar_diferencias(api, sheet)
    _eq("anuladas detectadas", len(dif["anuladas"]), 1)
    res = A.calcular_ajuste(dif, mapa, base_cobranzas_por_vendedor=base)
    aj = res["ajuste_comision_por_vendedor"]
    # 650k -> 750k: solo los 50k que pasan el umbral comisionan.
    _eq("cruza 700k -> 50k x 3% (no 100k)", aj["SUBE"], 1_500)
    # 1M -> 0: el ajuste es negativo por todo el excedente del umbral.
    _eq("anulada -> negativo", aj["ANULA"], -9_000)
    _eq("negativo queda en alerta", len(res["vendedores_con_ajuste_negativo"]), 1)


def test_ajuste_api_sin_base_no_inventa():
    """Sin base de M-1 el ajuste no puede inventar tramo: arranca de cero."""
    print("Ajuste retroactivo (camino API) sin base:")
    sheet = _sheet([])
    api = [{"Numero": "T1", "NroDocumento": "R1", "RazonSocial": "Cli",
            "ImporteTotal": "100.000,00", "Fecha": "20/07/2026"}]
    dif = A.detectar_diferencias(api, sheet)
    aj = A.calcular_ajuste(dif, {"R1": "V1"})["ajuste_comision_por_vendedor"]
    _eq("base 0 + 100k -> bajo umbral -> 0", aj["V1"], 0)


def main():
    for t in [
        test_tramos_venta, test_tramos_cobranza, test_dual_run_v1,
        test_clasificar_pilar, test_bono_y_licencia,
        test_ajuste_api_por_tramos, test_ajuste_api_cruza_umbral_y_anuladas,
        test_ajuste_api_sin_base_no_inventa,
        test_compute_commissions_fijo_total, test_retro_tramos,
    ]:
        t()
    print()
    if _fallos:
        print(f"❌ {len(_fallos)} fallo(s): {_fallos}")
        return 1
    print("✅ TODOS LOS TESTS v1.2 PASAN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
