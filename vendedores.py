"""
vendedores.py — Mapping IDVendedor (Contabilium) → email/nombre canónico.

La API de Contabilium NO expone un maestro de vendedores / usuarios
adicionales, así que este mapping se mantiene manual en código. El
conjunto cambia pocas veces al año (alta/baja de un comercial).

Cómo actualizarlo:

    1. Correr el script
       `_exploracion-api-contabilium/derivar_mappings.py`
       para un mes reciente. El script cruza facturación xlsx vs API
       por `Numero` de comprobante y deduce automáticamente el mapping
       IDVendedor → email. Imprime un dict listo para pegar acá.

    2. Cuando se da de alta un vendedor nuevo, correr el script el
       primer mes donde haya facturado, agregar la entrada al dict de
       abajo, y pushear al repo.

Uso en el pipeline:

    - `api_loader.load_clientes_api(session, vendedores_map=VENDEDORES)`
    - `api_loader.load_fc_api(session, ..., vendedores_map=VENDEDORES)`
    - Cualquier IDVendedor no mapeado cae en fallback "ID_<n>" en el
      DataFrame resultante, así es trivial ver qué falta mapear.

Exclusión de cuentas operativas:

    `VENDEDORES_OP_EXCLUIDOS` es el reemplazo API-nativo del conjunto
    actual `{'OPJESICA@SUPRABOND.COM.UY', 'OPVALERIA@SUPRABOND.COM.UY'}`
    que se filtra en `transforms.exclude_op_vendedores`. Cuando
    migremos el pipeline a API (Tanda E), `transforms.py` va a comparar
    por ID en lugar de por email. Mantener ambos en sync.

Ver `_learning/decisions.md` entrada 2026-04-10 para el contexto de
por qué estas cuentas se excluyen de todas las métricas.
"""

from __future__ import annotations

# Mapping IDVendedor → email/nombre canónico.
# Derivado el 2026-04-17 cruzando fc_mensual.xlsx de marzo 2026 vs API
# por Numero de comprobante — ratio de match 100% (cada ID tiene un solo
# email asociado, sin ambigüedad).
VENDEDORES: dict[int, str] = {
    232: "OPJESICA@SUPRABOND.COM.UY",
    237: "MARIO@SUPRABOND.COM.UY",
    260: "OPVALERIA@SUPRABOND.COM.UY",
    292: "CARLOS@SUPRABOND.COM.UY",
    346: "ARTURO@SUPRABOND.COM.UY",
    366: "MARCELO@SUPRABOND.COM.UY",
    506: "DAVID@SUPRABOND.COM.UY",
    666: "NESTOR@SUPRABOND.COM.UY",
}

# IDs de cuentas operativas/administrativas cuyas filas NO se
# contabilizan en NINGUNA métrica del dashboard.
# Equivalente por-ID del conjunto histórico
# {'OPJESICA@SUPRABOND.COM.UY', 'OPVALERIA@SUPRABOND.COM.UY'}.
VENDEDORES_OP_EXCLUIDOS: frozenset[int] = frozenset({232, 260})
