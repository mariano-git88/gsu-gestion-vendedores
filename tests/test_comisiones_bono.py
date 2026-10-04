"""
test_comisiones_bono.py — Blinda el puente entre el histórico del Sheet y
la fórmula del bono trimestral (`comisiones_bono.py`).

La fórmula en sí la blinda `test_comisiones_v12.py`. Acá se prueba lo que
puede salir mal en el ARMADO: las ventas del histórico son brutas y los
umbrales son netos, un mes puede no tener fila, el Sheet se edita a mano.

Se corre sin API ni pytest:  python3 tests/test_comisiones_bono.py
(desde la raíz del repo). Datos 100% sintéticos — el repo es público.
"""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from openpyxl import load_workbook

import comisiones_bono as B
import commissions as C

_fallos = []

PERIODOS_Q3 = ["2026-07", "2026-08", "2026-09"]


def _eq(label, got, exp, tol=1e-6):
    ok = abs(got - exp) < tol
    print(f"  [{'OK' if ok else 'FALLA'}] {label}: got={got:,.4f} exp={exp:,.4f}")
    if not ok:
        _fallos.append(label)


def _is(label, got, exp):
    ok = got == exp
    print(f"  [{'OK' if ok else 'FALLA'}] {label}: got={got!r} exp={exp!r}")
    if not ok:
        _fallos.append(label)


def _hist(filas):
    """filas: (vendedor, periodo, ventas_brutas, cobranzas)."""
    return pd.DataFrame(
        [{"vendedor": v, "periodo": p, "ventas": vb, "cobranzas": co,
          "comision_neta": 0, "fecha_calculo": ""} for v, p, vb, co in filas]
    )


# ---------------------------------------------------------------------

def test_trimestres():
    print("Trimestres y períodos:")
    _is("Q3 2026", B.periodos_del_trimestre(2026, 3), PERIODOS_Q3)
    _is("Q1 2026", B.periodos_del_trimestre(2026, 1),
        ["2026-01", "2026-02", "2026-03"])
    _is("Q4 2026", B.periodos_del_trimestre(2026, 4),
        ["2026-10", "2026-11", "2026-12"])
    _is("ago-2026 es Q3", B.trimestre_de_periodo("2026-08"), (2026, 3))
    _is("ene-2026 es Q1", B.trimestre_de_periodo("2026-01"), (2026, 1))
    _is("dic-2025 es Q4", B.trimestre_de_periodo("2025-12"), (2025, 4))
    _is("label", B.label_trimestre(2026, 3), "Q3 2026 (jul–sep)")
    df = _hist([("V1", "2026-07", 0, 0), ("V1", "2026-04", 0, 0)])
    _is("trimestres presentes, más nuevo primero",
        B.trimestres_en_historico(df), [(2026, 3), (2026, 2)])
    _is("histórico vacío → sin trimestres",
        B.trimestres_en_historico(pd.DataFrame()), [])


def test_gate_mes_faltante():
    print("Gate: un mes que no está en el histórico frena el cálculo:")
    df = _hist([("V1", "2026-07", 1_000_000, 900_000),
                ("V1", "2026-08", 1_000_000, 900_000)])
    _is("falta septiembre", B.periodos_faltantes(df, PERIODOS_Q3), ["2026-09"])
    df2 = _hist([("V1", p, 1, 1) for p in PERIODOS_Q3])
    _is("están los 3 → no falta nada", B.periodos_faltantes(df2, PERIODOS_Q3), [])
    _is("histórico vacío → faltan los 3",
        B.periodos_faltantes(pd.DataFrame(), PERIODOS_Q3), PERIODOS_Q3)


