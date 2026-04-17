"""
subrubros.py — Mapping IdRubro / IdSubrubro (Contabilium) → código corto.

En el pipeline original basado en xlsx, los productos vienen con
`Sub Rubro` como código corto ("A", "BA", "PM", etc.). En la API de
Contabilium, el mismo dato es un entero (`IdSubrubro`, ej. 2644). Este
archivo mantiene el mapping para preservar la continuidad del
dashboard y del histórico de análisis.

Cómo poblarlo:

    1. Correr el script
       `_exploracion-api-contabilium/derivar_mappings.py`.
       Cruza `productos.xlsx` (si está disponible) con la API por
       `Codigo`/`SKU` e infiere IdSubrubro → código corto
       automáticamente. Imprime dicts listos para pegar acá.

    2. Si no hay productos.xlsx a mano, se puede poblar manualmente
       usando `api_loader.listar_subrubros_desde_productos(session)`
       que devuelve, para cada IdSubrubro observado, un sample de
       SKUs y nombres de productos — de ahí se infiere el código.

    3. El mapping definitivo ideal vendría del endpoint
       `GET /api/common/SubRubros` (ver sidebar Postman folder
       "Common"), pero no tenemos la URL exacta aún. Si se confirma
       esa URL en el futuro, se puede reemplazar este dict por una
       función que lo pullea de la API.

Uso en el pipeline:

    - `api_loader.load_productos_api(session, subrubros_map=SUBRUBROS,
       rubros_map=RUBROS)`
    - IDs no mapeados caen en fallback "ID_<n>".
"""

from __future__ import annotations

# IdSubrubro (int) → código corto del xlsx ("A", "BA", "PM", etc.).
# Derivado el 2026-04-17 cruzando productos.xlsx vs /api/conceptos/search
# por SKU. Ratio 100% por ID (sin ambigüedad interna).
#
# Nota: los IdSubrubro 2648 y 2656 mapean AMBOS al código 'GI'. En
# Contabilium son dos subrubros distintos que en el xlsx se consolidan
# bajo la misma etiqueta. El mapping es determinístico (2648→'GI' y
# 2656→'GI') así que no hay problema funcional.
#
# Los productos con IdSubrubro=0 (85 productos en marzo 2026) no tienen
# subrubro asignado en Contabilium. Intencionalmente NO se listan acá
# para que caigan al fallback "ID_0" en `load_productos_api` y el panel
# de salud los pueda reportar como "sin clasificar", respetando el
# comportamiento actual del xlsx (donde esos productos tienen
# `Sub Rubro = NaN`).
SUBRUBROS: dict[int, str] = {
    2644: "A",
    2648: "GI",
    2649: "H",
    2650: "P",
    2651: "S",
    2652: "BA",
    2653: "BC",
    2655: "BH",
    2656: "GI",
    2932: "SC",
    3898: "B JD",
    3899: "B ACC",
    3900: "B ME",
    3902: "DM",
}

# IdRubro (int) → nombre del rubro padre.
# Derivado en la misma pasada que SUBRUBROS. Ratio 100%.
RUBROS: dict[int, str] = {
    1594: "BULIT",
    1595: "SUPRABOND",
    1596: "INSUMOS",
    1597: "MARKETING",
    1819: "SOMERSET",
    2009: "AQUALAF",
    2010: "Peirano",
    3882: "DREMEL",
    3883: "Bosch",
    4906: "General",
}
