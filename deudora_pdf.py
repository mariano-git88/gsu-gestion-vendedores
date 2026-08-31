"""
deudora_pdf.py — La deudora en PDF, un archivo por vendedor.

Formato calcado del `Deudora 049.pdf` que usaban en ERPA con el SUMMA viejo
(está en `assets/`): por cada cliente, el encabezado con sus datos de
contacto, la lista de comprobantes con saldo y el total de la cuenta. Es el
papel que el vendedor lleva a la visita, así que prioriza que se lea de un
vistazo por sobre la cantidad de información.

Dos diferencias con el informe de ERPA, y las dos son limitaciones del ERP,
no decisiones de diseño (ver `reference_contabilium_lo_que_no_tiene`):

  - **No hay columna Cuota.** Contabilium emite una sola factura por una
    venta a 30/60/90, con un solo saldo: no sabe qué parte vence en cada
    cuota. Se muestra la factura entera con el vencimiento promedio.
  - **No hay "Saldo Documentos".** No existe cartera de cheques por API.

Funciones puras: reciben DataFrames, devuelven bytes. No importa streamlit.
"""

from __future__ import annotations

import io
from datetime import date

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

import deudora

# Gris de Suprabond para los encabezados de tabla, y el rojo solo para lo
# vencido. El color no lleva información que no esté también en el número:
# el PDF se imprime en blanco y negro más veces de las que uno cree.
GRIS = colors.HexColor("#E8EAE8")
GRIS_LINEA = colors.HexColor("#BFC5BF")
ROJO = colors.HexColor("#A33A2A")
TINTA = colors.HexColor("#16211C")


def _uyu(v) -> str:
    """1234.5 → '1.234,50'. Vacío si no es un número."""
    try:
        return f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return ""


def _fecha(v) -> str:
    return "" if pd.isna(v) else f"{pd.Timestamp(v):%d/%m/%Y}"


def _estilos():
    s = getSampleStyleSheet()
    return {
        "titulo": ParagraphStyle("t", parent=s["Heading1"], fontSize=13,
                                 leading=15, textColor=TINTA),
        "sub": ParagraphStyle("s", parent=s["BodyText"], fontSize=8.5,
                              textColor=colors.HexColor("#4E5C55")),
        "cliente": ParagraphStyle("c", parent=s["BodyText"], fontSize=9.5,
                                  leading=12, textColor=TINTA),
        "chico": ParagraphStyle("x", parent=s["BodyText"], fontSize=7.5,
                                leading=9, textColor=colors.HexColor("#4E5C55")),
    }


def generar_pdf_vendedor(
    vendedor: str,
    resumen: pd.DataFrame,
    movimientos: pd.DataFrame,
    fecha: date | None = None,
    solo_con_deuda: bool = True,
) -> bytes:
    """PDF con la cuenta corriente de todos los clientes de un vendedor.

    Args:
      vendedor: nombre tal como aparece en `resumen["vendedor"]`.
      resumen: salida de `deudora.resumen_por_cliente` (toda la cartera; se
        filtra acá adentro).
      movimientos: salida de `deudora.armar_movimientos`.
      fecha: fecha del informe. Default: hoy.
      solo_con_deuda: deja afuera a los clientes con saldo cero o a favor.
        En la visita interesa a quién hay que cobrarle.
    """
    fecha = fecha or date.today()
    est = _estilos()
    mios = resumen[resumen["vendedor"] == vendedor].copy()
    if solo_con_deuda:
        mios = mios[mios["deuda_total"] > 1.0]
    mios = mios.sort_values("vencido_neto", ascending=False)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=10 * mm, bottomMargin=10 * mm,
        title=f"Cuenta corriente — {vendedor}",
        author="Suprabond GSU",
    )

    el = [
        Paragraph("<b>CUENTA CORRIENTE DE CLIENTES</b>", est["titulo"]),
        Paragraph(
            f"{vendedor} · al {fecha:%d/%m/%Y} · "
            f"{len(mios)} clientes con saldo", est["sub"]),
        Spacer(1, 5 * mm),
    ]

    if mios.empty:
        el.append(Paragraph("Sin clientes con saldo pendiente.", est["cliente"]))
        doc.build(el)
        return buf.getvalue()

    for _, cli in mios.iterrows():
        el.extend(_bloque_cliente(cli, movimientos, est))

    el.append(Spacer(1, 4 * mm))
    el.append(Paragraph(
        f"<b>TOTAL DE LA CARTERA:</b> {_uyu(mios['deuda_total'].sum())} · "
        f"vencido {_uyu(mios['vencido_neto'].sum())}", est["cliente"]))
    el.append(Spacer(1, 2 * mm))
    el.append(Paragraph(
        "Los vencimientos se calculan según la condición de pago de cada "
        "factura, que no siempre coincide con la fecha impresa en el "
        "comprobante. Las ventas en cuotas se muestran como una sola "
        "factura con el vencimiento promedio.", est["chico"]))

    doc.build(el)
    return buf.getvalue()