def test_brutas_a_netas():
    print("Las ventas del histórico son BRUTAS; el umbral es NETO:")
    # 732.000 brutas = 600.000 netas exactas → mes pleno justo en el borde.
    df = _hist([("V1", p, 732_000, 700_000) for p in PERIODOS_Q3])
    datos, _ = B.armar_datos_bono(df, PERIODOS_Q3)
    _eq("venta neta de 732.000 brutas", datos["V1"]["ventas_netas"][0], 600_000)
    _eq("cobranza va cruda, sin dividir", datos["V1"]["cobranzas"][0], 700_000)
    bono = C.compute_bono_trimestral(datos)
    # 600k neta y 700k cobrados están EN el umbral → son meses plenos (≥),
    # pero la comisión del tramo es 0, así que el bono es 0 con Cat A.
    _is("Cat A venta en el borde", bono["V1"]["cat_venta"], "A")
    _is("Cat A cobranza en el borde", bono["V1"]["cat_cobranza"], "A")
    _eq("comisión trim en el umbral = 0", bono["V1"]["com_venta_trim"], 0)
    _eq("bono en el umbral = 0", bono["V1"]["bono_total"], 0)
    # Un peso menos de venta bruta y ya no es mes pleno.
    df2 = _hist([("V1", PERIODOS_Q3[0], 731_000, 700_000)]
                + [("V1", p, 732_000, 700_000) for p in PERIODOS_Q3[1:]])
    datos2, _ = B.armar_datos_bono(df2, PERIODOS_Q3)
    bono2 = C.compute_bono_trimestral(datos2)
    _is("731.000 brutas ya no es mes pleno", bono2["V1"]["cat_venta"], "B")


def test_mes_sin_fila_se_ve():
    print("Un vendedor sin fila en un mes: computa 0 y queda anotado:")
    df = _hist([("V1", "2026-07", 1_220_000, 1_000_000),
                ("V1", "2026-08", 1_220_000, 1_000_000)])
    datos, detalle = B.armar_datos_bono(df, PERIODOS_Q3)
    _eq("septiembre computa 0 en venta", datos["V1"]["ventas_netas"][2], 0)
    _eq("septiembre computa 0 en cobranza", datos["V1"]["cobranzas"][2], 0)
    _is("el mes sin fila queda anotado",
        detalle["V1"]["meses_sin_fila"], ["2026-09"])
    bono = C.compute_bono_trimestral(datos)
    # 2 de 3 plenos → no es A. Promedio venta = 666.667 ≥ 300.000 → B.
    _is("2 de 3 meses plenos → Cat B", bono["V1"]["cat_venta"], "B")


def test_operarios_excluidos():
    print("Los operarios no entran al bono ni si quedaron en el histórico:")
    op = sorted(C.VENDEDORES_EXCLUIDOS_OP)[0]
    df = _hist([("V1", p, 1_220_000, 1_000_000) for p in PERIODOS_Q3]
               + [(op, p, 1_220_000, 1_000_000) for p in PERIODOS_Q3])
    datos, _ = B.armar_datos_bono(df, PERIODOS_Q3)
    _is("solo queda el vendedor real", sorted(datos.keys()), ["V1"])


def test_sheet_editado_a_mano():
    print("Tolerancia al Sheet editado a mano (texto, espacios, duplicados):")
    df = pd.DataFrame([
        # importes como texto, vendedor en minúscula y con espacios
        {"vendedor": " v1 ", "periodo": "2026-07", "ventas": "610000",
         "cobranzas": "500000", "comision_neta": 0, "fecha_calculo": ""},
        # dos filas del MISMO mes: se suman (son dos pedazos del mes)
        {"vendedor": "V1", "periodo": "2026-07", "ventas": "610000",
         "cobranzas": "500000", "comision_neta": 0, "fecha_calculo": ""},
        {"vendedor": "V1", "periodo": "2026-08", "ventas": "1220000",
         "cobranzas": "1000000", "comision_neta": 0, "fecha_calculo": ""},
        # basura en el importe → 0, no explota
        {"vendedor": "V1", "periodo": "2026-09", "ventas": "n/d",
         "cobranzas": "1000000", "comision_neta": 0, "fecha_calculo": ""},
    ])
    datos, _ = B.armar_datos_bono(df, PERIODOS_Q3)
    _is("se normaliza el vendedor", sorted(datos.keys()), ["V1"])
    _eq("las dos filas de julio se suman",
        datos["V1"]["ventas_netas"][0], 1_220_000 / C.DIVISOR_IVA)
    _eq("importe no numérico → 0", datos["V1"]["ventas_netas"][2], 0)


