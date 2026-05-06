"""
agent.py — Asistente conversacional para el dashboard de GSU.

Backend del chat de la tab "🤖 Asistente". Usa Claude (Anthropic) con
tool use: el LLM ve la pregunta del usuario y elige qué función llamar
de un set acotado. Cada tool consulta los DataFrames cacheados del
dashboard (no SQL libre — más predecible y auditable).

Diseño de tools:
  - Cada tool recibe sus argumentos del LLM (validados por JSON schema).
  - Devuelve un dict serializable como string JSON.
  - Si los datos no están disponibles (sync vacío, columnas faltantes),
    devuelve {"error": "..."} en lugar de crashear.

Modelo: claude-sonnet-4-6 (rápido + bueno para tool use).

Costos típicos:
  - Pregunta simple sin tool: ~1k tokens input + 200 output ≈ USD 0.0033
  - Pregunta con 1 tool round-trip: ~2k input + 400 output ≈ USD 0.0066
  - Pregunta con 2-3 tools: ~4k input + 600 output ≈ USD 0.012
  Para uso interno con 1-2 usuarios el costo total mensual es < USD 5.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
import anthropic

import api_loader

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048
MAX_TOOL_LOOPS = 6  # tope de seguridad anti-loop con tool use

def _build_system_prompt(ctx: dict) -> str:
    """Genera el system prompt incluyendo la fecha actual y el rango
    real de datos cargados, así el LLM no inventa fechas con su training cutoff."""
    from datetime import date as _date
    hoy = _date.today().isoformat()

    # Detectar el rango real de datos disponibles en df_fc.
    rango_datos = "(no hay datos sincronizados)"
    df = ctx.get("df_fc")
    if df is not None and not df.empty and "fecha" in df.columns:
        fechas = pd.to_datetime(df["fecha"], errors="coerce").dropna()
        if not fechas.empty:
            rango_datos = f"{fechas.min().date()} → {fechas.max().date()} ({len(df):,} filas)"

    return f"""Eres un asistente analítico para el dashboard de Gestión de Vendedores de Suprabond Uruguay (GSU).
Tu trabajo: responder preguntas de negocio del Jefe de Ventas usando las herramientas disponibles.

Fecha de hoy: **{hoy}** (usá esta fecha como referencia para "hoy", "este mes", "últimos N meses", etc.)
Rango de datos sincronizados: **{rango_datos}**

Contexto importante:
- Todos los datos son de Suprabond (UY). Moneda UYU. IVA básico 22%.
- Hay vendedores comerciales (los que aparecen en facturación) y "OP" (operativos como OPJESICA, OPVALERIA) que se excluyen del análisis.
- Los SKUs se clasifican por `sub_rubro` (categoría chica) y `familia` (agrupación más grande). Algunos pueden ser COMBOS.
- "Cliente activo" = compró al menos una vez en los últimos N meses.

