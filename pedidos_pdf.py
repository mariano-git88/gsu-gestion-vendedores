"""
pedidos_pdf.py — Generación de PDFs para las órdenes cargadas en
Contabilium desde la app de Pedidos.

Contabilium NO expone un endpoint de PDF para órdenes de venta (sólo lo
hace para comprobantes/facturas — verificado 2026-05-20 contra el ERP
real, todas las variantes razonables devuelven el JSON del detalle).
Por eso generamos nuestro propio formato con todos los datos de la
orden: pensado para que la persona del depósito tenga lo necesario para
armar el pedido sin tener que entrar a Contabilium uno por uno.

API pública:
  - generar_pdf_orden(orden) → bytes (1 orden, 1 archivo PDF, A4)
  - combinar_pdfs(list[bytes]) → bytes (todos unidos en un solo PDF)
"""

from __future__ import annotations

import io

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

_IVA = 0.22


def _fmt_uyu(v: float) -> str:
    return f"$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def generar_pdf_orden(orden: dict) -> bytes:
    """Construye el PDF de UNA orden de venta.

    Estructura esperada del dict `orden`:
        {
          "pedido_hoja": "Pedido 1",
          "numero_orden": "00010749",
          "id_orden": "2069987",
          "fecha": "2026-05-20",
          "cliente": {"codigo", "razon_social", "rut"},
          "vendedor_id": 237,
          "deposito": "VENTAS",
          "observaciones": "32% en burletes ...",
          "items": [
              {"codigo", "descripcion", "cantidad",
               "precio_unit", "bonif_pct"},
              ...
          ],
        }
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=f"Orden de venta {orden['numero_orden']}",
        author="Suprabond GSU",
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=14, leading=16)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=9)

    elements = []

    # --- Encabezado ---
    elements.append(
        Paragraph(
            f"<b>ORDEN DE VENTA Nº {orden['numero_orden']}</b>", h1
        )
    )
    elements.append(
        Paragraph(
            f"Suprabond GSU · generado desde {orden['pedido_hoja']}", body
        )
    )
    elements.append(Spacer(1, 4 * mm))

    # --- Bloque cliente + meta (2 columnas) ---
    cli = orden["cliente"]
    meta_left = (
        f"<b>Cliente:</b> {cli['razon_social']}<br/>"
        f"<b>RUT:</b> {cli['rut']}<br/>"
        f"<b>Código Contab.:</b> {cli['codigo']}"
    )
    meta_right = (
        f"<b>Fecha:</b> {orden['fecha']}<br/>"
        f"<b>Vendedor (ID):</b> {orden.get('vendedor_id') or '—'}<br/>"
        f"<b>Depósito:</b> {orden.get('deposito') or '—'}"
    )
    head_tbl = Table(
        [[Paragraph(meta_left, body), Paragraph(meta_right, body)]],
        colWidths=[110 * mm, 70 * mm],
    )
    head_tbl.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    elements.append(head_tbl)
    elements.append(Spacer(1, 4 * mm))

    # --- Tabla de ítems ---
    data = [["Código", "Descripción", "Cant.", "Precio U.", "Bonif", "Subtotal"]]
    total_neto = 0.0
    for it in orden["items"]:
        cant = float(it["cantidad"])
        precio = float(it["precio_unit"])
        bonif = float(it.get("bonif_pct") or 0.0)
        subt = cant * precio * (1 - bonif / 100.0)
        total_neto += subt
        data.append(
            [
                it["codigo"],
                Paragraph(str(it["descripcion"])[:80], body),
                f"{cant:g}",
                _fmt_uyu(precio),
                f"{bonif:g}%" if bonif else "—",
                _fmt_uyu(subt),
            ]
        )
    items_tbl = Table(
        data,
        colWidths=[28 * mm, 72 * mm, 14 * mm, 22 * mm, 14 * mm, 30 * mm],
        repeatRows=1,
    )
    items_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#222222")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 9),
                ("FONTSIZE", (0, 1), (-1, -1), 8),
                ("ALIGN", (2, 1), (2, -1), "RIGHT"),
                ("ALIGN", (3, 1), (3, -1), "RIGHT"),
                ("ALIGN", (4, 1), (4, -1), "RIGHT"),
                ("ALIGN", (5, 1), (5, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    elements.append(items_tbl)
    elements.append(Spacer(1, 4 * mm))

    # --- Observaciones (si hay) ---
    obs = (orden.get("observaciones") or "").strip()
    if obs:
        elements.append(Paragraph(f"<b>Observaciones:</b> {obs}", body))
        elements.append(Spacer(1, 3 * mm))

    # --- Totales (alineados a la derecha) ---
    iva_total = total_neto * _IVA
    total_con_iva = total_neto + iva_total
    totales_data = [
        ["Total sin IVA:", _fmt_uyu(total_neto)],
        ["IVA 22%:", _fmt_uyu(iva_total)],
        ["TOTAL CON IVA:", _fmt_uyu(total_con_iva)],
    ]
    tot_tbl = Table(
        totales_data, colWidths=[40 * mm, 35 * mm], hAlign="RIGHT"
    )
    tot_tbl.setStyle(
        TableStyle(
            [
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
                ("LINEABOVE", (0, 2), (-1, 2), 0.5, colors.black),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    elements.append(tot_tbl)

    # Pie chico con el ID interno (útil para soporte / búsqueda)
    elements.append(Spacer(1, 5 * mm))
    elements.append(
        Paragraph(
            f"<font size=7 color='#777777'>"
            f"ID interno Contabilium: {orden.get('id_orden', '—')} · "
            f"Documento generado por la app de Carga de Pedidos · "
            f"NO es el comprobante fiscal (eso lo emite el Facturador)"
            f"</font>",
            body,
        )
    )

    doc.build(elements)
    return buf.getvalue()


def combinar_pdfs(pdfs: list[bytes]) -> bytes:
    """Une todos los PDFs en uno solo, en el orden recibido. Cada PDF
    de entrada aporta sus páginas tal cual."""
    writer = PdfWriter()
    for pdf_bytes in pdfs:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        for page in reader.pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