def test_licencia_cambia_la_categoria():
    print("Licencia: el mes se sustituye por el promedio de los otros dos:")
    # Julio y agosto plenos y fuertes; septiembre casi en cero por licencia.
    df = _hist([("V1", "2026-07", 1_220_000, 1_000_000),
                ("V1", "2026-08", 1_220_000, 1_000_000),
                ("V1", "2026-09", 10_000, 10_000)])
    datos_sin, _ = B.armar_datos_bono(df, PERIODOS_Q3)
    bono_sin = C.compute_bono_trimestral(datos_sin)
    _is("sin marcar licencia → Cat B", bono_sin["V1"]["cat_venta"], "B")

    datos_con, detalle_con = B.armar_datos_bono(
        df, PERIODOS_Q3, licencias={"V1": {2}}
    )
    bono_con = C.compute_bono_trimestral(datos_con)
    _is("con licencia en septiembre → Cat A", bono_con["V1"]["cat_venta"], "A")
    _is("cobranza también pasa a A", bono_con["V1"]["cat_cobranza"], "A")
    # El monto también se recomputa desde el volumen sustituido: los 3 meses
    # valen lo mismo que julio (1.000.000 neto → 9.400 de comisión).
    _eq("com venta trim con licencia", bono_con["V1"]["com_venta_trim"], 3 * 9_400)
    _eq("bono venta = 10% de eso", bono_con["V1"]["bono_venta"], 2_820)

    # El detalle muestra el crudo y el computado, para poder auditarlo.
    det = B.tabla_detalle(detalle_con, PERIODOS_Q3)
    fila_sep = det[det["mes"] == "2026-09"].iloc[0]
    _eq("la licencia se marca en el detalle (mes entero = 30 días)",
        fila_sep["dias_licencia"], 30)
    _eq("crudo de septiembre", fila_sep["venta_neta"], 10_000 / C.DIVISOR_IVA)
    _eq("computado de septiembre", fila_sep["venta_computada"], 1_000_000)
    _is("se marca como mes pleno por el computado",
        fila_sep["venta_plena"], "✔")


def test_licencia_indices_invalidos():
    print("Índices de licencia fuera de rango se ignoran, no explotan:")
    df = _hist([("V1", p, 1_220_000, 1_000_000) for p in PERIODOS_Q3])
    _, det = B.armar_datos_bono(df, PERIODOS_Q3, licencias={"V1": {0, 5, -1}})
    _is("solo queda el índice válido",
        sorted(det["V1"]["licencia_dias"]), [0])
    _is("la fórmula no vuelve a sustituir (ya se aplicó acá)",
        B.armar_datos_bono(df, PERIODOS_Q3)[0]["V1"]["licencia_meses"], set())
    _is("vendedor que no existe en el dict no molesta", "V2" in det, False)


def test_resumen_y_total():
    print("Tabla resumen: el total es la suma de los bonos redondeados:")
    df = _hist(
        [("V1", p, 1_830_000, 1_500_000) for p in PERIODOS_Q3]   # Cat A/A
        + [("V2", p, 500_000, 400_000) for p in PERIODOS_Q3]     # sin categoría
    )
    datos, _ = B.armar_datos_bono(df, PERIODOS_Q3)
    bono = C.compute_bono_trimestral(datos)
    res = B.tabla_resumen(bono)
    _is("una fila por vendedor", len(res), 2)
    _is("columnas esperadas", list(res.columns), B.COL_RESUMEN)
    _is("V2 sin categoría en venta",
        res[res["vendedor"] == "V2"].iloc[0]["cat_venta"], "—")
    _eq("V2 no cobra bono",
        res[res["vendedor"] == "V2"].iloc[0]["bono_total"], 0)
    _eq("el total del Excel = suma de bono_total",
        float(res["bono_total"].sum()), float(bono["V1"]["bono_total"]))