Reglas de respuesta:
1. Si la pregunta es ambigua, pedí aclaración antes de llamar tools.
2. Si una tool devuelve `error`, mostrá ese error y sugerí alternativa, no inventes datos.
3. **Si te piden un período fuera del rango de datos sincronizados**, decílo claramente y proponé el período disponible. NO consultes fechas vacías una y otra vez.
4. Cuando muestres montos, usá formato UYU "$ 1.234,56".
5. Cuando muestres rankings o listas, mostrá top 10 a menos que pidan otro número.
6. Sé conciso. Una respuesta de 3-5 líneas suele ser ideal. Tabla solo si pidieron muchas filas.
7. Las fechas en formato YYYY-MM-DD para tools, DD/MM/YYYY para mostrar al usuario.
8. Si no podés responder con las tools disponibles, decílo claramente."""


# =====================================================================
# Tool definitions (formato Anthropic tool_use)
# =====================================================================

TOOLS: list[dict] = [
    {
        "name": "get_ventas_por_subgrupo",
        "description": (
            "Devuelve las ventas (suma de monto en UYU) agrupadas por mes "
            "para un subgrupo (sub_rubro) específico, dentro de un rango "
            "de fechas. Útil para preguntas como 'cuál fue el mes que más "
            "se vendió de [subgrupo]'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subgrupo": {
                    "type": "string",
                    "description": "Nombre exacto del sub_rubro (ej. 'ADHESIVOS', 'SILICONAS'). Si no se especifica, agrupa por todos los subgrupos.",
                },
                "fecha_desde": {"type": "string", "description": "YYYY-MM-DD"},
                "fecha_hasta": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["fecha_desde", "fecha_hasta"],
        },
    },
    {
        "name": "get_top_clientes",
        "description": (
            "Top N clientes por monto facturado en un rango de fechas. "
            "Devuelve razón social, documento (RUT), vendedor, monto y unidades."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha_desde": {"type": "string", "description": "YYYY-MM-DD"},
                "fecha_hasta": {"type": "string", "description": "YYYY-MM-DD"},
                "top_n": {"type": "integer", "default": 10, "description": "Cantidad de clientes a devolver."},
            },
            "required": ["fecha_desde", "fecha_hasta"],
        },
    },
    {
        "name": "get_stock_por_sku",
        "description": (
            "Stock actual de un producto por depósito. Hace una llamada "
            "EN VIVO a la API de Contabilium (no usa cache). Devuelve "
            "stock por depósito + total."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "Código del producto (SKU)."},
            },
            "required": ["sku"],
        },
    },
    {
        "name": "get_ventas_por_vendedor",
        "description": (
            "Ranking de vendedores por monto facturado en un rango de fechas. "
            "Excluye vendedores OP (operativos, no comerciales)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha_desde": {"type": "string", "description": "YYYY-MM-DD"},
                "fecha_hasta": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["fecha_desde", "fecha_hasta"],
        },
    },
    {
        "name": "get_clientes_inactivos",
        "description": (
            "Lista clientes de la cartera que NO compraron en los últimos N meses. "
            "Útil para detectar fuga o re-engagement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "meses_sin_compra": {"type": "integer", "default": 3, "description": "Umbral de inactividad."},
                "vendedor": {"type": "string", "description": "Opcional: filtrar a una cartera específica."},
            },
            "required": [],
        },
    },
    {
        "name": "comparar_periodos",
        "description": (
            "Compara totales de venta entre 2 períodos. Devuelve totales, delta absoluto, "
            "delta porcentual y top movers (clientes y productos que más subieron / bajaron). "
            "Útil para 'cómo va este mes vs el anterior', 'año vs año pasado', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "periodo_a_desde": {"type": "string", "description": "Inicio período A (YYYY-MM-DD)"},
                "periodo_a_hasta": {"type": "string", "description": "Fin período A"},
                "periodo_b_desde": {"type": "string", "description": "Inicio período B (de comparación)"},
                "periodo_b_hasta": {"type": "string", "description": "Fin período B"},
            },
            "required": ["periodo_a_desde", "periodo_a_hasta", "periodo_b_desde", "periodo_b_hasta"],
        },
    },
    {
        "name": "get_top_productos",
        "description": (
            "Top N productos (SKUs) por monto facturado en un rango. Devuelve sku, nombre, "
            "monto, unidades, comprobantes distintos y, si hay clasificación, sub_rubro y familia."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha_desde": {"type": "string", "description": "YYYY-MM-DD"},
                "fecha_hasta": {"type": "string", "description": "YYYY-MM-DD"},
                "top_n": {"type": "integer", "default": 10},
                "ordenar_por": {"type": "string", "enum": ["monto", "unidades"], "default": "monto"},
            },
            "required": ["fecha_desde", "fecha_hasta"],
        },
    },
    {
        "name": "get_ventas_cliente",
        "description": (
            "Drill-down de un cliente: total facturado, breakdown por mes y por subgrupo, "
            "y top productos comprados. Identificar el cliente por documento (RUT) O por "
            "razón social parcial (la búsqueda es case-insensitive). Si la búsqueda matchea "
            "múltiples clientes, devuelve un error con la lista para que el LLM elija."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "documento": {"type": "string", "description": "RUT exacto del cliente."},
                "razon_social": {"type": "string", "description": "Texto parcial de la razón social (alternativa a documento)."},
                "fecha_desde": {"type": "string", "description": "YYYY-MM-DD"},
                "fecha_hasta": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["fecha_desde", "fecha_hasta"],
        },
    },
    {
        "name": "get_clientes_nuevos",
        "description": (
            "Lista clientes que compraron por PRIMERA VEZ en un período (no aparecen en "
            "facturación previa al inicio del período). Útil para medir adquisición."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha_desde": {"type": "string", "description": "Inicio del período en cuestión."},
                "fecha_hasta": {"type": "string", "description": "Fin del período en cuestión."},
            },
            "required": ["fecha_desde", "fecha_hasta"],
        },
    },
    {
        "name": "get_evolucion_mensual",
        "description": (
            "Serie mensual de ventas (suma por mes) en un rango amplio. Útil para tendencias. "
            "Opcionalmente filtrable por vendedor, subgrupo o familia."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha_desde": {"type": "string", "description": "YYYY-MM-DD (típicamente 6-24 meses atrás)"},
                "fecha_hasta": {"type": "string", "description": "YYYY-MM-DD"},
                "vendedor": {"type": "string", "description": "Opcional: filtrar por email/nombre del vendedor."},
                "subgrupo": {"type": "string", "description": "Opcional: filtrar por sub_rubro."},
                "familia": {"type": "string", "description": "Opcional: filtrar por familia."},
            },
            "required": ["fecha_desde", "fecha_hasta"],
        },
    },
    {
        "name": "buscar_cliente_o_producto",
        "description": (
            "Utility: busca clientes por razón social (parcial, case-insensitive) o productos "
            "por SKU/nombre (parcial). Devuelve hasta 20 matches con sus identificadores. "
            "USAR CUANDO el usuario menciona un cliente/producto por nombre y necesitás resolver "
            "el RUT/SKU exacto antes de llamar otras tools."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo": {"type": "string", "enum": ["cliente", "producto"]},
                "query": {"type": "string", "description": "Texto a buscar."},
            },
            "required": ["tipo", "query"],
        },
    },
    {
        "name": "get_dimensiones_disponibles",
        "description": (
            "Devuelve listas de las dimensiones cargadas: subgrupos (sub_rubro), familias, "
            "vendedores activos. Llamar al inicio si no estás seguro de qué nombres exactos "
            "usar para las otras tools."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_cobertura_cartera",
        "description": (
            "Para un vendedor (o todos): cuántos clientes de su cartera asignada compraron "
            "en el período / cuántos no. Devuelve total, activos, inactivos y % cobertura."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "vendedor": {"type": "string", "description": "Opcional. Si se omite, devuelve cobertura por todos los vendedores."},
                "fecha_desde": {"type": "string", "description": "YYYY-MM-DD"},
                "fecha_hasta": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["fecha_desde", "fecha_hasta"],
        },
    },
    {
        "name": "get_productos_sin_movimiento",
        "description": (
            "Lista SKUs del maestro de productos que NO se vendieron en los últimos N meses. "
            "Útil para detectar stock muerto / SKUs a discontinuar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "meses": {"type": "integer", "default": 6, "description": "Umbral de inactividad."},
                "subgrupo": {"type": "string", "description": "Opcional: filtrar por sub_rubro."},
            },
            "required": [],
        },
    },
    {
        "name": "get_caidas_significativas",
        "description": (
            "Compara 2 períodos y devuelve clientes (o productos) que cayeron más de X%. "
            "Útil para alertas tempranas de fuga o problemas operativos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "periodo_a_desde": {"type": "string"},
                "periodo_a_hasta": {"type": "string"},
                "periodo_b_desde": {"type": "string"},
                "periodo_b_hasta": {"type": "string"},
                "dimension": {"type": "string", "enum": ["cliente", "producto"], "default": "cliente"},
                "umbral_caida_pct": {"type": "number", "default": 30.0, "description": "Caída mínima en % para incluir."},
                "monto_minimo": {"type": "number", "default": 10000.0, "description": "Filtra ruido: solo entidades con compras > este monto en periodo A."},
            },
            "required": ["periodo_a_desde", "periodo_a_hasta", "periodo_b_desde", "periodo_b_hasta"],
        },
    },
    {
        "name": "get_devoluciones",
        "description": (
            "Notas de crédito (devoluciones) emitidas en un período. Cuenta, monto total, "
            "y top clientes/productos por monto devuelto."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha_desde": {"type": "string"},
                "fecha_hasta": {"type": "string"},
            },
            "required": ["fecha_desde", "fecha_hasta"],
        },
    },
    {
        "name": "get_ticket_metrics",
        "description": (
            "Estadísticas de los tickets (comprobantes únicos) en un período: cantidad, "
            "monto promedio, mediano, mínimo, máximo, P95."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha_desde": {"type": "string"},
                "fecha_hasta": {"type": "string"},
                "vendedor": {"type": "string", "description": "Opcional."},
            },
            "required": ["fecha_desde", "fecha_hasta"],
        },
    },
    {
        "name": "get_mix_ventas",
        "description": (
            "Breakdown porcentual de ventas en un período por una dimensión (subgrupo, "
            "familia o vendedor). Devuelve cada bucket con monto y % del total."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha_desde": {"type": "string"},
                "fecha_hasta": {"type": "string"},
                "dimension": {"type": "string", "enum": ["sub_rubro", "familia", "vendedor"], "default": "sub_rubro"},
            },
            "required": ["fecha_desde", "fecha_hasta"],
        },
    },
]


# =====================================================================
# Tool implementations
# =====================================================================

def _filtrar_periodo(df: pd.DataFrame, fecha_desde: str, fecha_hasta: str) -> pd.DataFrame:
    """Filtra df por columna `fecha` (datetime o str ISO) en [fd, fh]."""
    if df is None or df.empty or "fecha" not in df.columns:
        return df.iloc[0:0] if df is not None else pd.DataFrame()
    fd = pd.to_datetime(fecha_desde)
    fh = pd.to_datetime(fecha_hasta) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    fechas = pd.to_datetime(df["fecha"], errors="coerce")
    return df[(fechas >= fd) & (fechas <= fh)]


def get_ventas_por_subgrupo(args: dict, ctx: dict) -> dict:
    df = ctx.get("df_fc")
    if df is None or df.empty:
        return {"error": "No hay datos de facturación cargados. Sincronizá desde la sidebar."}
    if "sub_rubro" not in df.columns:
        return {"error": "El df de facturación no tiene la columna sub_rubro."}

    df = _filtrar_periodo(df, args["fecha_desde"], args["fecha_hasta"])
    subgrupo = args.get("subgrupo")
    if subgrupo:
        df = df[df["sub_rubro"].astype(str).str.upper() == subgrupo.upper()]
        if df.empty:
            subs_disponibles = sorted(set(ctx["df_fc"]["sub_rubro"].dropna().astype(str).unique()))[:30]
            return {
                "error": f"No hay ventas del subgrupo '{subgrupo}' en el período.",
                "subgrupos_disponibles": subs_disponibles,
            }

    df = df.copy()
    df["mes"] = pd.to_datetime(df["fecha"]).dt.strftime("%Y-%m")
    if subgrupo:
        agg = df.groupby("mes", as_index=False)["monto"].sum()
        agg = agg.sort_values("monto", ascending=False)
    else:
        agg = df.groupby(["mes", "sub_rubro"], as_index=False)["monto"].sum()
        agg = agg.sort_values(["mes", "monto"], ascending=[True, False])

    return {
        "subgrupo": subgrupo or "TODOS",
        "rango": f"{args['fecha_desde']} → {args['fecha_hasta']}",
        "filas": agg.head(50).to_dict(orient="records"),
        "total_uyu": float(agg["monto"].sum()) if not agg.empty else 0.0,
    }


def get_top_clientes(args: dict, ctx: dict) -> dict:
    df = ctx.get("df_fc")
    if df is None or df.empty:
        return {"error": "No hay datos de facturación cargados."}
    df = _filtrar_periodo(df, args["fecha_desde"], args["fecha_hasta"])
    if df.empty:
        return {"error": "No hay ventas en el período."}

    top_n = int(args.get("top_n") or 10)
    agg = df.groupby(["documento", "razon_social"], as_index=False).agg(
        monto=("monto", "sum"),
        unidades=("unidades", "sum"),
        comprobantes=("id_comprobante", "nunique"),
    )
    if "vendedor" in df.columns:
        vend_por_doc = df.groupby("documento")["vendedor"].agg(lambda s: ", ".join(sorted(set(s.dropna().astype(str)))))
        agg["vendedor"] = agg["documento"].map(vend_por_doc)
    agg = agg.sort_values("monto", ascending=False).head(top_n)
    return {
        "rango": f"{args['fecha_desde']} → {args['fecha_hasta']}",
        "top_n": top_n,
        "filas": agg.to_dict(orient="records"),
    }


def get_stock_por_sku(args: dict, ctx: dict) -> dict:
    sku = (args.get("sku") or "").strip()
    if not sku:
        return {"error": "Falta el SKU."}
    session = ctx.get("api_session")
    if session is None:
        return {"error": "No hay sesión activa de Contabilium. Sincronizá primero."}

    try:
        path = f"/api/inventarios/getStockBySKU?codigo={sku}"
        session, payload = api_loader.api_get(session, path)
        ctx["api_session"] = session  # actualizar token si se refrescó
    except api_loader.ApiError as e:
        return {"error": f"Error consultando stock: {e}"}

    if not isinstance(payload, list):
        return {"error": f"Respuesta inesperada: {payload}"}

    total = sum(float(it.get("Stock") or it.get("stock") or 0) for it in payload)
    return {
        "sku": sku,
        "total": total,
        "por_deposito": [
            {
                "deposito": it.get("Deposito") or it.get("NombreDeposito") or it.get("nombre"),
                "stock": float(it.get("Stock") or it.get("stock") or 0),
            }
            for it in payload
        ],
    }


def get_ventas_por_vendedor(args: dict, ctx: dict) -> dict:
    df = ctx.get("df_fc")
    if df is None or df.empty or "vendedor" not in df.columns:
        return {"error": "No hay datos de facturación con vendedor."}
    df = _filtrar_periodo(df, args["fecha_desde"], args["fecha_hasta"])
    if df.empty:
        return {"error": "No hay ventas en el período."}

    # OP excluidos ya vienen filtrados de prepare_facturacion, pero por las dudas:
    excluidos = {"OPJESICA@SUPRABOND.COM.UY", "OPVALERIA@SUPRABOND.COM.UY"}
    df = df[~df["vendedor"].astype(str).str.upper().isin(excluidos)]

    agg = df.groupby("vendedor", as_index=False).agg(
        monto=("monto", "sum"),
        unidades=("unidades", "sum"),
        clientes_distintos=("documento", "nunique"),
    )
    agg = agg.sort_values("monto", ascending=False)
    return {
        "rango": f"{args['fecha_desde']} → {args['fecha_hasta']}",
        "filas": agg.to_dict(orient="records"),
        "total_uyu": float(agg["monto"].sum()),
    }


def get_clientes_inactivos(args: dict, ctx: dict) -> dict:
    df_fc = ctx.get("df_fc")
    df_clientes = ctx.get("df_clientes")
    if df_clientes is None or df_clientes.empty:
        return {"error": "No hay maestro de clientes cargado."}

    meses = int(args.get("meses_sin_compra") or 3)
    cutoff = pd.Timestamp(date.today()) - pd.DateOffset(months=meses)
    vendedor_filtro = (args.get("vendedor") or "").strip().upper()

    if df_fc is None or df_fc.empty:
        # Si no hay facturación, todos son "inactivos".
        ultima_compra_por_doc: dict[str, pd.Timestamp] = {}
    else:
        df_fc = df_fc.copy()
        df_fc["fecha"] = pd.to_datetime(df_fc["fecha"], errors="coerce")
        ultimas = df_fc.groupby("documento")["fecha"].max()
        ultima_compra_por_doc = ultimas.to_dict()

    filas = []
    for _, c in df_clientes.iterrows():
        doc = str(c.get("documento") or "")
        if not doc:
            continue
        if vendedor_filtro and (c.get("vendedor") or "").upper() != vendedor_filtro:
            continue
        ultima = ultima_compra_por_doc.get(doc)
        if ultima is None or pd.isna(ultima) or ultima < cutoff:
            filas.append({
                "documento": doc,
                "razon_social": c.get("razon_social", ""),
                "vendedor": c.get("vendedor", ""),
                "ultima_compra": ultima.strftime("%Y-%m-%d") if (ultima is not None and not pd.isna(ultima)) else None,
            })

    filas.sort(key=lambda r: (r["ultima_compra"] or "", r["razon_social"]))
    return {
        "meses_sin_compra": meses,
        "vendedor_filtro": vendedor_filtro or None,
        "total_inactivos": len(filas),
        "filas": filas[:50],  # cap para no saturar el contexto del LLM
    }


# =====================================================================
# Tools nuevas (Sprint Asistente parte 2)
# =====================================================================

def _agg_total(df: pd.DataFrame) -> dict:
    """Helper: total monto + unidades + comprobantes distintos en df."""
    if df is None or df.empty:
        return {"monto": 0.0, "unidades": 0.0, "comprobantes": 0}
    return {
        "monto": float(df["monto"].sum()),
        "unidades": float(df["unidades"].sum()) if "unidades" in df.columns else 0.0,
        "comprobantes": int(df["id_comprobante"].nunique()) if "id_comprobante" in df.columns else 0,
    }


def comparar_periodos(args: dict, ctx: dict) -> dict:
    df = ctx.get("df_fc")
    if df is None or df.empty:
        return {"error": "No hay datos de facturación cargados."}
    df_a = _filtrar_periodo(df, args["periodo_a_desde"], args["periodo_a_hasta"])
    df_b = _filtrar_periodo(df, args["periodo_b_desde"], args["periodo_b_hasta"])
    tot_a = _agg_total(df_a)
    tot_b = _agg_total(df_b)
    delta_abs = tot_a["monto"] - tot_b["monto"]
    delta_pct = (delta_abs / tot_b["monto"] * 100.0) if tot_b["monto"] else None

    # Top movers por cliente.
    top_movers_clientes = []
    if not df_a.empty or not df_b.empty:
        agg_a = df_a.groupby("documento")["monto"].sum() if not df_a.empty else pd.Series(dtype=float)
        agg_b = df_b.groupby("documento")["monto"].sum() if not df_b.empty else pd.Series(dtype=float)
        all_docs = set(agg_a.index) | set(agg_b.index)
        rows = []
        razon_por_doc = (
            df.drop_duplicates("documento").set_index("documento")["razon_social"].to_dict()
        )
        for doc in all_docs:
            ma = float(agg_a.get(doc, 0.0))
            mb = float(agg_b.get(doc, 0.0))
            rows.append({
                "documento": doc,
                "razon_social": razon_por_doc.get(doc, ""),
                "monto_a": ma,
                "monto_b": mb,
                "delta": ma - mb,
            })
        rows.sort(key=lambda r: abs(r["delta"]), reverse=True)
        top_movers_clientes = rows[:10]

    return {
        "periodo_a": {"desde": args["periodo_a_desde"], "hasta": args["periodo_a_hasta"], **tot_a},
        "periodo_b": {"desde": args["periodo_b_desde"], "hasta": args["periodo_b_hasta"], **tot_b},
        "delta_abs": delta_abs,
        "delta_pct": delta_pct,
        "top_movers_clientes": top_movers_clientes,
    }


def get_top_productos(args: dict, ctx: dict) -> dict:
    df = ctx.get("df_fc")
    if df is None or df.empty:
        return {"error": "No hay datos de facturación cargados."}
    df = _filtrar_periodo(df, args["fecha_desde"], args["fecha_hasta"])
    if df.empty:
        return {"error": "No hay ventas en el período."}

    top_n = int(args.get("top_n") or 10)
    ordenar_por = args.get("ordenar_por", "monto")

    group_cols = ["sku"]
    if "producto" in df.columns:
        group_cols.append("producto")
    agg = df.groupby(group_cols, as_index=False).agg(
        monto=("monto", "sum"),
        unidades=("unidades", "sum"),
        comprobantes=("id_comprobante", "nunique"),
    )
    if "sub_rubro" in df.columns:
        sub_por_sku = df.drop_duplicates("sku").set_index("sku")["sub_rubro"].to_dict()
        agg["sub_rubro"] = agg["sku"].map(sub_por_sku)
    if "familia" in df.columns:
        fam_por_sku = df.drop_duplicates("sku").set_index("sku")["familia"].to_dict()
        agg["familia"] = agg["sku"].map(fam_por_sku)
    sort_col = ordenar_por if ordenar_por in agg.columns else "monto"
    agg = agg.sort_values(sort_col, ascending=False).head(top_n)
    return {
        "rango": f"{args['fecha_desde']} → {args['fecha_hasta']}",
        "top_n": top_n,
        "ordenar_por": sort_col,
        "filas": agg.to_dict(orient="records"),
    }


def get_ventas_cliente(args: dict, ctx: dict) -> dict:
    df = ctx.get("df_fc")
    if df is None or df.empty:
        return {"error": "No hay datos de facturación cargados."}

    documento = (args.get("documento") or "").strip()
    razon = (args.get("razon_social") or "").strip()
    if not documento and not razon:
        return {"error": "Necesito documento o razon_social para identificar al cliente."}

    df_p = _filtrar_periodo(df, args["fecha_desde"], args["fecha_hasta"])

    # Resolver el cliente.
    if documento:
        df_cli = df_p[df_p["documento"].astype(str) == documento]
        identificacion = {"documento": documento}
    else:
        mask = df_p["razon_social"].astype(str).str.upper().str.contains(razon.upper(), na=False)
        df_cli = df_p[mask]
        # Si matchean varios docs distintos, ambiguo.
        docs_match = df_cli["documento"].dropna().unique().tolist()
        if len(docs_match) > 1:
            opciones = (
                df_cli.drop_duplicates("documento")[["documento", "razon_social"]]
                .head(20).to_dict(orient="records")
            )
            return {
                "error": f"La búsqueda '{razon}' matchea {len(docs_match)} clientes distintos. "
                         "Pasame documento exacto o razón social más específica.",
                "opciones": opciones,
            }
        identificacion = {"razon_social_query": razon, "documento_resuelto": docs_match[0] if docs_match else None}

    if df_cli.empty:
        return {"error": "No hay compras en el período para ese cliente.", **identificacion}

    razon_real = df_cli["razon_social"].iloc[0]
    total = _agg_total(df_cli)

    # Por mes.
    df_cli = df_cli.copy()
    df_cli["mes"] = pd.to_datetime(df_cli["fecha"]).dt.strftime("%Y-%m")
    por_mes = df_cli.groupby("mes", as_index=False)["monto"].sum().to_dict(orient="records")

    # Por subgrupo (si hay).
    por_sub = []
    if "sub_rubro" in df_cli.columns:
        por_sub = (
            df_cli.groupby("sub_rubro", as_index=False)["monto"].sum()
            .sort_values("monto", ascending=False).head(10).to_dict(orient="records")
        )

    # Top productos del cliente.
    top_prods = (
        df_cli.groupby(["sku", "producto"], as_index=False)["monto"].sum()
        .sort_values("monto", ascending=False).head(10).to_dict(orient="records")
    )

    return {
        "cliente": razon_real,
        **identificacion,
        "rango": f"{args['fecha_desde']} → {args['fecha_hasta']}",
        "total": total,
        "por_mes": por_mes,
        "por_subgrupo": por_sub,
        "top_productos": top_prods,
    }


def get_clientes_nuevos(args: dict, ctx: dict) -> dict:
    df = ctx.get("df_fc")
    if df is None or df.empty:
        return {"error": "No hay datos de facturación cargados."}

    fd = pd.to_datetime(args["fecha_desde"])
    fh = pd.to_datetime(args["fecha_hasta"]) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    fechas = pd.to_datetime(df["fecha"], errors="coerce")

    # Clientes que aparecieron por primera vez dentro del rango: su
    # primera_compra está en [fd, fh] y NO tienen compras anteriores a fd.
    primera_por_doc = df.groupby("documento")["fecha"].min()
    primera_por_doc = pd.to_datetime(primera_por_doc, errors="coerce")
    nuevos_docs = primera_por_doc[(primera_por_doc >= fd) & (primera_por_doc <= fh)].index.tolist()

    if not nuevos_docs:
        return {"total": 0, "filas": [], "rango": f"{args['fecha_desde']} → {args['fecha_hasta']}"}

    df_nuevos = df[df["documento"].isin(nuevos_docs)]
    df_nuevos_periodo = df_nuevos[(fechas >= fd) & (fechas <= fh)]

    agg = df_nuevos_periodo.groupby(["documento", "razon_social"], as_index=False).agg(
        monto=("monto", "sum"),
        comprobantes=("id_comprobante", "nunique"),
    )
    if "vendedor" in df_nuevos_periodo.columns:
        vmap = df_nuevos_periodo.drop_duplicates("documento").set_index("documento")["vendedor"]
        agg["vendedor"] = agg["documento"].map(vmap)
    agg["primera_compra"] = agg["documento"].map(
        lambda d: primera_por_doc.get(d).strftime("%Y-%m-%d") if pd.notna(primera_por_doc.get(d)) else ""
    )
    agg = agg.sort_values("monto", ascending=False)
    return {
        "rango": f"{args['fecha_desde']} → {args['fecha_hasta']}",
        "total": len(agg),
        "monto_total_uyu": float(agg["monto"].sum()),
        "filas": agg.head(50).to_dict(orient="records"),
    }


def get_evolucion_mensual(args: dict, ctx: dict) -> dict:
    df = ctx.get("df_fc")
    if df is None or df.empty:
        return {"error": "No hay datos de facturación cargados."}
    df = _filtrar_periodo(df, args["fecha_desde"], args["fecha_hasta"])
    if df.empty:
        return {"error": "No hay ventas en el período."}

    # Filtros opcionales.
    if args.get("vendedor"):
        df = df[df["vendedor"].astype(str).str.upper().str.contains(args["vendedor"].upper(), na=False)]
    if args.get("subgrupo") and "sub_rubro" in df.columns:
        df = df[df["sub_rubro"].astype(str).str.upper() == args["subgrupo"].upper()]
    if args.get("familia") and "familia" in df.columns:
        df = df[df["familia"].astype(str).str.upper() == args["familia"].upper()]

    if df.empty:
        return {"error": "Después de aplicar los filtros no quedan ventas."}

    df = df.copy()
    df["mes"] = pd.to_datetime(df["fecha"]).dt.strftime("%Y-%m")
    serie = df.groupby("mes", as_index=False).agg(
        monto=("monto", "sum"),
        unidades=("unidades", "sum"),
        comprobantes=("id_comprobante", "nunique"),
    ).sort_values("mes")
    return {
        "rango": f"{args['fecha_desde']} → {args['fecha_hasta']}",
        "filtros": {k: args.get(k) for k in ("vendedor", "subgrupo", "familia") if args.get(k)},
        "serie": serie.to_dict(orient="records"),
    }


def buscar_cliente_o_producto(args: dict, ctx: dict) -> dict:
    tipo = args.get("tipo")
    query = (args.get("query") or "").strip().upper()
    if not query:
        return {"error": "Falta query."}

    if tipo == "cliente":
        df_cli = ctx.get("df_clientes")
        if df_cli is None or df_cli.empty:
            return {"error": "No hay maestro de clientes."}
        mask = (
            df_cli["razon_social"].astype(str).str.upper().str.contains(query, na=False)
            | df_cli["documento"].astype(str).str.upper().str.contains(query, na=False)
        )
        matches = df_cli[mask].head(20)
        return {
            "tipo": "cliente",
            "query": query,
            "total_matches": int(mask.sum()),
            "filas": matches[["documento", "razon_social", "vendedor"]].to_dict(orient="records"),
        }

    if tipo == "producto":
        df_prod = ctx.get("df_productos")
        if df_prod is None or df_prod.empty:
            return {"error": "No hay maestro de productos."}
        cols_disp = [c for c in ("sku", "nombre") if c in df_prod.columns]
        mask = pd.Series(False, index=df_prod.index)
        for c in cols_disp:
            mask = mask | df_prod[c].astype(str).str.upper().str.contains(query, na=False)
        matches = df_prod[mask].head(20)
        cols_out = [c for c in ("sku", "nombre", "stock", "precio") if c in matches.columns]
        return {
            "tipo": "producto",
            "query": query,
            "total_matches": int(mask.sum()),
            "filas": matches[cols_out].to_dict(orient="records"),
        }

    return {"error": f"Tipo desconocido: {tipo}. Usar 'cliente' o 'producto'."}


def get_dimensiones_disponibles(args: dict, ctx: dict) -> dict:
    df = ctx.get("df_fc")
    out: dict = {}
    if df is not None and not df.empty:
        if "sub_rubro" in df.columns:
            out["subgrupos"] = sorted(set(df["sub_rubro"].dropna().astype(str)))[:200]
        if "familia" in df.columns:
            out["familias"] = sorted(set(df["familia"].dropna().astype(str)))[:200]
        if "vendedor" in df.columns:
            out["vendedores_con_ventas"] = sorted(set(df["vendedor"].dropna().astype(str)))[:200]
    df_cli = ctx.get("df_clientes")
    if df_cli is not None and not df_cli.empty and "vendedor" in df_cli.columns:
        out["vendedores_en_cartera"] = sorted(set(df_cli["vendedor"].dropna().astype(str)))[:200]
    if not out:
        return {"error": "No hay datos suficientes para listar dimensiones."}
    return out


def get_cobertura_cartera(args: dict, ctx: dict) -> dict:
    df = ctx.get("df_fc")
    df_cli = ctx.get("df_clientes")
    if df_cli is None or df_cli.empty:
        return {"error": "No hay maestro de clientes."}
    if df is None or df.empty:
        return {"error": "No hay datos de facturación cargados."}

    df_p = _filtrar_periodo(df, args["fecha_desde"], args["fecha_hasta"])
    docs_que_compraron = set(df_p["documento"].dropna().astype(str))

    vendedor_filtro = (args.get("vendedor") or "").strip().upper()
    df_target = df_cli
    if vendedor_filtro:
        df_target = df_cli[df_cli["vendedor"].astype(str).str.upper() == vendedor_filtro]
        if df_target.empty:
            return {"error": f"No hay clientes en cartera de '{vendedor_filtro}'."}

    if vendedor_filtro:
        # Una sola fila.
        total_cart = len(df_target)
        activos = sum(1 for d in df_target["documento"].astype(str) if d in docs_que_compraron)
        return {
            "vendedor": vendedor_filtro,
            "rango": f"{args['fecha_desde']} → {args['fecha_hasta']}",
            "clientes_en_cartera": total_cart,
            "clientes_activos": activos,
            "clientes_inactivos": total_cart - activos,
            "cobertura_pct": round(activos / total_cart * 100.0, 1) if total_cart else 0.0,
        }

    # Por vendedor.
    rows = []
    for vend, sub in df_cli.groupby("vendedor"):
        total_cart = len(sub)
        activos = sum(1 for d in sub["documento"].astype(str) if d in docs_que_compraron)
        rows.append({
            "vendedor": vend,
            "clientes_en_cartera": total_cart,
            "clientes_activos": activos,
            "clientes_inactivos": total_cart - activos,
            "cobertura_pct": round(activos / total_cart * 100.0, 1) if total_cart else 0.0,
        })
    rows.sort(key=lambda r: r["cobertura_pct"], reverse=True)
    return {
        "rango": f"{args['fecha_desde']} → {args['fecha_hasta']}",
        "filas": rows,
    }


def get_productos_sin_movimiento(args: dict, ctx: dict) -> dict:
    df_prod = ctx.get("df_productos")
    df = ctx.get("df_fc")
    if df_prod is None or df_prod.empty:
        return {"error": "No hay maestro de productos."}

    meses = int(args.get("meses") or 6)
    cutoff = pd.Timestamp(date.today()) - pd.DateOffset(months=meses)
    skus_con_venta = set()
    if df is not None and not df.empty:
        df_recent = df[pd.to_datetime(df["fecha"], errors="coerce") >= cutoff]
        skus_con_venta = set(df_recent["sku"].dropna().astype(str))

    sin_mov = df_prod[~df_prod["sku"].astype(str).isin(skus_con_venta)]
    if args.get("subgrupo") and "sub_rubro" in sin_mov.columns:
        sin_mov = sin_mov[sin_mov["sub_rubro"].astype(str).str.upper() == args["subgrupo"].upper()]

    cols = [c for c in ("sku", "nombre", "stock", "precio", "sub_rubro") if c in sin_mov.columns]
    return {
        "meses": meses,
        "subgrupo_filtro": args.get("subgrupo"),
        "total_sin_movimiento": int(len(sin_mov)),
        "filas": sin_mov[cols].head(100).to_dict(orient="records"),
    }


def get_caidas_significativas(args: dict, ctx: dict) -> dict:
    df = ctx.get("df_fc")
    if df is None or df.empty:
        return {"error": "No hay datos de facturación cargados."}
    df_a = _filtrar_periodo(df, args["periodo_a_desde"], args["periodo_a_hasta"])
    df_b = _filtrar_periodo(df, args["periodo_b_desde"], args["periodo_b_hasta"])

    dim = args.get("dimension", "cliente")
    umbral = float(args.get("umbral_caida_pct") or 30.0)
    monto_min = float(args.get("monto_minimo") or 10000.0)

    if dim == "cliente":
        key = "documento"
        label_col = "razon_social"
    elif dim == "producto":
        key = "sku"
        label_col = "producto"
    else:
        return {"error": f"Dimensión inválida: {dim}"}

    agg_a = df_a.groupby(key)["monto"].sum() if not df_a.empty else pd.Series(dtype=float)
    agg_b = df_b.groupby(key)["monto"].sum() if not df_b.empty else pd.Series(dtype=float)

    label_map = {}
    for d in (df_a, df_b):
        if not d.empty and label_col in d.columns:
            for k, v in d.drop_duplicates(key).set_index(key)[label_col].to_dict().items():
                label_map.setdefault(k, v)

    rows = []
    for k in agg_a.index:
        ma = float(agg_a.get(k, 0.0))
        mb = float(agg_b.get(k, 0.0))
        if ma < monto_min:  # ruido
            continue
        if ma == 0:
            continue
        delta = mb - ma
        delta_pct = (delta / ma) * 100.0
        if delta_pct <= -umbral:
            rows.append({
                key: k,
                label_col: label_map.get(k, ""),
                "monto_periodo_a": ma,
                "monto_periodo_b": mb,
                "delta_pct": round(delta_pct, 1),
            })
    rows.sort(key=lambda r: r["delta_pct"])  # más caída primero
    return {
        "dimension": dim,
        "umbral_caida_pct": umbral,
        "monto_minimo": monto_min,
        "total_caidas": len(rows),
        "filas": rows[:50],
    }


def get_devoluciones(args: dict, ctx: dict) -> dict:
    df = ctx.get("df_fc")
    if df is None or df.empty:
        return {"error": "No hay datos de facturación cargados."}
    df = _filtrar_periodo(df, args["fecha_desde"], args["fecha_hasta"])
    if "tipo" not in df.columns:
        return {"error": "El df no tiene columna 'tipo' para detectar NCF."}
    nc = df[df["tipo"].astype(str).str.upper().str.startswith("NC")]
    if nc.empty:
        return {"total_ncf": 0, "monto_devuelto": 0.0, "filas": []}
    monto = float(nc["monto"].sum())  # negativo (notas de crédito)
    top_clientes = (
        nc.groupby(["documento", "razon_social"], as_index=False)["monto"].sum()
        .sort_values("monto").head(10).to_dict(orient="records")
    )
    top_productos = []
    if "sku" in nc.columns:
        top_productos = (
            nc.groupby(["sku", "producto"], as_index=False)["monto"].sum()
            .sort_values("monto").head(10).to_dict(orient="records")
        )
    return {
        "rango": f"{args['fecha_desde']} → {args['fecha_hasta']}",
        "total_ncf": int(nc["id_comprobante"].nunique()),
        "monto_devuelto": monto,
        "top_clientes_devolucion": top_clientes,
        "top_productos_devueltos": top_productos,
    }


def get_ticket_metrics(args: dict, ctx: dict) -> dict:
    df = ctx.get("df_fc")
    if df is None or df.empty:
        return {"error": "No hay datos de facturación cargados."}
    df = _filtrar_periodo(df, args["fecha_desde"], args["fecha_hasta"])
    if df.empty:
        return {"error": "No hay ventas en el período."}
    if args.get("vendedor"):
        df = df[df["vendedor"].astype(str).str.upper().str.contains(args["vendedor"].upper(), na=False)]
        if df.empty:
            return {"error": "No hay ventas para ese vendedor en el período."}
    montos_por_ticket = df.groupby("id_comprobante")["monto"].sum()
    return {
        "rango": f"{args['fecha_desde']} → {args['fecha_hasta']}",
        "vendedor_filtro": args.get("vendedor"),
        "tickets": int(len(montos_por_ticket)),
        "monto_total": float(montos_por_ticket.sum()),
        "promedio": float(montos_por_ticket.mean()),
        "mediano": float(montos_por_ticket.median()),
        "minimo": float(montos_por_ticket.min()),
        "maximo": float(montos_por_ticket.max()),
        "p95": float(montos_por_ticket.quantile(0.95)),
    }


def get_mix_ventas(args: dict, ctx: dict) -> dict:
    df = ctx.get("df_fc")
    if df is None or df.empty:
        return {"error": "No hay datos de facturación cargados."}
    df = _filtrar_periodo(df, args["fecha_desde"], args["fecha_hasta"])
    if df.empty:
        return {"error": "No hay ventas en el período."}
    dim = args.get("dimension", "sub_rubro")
    if dim not in df.columns:
        return {"error": f"La columna '{dim}' no existe en el df."}
    agg = df.groupby(dim, as_index=False)["monto"].sum().sort_values("monto", ascending=False)
    total = float(agg["monto"].sum())
    if total == 0:
        return {"error": "Total de ventas en período = 0."}
    agg["pct"] = (agg["monto"] / total * 100.0).round(2)
    return {
        "rango": f"{args['fecha_desde']} → {args['fecha_hasta']}",
        "dimension": dim,
        "total_uyu": total,
        "filas": agg.to_dict(orient="records"),
    }


_TOOL_FUNCTIONS = {
    "get_ventas_por_subgrupo": get_ventas_por_subgrupo,
    "get_top_clientes": get_top_clientes,
    "get_stock_por_sku": get_stock_por_sku,
    "get_ventas_por_vendedor": get_ventas_por_vendedor,
    "get_clientes_inactivos": get_clientes_inactivos,
    "comparar_periodos": comparar_periodos,
    "get_top_productos": get_top_productos,
    "get_ventas_cliente": get_ventas_cliente,
    "get_clientes_nuevos": get_clientes_nuevos,
    "get_evolucion_mensual": get_evolucion_mensual,
    "buscar_cliente_o_producto": buscar_cliente_o_producto,
    "get_dimensiones_disponibles": get_dimensiones_disponibles,
    "get_cobertura_cartera": get_cobertura_cartera,
    "get_productos_sin_movimiento": get_productos_sin_movimiento,
    "get_caidas_significativas": get_caidas_significativas,
    "get_devoluciones": get_devoluciones,
    "get_ticket_metrics": get_ticket_metrics,
    "get_mix_ventas": get_mix_ventas,
}


# =====================================================================
# Loop conversacional
# =====================================================================

def _serialize_tool_result(result: Any) -> str:
    """Convierte un dict/list con posibles tipos pandas a JSON string."""
    def _default(o):
        if isinstance(o, (pd.Timestamp, datetime, date)):
            return o.isoformat()
        if hasattr(o, "item"):
            return o.item()  # numpy scalars
        return str(o)
    return json.dumps(result, default=_default, ensure_ascii=False)


def responder(
    messages: list[dict],
    ctx: dict,
    api_key: str,
) -> tuple[str, list[dict]]:
    """Loop con tool use. Devuelve (texto_final, tool_calls_log).

    `messages` se mutará in-place — agrega assistant + tool_result rounds.
    `ctx` debe tener: df_fc (DataFrame procesado), df_clientes, df_productos,
    api_session (ApiSession para consultas live como stock).
    """
    client = anthropic.Anthropic(api_key=api_key)
    tool_calls_log: list[dict] = []
    system_prompt = _build_system_prompt(ctx)

    for _ in range(MAX_TOOL_LOOPS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        # Agregar respuesta del assistant al historial.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Texto final.
            text_blocks = [b.text for b in response.content if getattr(b, "type", "") == "text"]
            return "\n\n".join(text_blocks).strip(), tool_calls_log

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                fn = _TOOL_FUNCTIONS.get(block.name)
                if fn is None:
                    result = {"error": f"Tool desconocida: {block.name}"}
                else:
                    try:
                        result = fn(dict(block.input), ctx)
                    except Exception as exc:
                        result = {"error": f"Excepción ejecutando {block.name}: {exc}"}
                tool_calls_log.append({
                    "tool": block.name,
                    "input": dict(block.input),
                    "result_preview": _serialize_tool_result(result)[:500],
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": _serialize_tool_result(result),
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        # stop_reason inesperado (max_tokens, refusal, ...): cortamos.
        text_blocks = [b.text for b in response.content if getattr(b, "type", "") == "text"]
        return (
            "\n\n".join(text_blocks).strip()
            or f"(El modelo cortó con stop_reason={response.stop_reason})",
            tool_calls_log,
        )

    return (
        "(Excedí el máximo de iteraciones de tool use. Reformulá la pregunta.)",
        tool_calls_log,
    )