# Arriba de esta cantidad de facturas, el bloque de un cliente ya no entra en
# una carilla y se lo deja fluir. Con `KeepTogether` sin tope, un cliente como
# Tienda Inglesa (97 facturas abiertas) empuja todo a la página siguiente y
# deja la anterior en blanco. El encabezado de la tabla se repite igual por
# `repeatRows=1`, así que partirlo no deja columnas huérfanas.
MAX_FILAS_JUNTAS = 12


def _bloque_cliente(cli: pd.Series, movimientos: pd.DataFrame, est) -> list:
    """Encabezado + comprobantes abiertos + total de UN cliente.

    Los clientes chicos van envueltos en `KeepTogether` para que el nombre no
    quede al pie de una página y sus facturas en la siguiente: en el
    mostrador eso se lee como si las facturas fueran del cliente de arriba.
    """
    contacto = " · ".join(
        str(x) for x in [cli.get("documento"), cli.get("ciudad"),
                         cli.get("telefono")] if x and str(x) != "nan"
    )
    cab = Paragraph(
        f"<b>{cli['razon_social']}</b><br/>"
        f"<font size=8 color='#4E5C55'>{contacto}</font>", est["cliente"])

    abiertas = deudora.extracto_de_cliente(
        movimientos, cli["id_cliente"], solo_abiertas=True)

    filas = [["Fecha", "Comprobante", "Condición", "Vence", "Días",
              "Importe", "Saldo"]]
    for _, m in abiertas.iterrows():
        dias = m["dias_vencido"]
        filas.append([
            _fecha(m["fecha"]),
            str(m["comprobante"] or ""),
            str(m["cond_pago"] or ""),
            _fecha(m["vencimiento"]),
            "" if pd.isna(dias) else f"{int(dias)}",
            _uyu(m["debe"]),
            _uyu(m["saldo_pendiente"]),
        ])

    tbl = Table(filas, colWidths=[20 * mm, 34 * mm, 44 * mm, 20 * mm,
                                  14 * mm, 30 * mm, 30 * mm],
                repeatRows=1)
    estilo = [
        ("BACKGROUND", (0, 0), (-1, 0), GRIS),
        ("TEXTCOLOR", (0, 0), (-1, -1), TINTA),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, GRIS_LINEA),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor("#E8EAE8")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    # Los días de atraso en rojo, fila por fila. La columna "Días" está vacía
    # cuando la factura todavía no venció, así que pintar por valor y no por
    # posición evita teñir lo que está al día.
    for i, (_, m) in enumerate(abiertas.iterrows(), start=1):
        if not pd.isna(m["dias_vencido"]) and int(m["dias_vencido"]) > 0:
            estilo.append(("TEXTCOLOR", (4, i), (4, i), ROJO))
    tbl.setStyle(TableStyle(estilo))

    total = Paragraph(
        f"<para align='right'><b>Deuda del cliente: {_uyu(cli['deuda_total'])}"
        f"</b>{_extra_total(cli)}</para>", est["cliente"])

    piezas = [cab, Spacer(1, 1.5 * mm), tbl, Spacer(1, 1.5 * mm), total,
              Spacer(1, 5 * mm)]
    if len(abiertas) <= MAX_FILAS_JUNTAS:
        return [KeepTogether(piezas)]
    # El encabezado sí se mantiene pegado a las primeras filas de la tabla.
    return piezas


def _extra_total(cli: pd.Series) -> str:
    """Cola del total: vencido y crédito a favor, solo si existen."""
    partes = []
    if float(cli.get("vencido_neto") or 0) > 1:
        partes.append(
            f"<font color='#A33A2A'>vencido {_uyu(cli['vencido_neto'])}</font>")
    if float(cli.get("credito_a_favor") or 0) > 1:
        partes.append(f"a favor {_uyu(cli['credito_a_favor'])}")
    return ("<br/><font size=8>" + " · ".join(partes) + "</font>") if partes else ""


def generar_pdf_todos(
    resumen: pd.DataFrame,
    movimientos: pd.DataFrame,
    fecha: date | None = None,
    solo_con_deuda: bool = True,
) -> bytes:
    """Un solo PDF con el informe de cada vendedor, uno atrás del otro.

    Es lo que Ernesto imprime para la reunión: el orden es por vencido
    descendente, así el que más problemas tiene queda primero.
    """
    from pedidos_pdf import combinar_pdfs

    orden = deudora.totales_por_vendedor(resumen)["vendedor"].tolist()
    pdfs = [
        generar_pdf_vendedor(v, resumen, movimientos, fecha, solo_con_deuda)
        for v in orden
    ]
    return combinar_pdfs(pdfs) if pdfs else b""