def test_xlsx_se_abre():
    print("El xlsx del bono se genera y se vuelve a abrir:")
    df = _hist([("V1", p, 1_830_000, 1_500_000) for p in PERIODOS_Q3])
    datos, detalle = B.armar_datos_bono(df, PERIODOS_Q3)
    bono = C.compute_bono_trimestral(datos)
    data = B.build_xlsx_bono(bono, detalle, PERIODOS_Q3,
                             B.label_trimestre(2026, 3), B.REGLA_PROPORCIONAL)
    _is("devuelve bytes", isinstance(data, bytes), True)
    wb = load_workbook(io.BytesIO(data))
    _is("las 3 hojas",
        wb.sheetnames, ["Bono trimestral", "Detalle mensual", "Cómo se calcula"])
    ws = wb["Bono trimestral"]
    # Encabezado en la fila 4, datos en la 5, TOTAL en la 6.
    _is("encabezado", ws.cell(row=4, column=1).value, "vendedor")
    _is("el vendedor", ws.cell(row=5, column=1).value, "V1")
    _is("fila de total", ws.cell(row=6, column=1).value, "TOTAL")
    j_total = B.COL_RESUMEN.index("bono_total") + 1
    _eq("el total del xlsx",
        float(ws.cell(row=6, column=j_total).value), bono["V1"]["bono_total"])
    _is("detalle: 3 meses", len(wb["Detalle mensual"]["A"]) >= 3 + 3, True)



def test_normalizar_licencias():
    print("Las dos formas de declarar licencia dan lo mismo:")
    _is("set → mes entero en días",
        B.normalizar_licencias({"V1": {0}}, PERIODOS_Q3), {"V1": {0: 31}})
    _is("dict con días",
        B.normalizar_licencias({"V1": {2: 10}}, PERIODOS_Q3), {"V1": {2: 10.0}})
    _is("días > días del mes se topean",
        B.normalizar_licencias({"V1": {2: 99}}, PERIODOS_Q3), {"V1": {2: 30}})
    _is("0 días no es licencia",
        B.normalizar_licencias({"V1": {0: 0}}, PERIODOS_Q3), {})
    _is("días negativos se descartan",
        B.normalizar_licencias({"V1": {0: -5}}, PERIODOS_Q3), {})
    _is("índice fuera de rango se descarta",
        B.normalizar_licencias({"V1": {7: 10}}, PERIODOS_Q3), {})
    _is("se normaliza el nombre del vendedor",
        B.normalizar_licencias({" v1 ": {0: 5}}, PERIODOS_Q3), {"V1": {0: 5.0}})
    _is("días del mes", B.dias_del_mes("2026-09"), 30)
    _is("febrero 2026", B.dias_del_mes("2026-02"), 28)


def test_proporcional_es_generalizacion():
    print("Mes ENTERO de licencia y sin producción propia = regla de mes completo:")
    df = _hist([("V1", "2026-07", 1_220_000, 1_000_000),
                ("V1", "2026-08", 1_830_000, 1_600_000),
                ("V1", "2026-09", 0, 0)])
    # licencia = los 30 días de septiembre
    d_mc, _ = B.armar_datos_bono(df, PERIODOS_Q3, licencias={"V1": {2}},
                                 regla_licencia=B.REGLA_MES_COMPLETO)
    d_pr, _ = B.armar_datos_bono(df, PERIODOS_Q3, licencias={"V1": {2: 30}},
                                 regla_licencia=B.REGLA_PROPORCIONAL)
    _eq("mismo volumen de venta en el mes de licencia",
        d_pr["V1"]["ventas_netas"][2], d_mc["V1"]["ventas_netas"][2])
    _eq("mismo volumen de cobranza",
        d_pr["V1"]["cobranzas"][2], d_mc["V1"]["cobranzas"][2])
    _eq("y es el promedio de los otros dos",
        d_pr["V1"]["ventas_netas"][2],
        (1_220_000 + 1_830_000) / 2 / C.DIVISOR_IVA)

    # El caso límite que NO coincide, y queda escrito para que nadie se
    # sorprenda: mes entero de licencia pero con facturación propia (una
    # orden que se facturó igual). Proporcional SUMA esa producción al
    # promedio; mes completo la descarta. Con licencias parciales — el caso
    # real — esto no se presenta.
    df2 = _hist([("V1", "2026-07", 1_220_000, 1_000_000),
                 ("V1", "2026-08", 1_830_000, 1_600_000),
                 ("V1", "2026-09", 12_200, 10_000)])
    e_mc, _ = B.armar_datos_bono(df2, PERIODOS_Q3, licencias={"V1": {2}},
                                 regla_licencia=B.REGLA_MES_COMPLETO)
    e_pr, _ = B.armar_datos_bono(df2, PERIODOS_Q3, licencias={"V1": {2: 30}},
                                 regla_licencia=B.REGLA_PROPORCIONAL)
    _eq("mes completo descarta la producción del mes de licencia",
        e_mc["V1"]["ventas_netas"][2], (1_220_000 + 1_830_000) / 2 / C.DIVISOR_IVA)
    _eq("proporcional se la suma al promedio",
        e_pr["V1"]["ventas_netas"][2],
        10_000 + (1_220_000 + 1_830_000) / 2 / C.DIVISOR_IVA)


def test_proporcional_repone_solo_los_dias():
    print("Proporcional con 10 de 30 días: repone un tercio del promedio:")
    df = _hist([("V1", "2026-07", 1_220_000, 900_000),
                ("V1", "2026-08", 1_220_000, 900_000),
                ("V1", "2026-09", 610_000, 450_000)])
    datos, detalle = B.armar_datos_bono(
        df, PERIODOS_Q3, licencias={"V1": {2: 10}},
        regla_licencia=B.REGLA_PROPORCIONAL,
    )
    prom = 1_000_000.0  # promedio neto de julio y agosto
    _eq("septiembre = real + 10/30 × promedio",
        datos["V1"]["ventas_netas"][2], 500_000 + prom / 3)
    _eq("los meses sin licencia no se tocan",
        datos["V1"]["ventas_netas"][0], 1_000_000)
    _eq("cobranza: 450.000 + 10/30 × 900.000",
        datos["V1"]["cobranzas"][2], 450_000 + 300_000)
    # 833.333 de venta neta supera el umbral: con la regla literal habría dado
    # 1.000.000; con la proporcional igual alcanza para mes pleno.
    bono = C.compute_bono_trimestral(datos)
    _is("Cat A venta", bono["V1"]["cat_venta"], "A")
    _is("Cat A cobranza (750.000 > 700.000)", bono["V1"]["cat_cobranza"], "A")
    det = B.tabla_detalle(detalle, PERIODOS_Q3)
    fila = det[det["mes"] == "2026-09"].iloc[0]
    _eq("el detalle muestra los días", fila["dias_licencia"], 10)
    _eq("y el crudo sin tocar", fila["venta_neta"], 500_000)


def test_proporcional_puede_bajar_la_categoria():
    print("La regla literal puede PERJUDICAR: el mes de licencia era el mejor:")
    # Julio (mes con licencia) es el mejor mes; agosto y septiembre flojos.
    df = _hist([("V1", "2026-07", 1_000_000, 900_000),
                ("V1", "2026-08", 740_000, 710_000),
                ("V1", "2026-09", 740_000, 710_000)])
    d_mc, _ = B.armar_datos_bono(df, PERIODOS_Q3, licencias={"V1": {0: 10}},
                                 regla_licencia=B.REGLA_MES_COMPLETO)
    d_pr, _ = B.armar_datos_bono(df, PERIODOS_Q3, licencias={"V1": {0: 10}},
                                 regla_licencia=B.REGLA_PROPORCIONAL)
    _eq("mes completo le BORRA el mes bueno (queda el promedio de los flojos)",
        d_mc["V1"]["ventas_netas"][0], 740_000 / C.DIVISOR_IVA)
    _eq("proporcional se lo conserva y le suma los días",
        d_pr["V1"]["ventas_netas"][0],
        1_000_000 / C.DIVISOR_IVA + (10 / 31) * (740_000 / C.DIVISOR_IVA))
    b_mc = C.compute_bono_trimestral(d_mc)
    b_pr = C.compute_bono_trimestral(d_pr)
    _is("con mes completo pierde plata",
        b_pr["V1"]["bono_total"] > b_mc["V1"]["bono_total"], True)


def test_regla_invalida_falla_fuerte():
    print("Una regla mal escrita no se procesa en silencio:")
    df = _hist([("V1", p, 1_220_000, 1_000_000) for p in PERIODOS_Q3])
    try:
        B.armar_datos_bono(df, PERIODOS_Q3, regla_licencia="promedio")
        _is("debió lanzar ValueError", False, True)
    except ValueError as e:
        _is("lanza ValueError", "promedio" in str(e), True)
    try:
        B.armar_datos_bono(df, ["2026-07", "2026-08"])
        _is("debió lanzar ValueError por 2 períodos", False, True)
    except ValueError:
        _is("2 períodos → ValueError", True, True)



def test_vigencia_del_bono():
    print("El bono no existe antes de la vigencia de la v1.2 (Q3 2026):")
    _is("primer trimestre con bono", B.PRIMER_TRIMESTRE_CON_BONO, (2026, 3))
    _is("Q3 2026 genera bono", B.trimestre_genera_bono(2026, 3), True)
    _is("Q4 2026 genera bono", B.trimestre_genera_bono(2026, 4), True)
    _is("Q1 2027 genera bono", B.trimestre_genera_bono(2027, 1), True)
    _is("Q2 2026 NO genera bono", B.trimestre_genera_bono(2026, 2), False)
    _is("Q1 2026 NO genera bono", B.trimestre_genera_bono(2026, 1), False)
    _is("Q4 2025 NO genera bono", B.trimestre_genera_bono(2025, 4), False)



def test_licencias_desde_editor():
    print("El pegado entre la tabla de licencias de pantalla y el cálculo:")
    # Forma exacta que devuelve el st.data_editor de la app: los encabezados
    # de mes NO son identificadores válidos, por eso el acceso es posicional.
    cols = ["vendedor", "jul (31 d)", "ago (31 d)", "sep (30 d)"]
    df = pd.DataFrame(
        [["V1", 0, 0, 10], ["V2", 0, 0, 0], ["V3", 5, 0, 0]], columns=cols
    )
    _is("solo los que tienen días",
        B.licencias_desde_editor(df), {"V1": {2: 10.0}, "V3": {0: 5.0}})

    # Celdas borradas: el editor escribe None/NaN.
    df2 = pd.DataFrame(
        [["V1", None, float("nan"), 7]], columns=cols
    )
    _is("vacíos y NaN son 0", B.licencias_desde_editor(df2), {"V1": {2: 7.0}})

    # Decimales y negativos.
    df3 = pd.DataFrame([["V1", -3, 0, 2.5]], columns=cols)
    _is("negativo se ignora, decimal pasa",
        B.licencias_desde_editor(df3), {"V1": {2: 2.5}})

    _is("tabla vacía → sin licencias",
        B.licencias_desde_editor(pd.DataFrame(columns=cols)), {})
    _is("None → sin licencias", B.licencias_desde_editor(None), {})

    # Si alguien cambia la forma de la tabla, se entera: no se procesa mal.
    try:
        B.licencias_desde_editor(pd.DataFrame([["V1", 0]], columns=["v", "a"]))
        _is("debió lanzar ValueError por forma", False, True)
    except ValueError:
        _is("tabla con forma inesperada → ValueError", True, True)

    # Y el resultado entra derecho a armar_datos_bono.
    hist = _hist([("V1", "2026-07", 1_220_000, 1_000_000),
                  ("V1", "2026-08", 1_220_000, 1_000_000),
                  ("V1", "2026-09", 0, 0)])
    lic = B.licencias_desde_editor(pd.DataFrame([["V1", 0, 0, 30]], columns=cols))
    datos, _ = B.armar_datos_bono(hist, PERIODOS_Q3, lic,
                                  regla_licencia=B.REGLA_PROPORCIONAL)
    _eq("septiembre repuesto entero con el promedio",
        datos["V1"]["ventas_netas"][2], 1_000_000)


def main():
    for t in [
        test_trimestres, test_gate_mes_faltante, test_brutas_a_netas,
        test_mes_sin_fila_se_ve, test_operarios_excluidos,
        test_sheet_editado_a_mano, test_licencia_cambia_la_categoria,
        test_licencia_indices_invalidos, test_resumen_y_total,
        test_xlsx_se_abre, test_normalizar_licencias,
        test_proporcional_es_generalizacion,
        test_proporcional_repone_solo_los_dias,
        test_proporcional_puede_bajar_la_categoria,
        test_regla_invalida_falla_fuerte, test_vigencia_del_bono,
        test_licencias_desde_editor,
    ]:
        t()
    print()
    if _fallos:
        print(f"❌ {len(_fallos)} fallo(s): {_fallos}")
        return 1
    print("✅ TODOS LOS TESTS DEL BONO PASAN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
