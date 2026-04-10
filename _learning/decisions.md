# Decisiones — Gestión de Vendedores GSU

Append-only. Cada entrada documenta un criterio acordado, su contexto y
las alternativas descartadas.

---

## 2026-04-10 — Excluir vendedores OPJESICA y OPVALERIA del dashboard

**Decisión:** las filas de facturación cuyo `vendedor` es uno de los
siguientes NO se contabilizan en NINGUNA métrica del dashboard:

- `OPJESICA@SUPRABOND.COM.UY`
- `OPVALERIA@SUPRABOND.COM.UY`

**Contexto:** son cuentas operativas/administrativas de Suprabond, no
representantes comerciales. Sus operaciones aparecen en la facturación
de Contabilium pero no deben contar para venta, cobertura ni ningún
cálculo de performance del equipo comercial. Es la misma regla que se
aplica en el proyecto "Liquidación de Comisiones GSU" — ahí están
documentadas las dos cuentas con la misma justificación.

**Implementación:**

- Constante `VENDEDORES_OP_EXCLUIDOS` definida en `transforms.py`.
- Función `exclude_op_vendedores(df_fc)` que separa las filas
  conservadas de las excluidas.
- Es el **PRIMER paso** del orquestador `prepare_facturacion()`, antes
  incluso del filtrado de NCF, así el resto del pipeline no procesa
  filas que igual van a descartarse.
- El check `check_vendedores_sin_cartera()` opera sobre el DataFrame
  ya post-exclusión (snapshot `df_post_op` dentro del orquestador), de
  modo que estos dos vendedores **no aparecen flagueados como
  huérfanos** en el panel de salud.
- Se reportan las filas excluidas en el panel de salud como
  **info** (no warning, no error) para trazabilidad: el usuario ve
  cuántas filas se removieron y de qué cuentas.

**Match:** se hace por **igualdad exacta** de string (case-sensitive,
con dominio incluido). Si un día el ERP exporta los emails en otra
capitalización o con un dominio distinto, el filtro no va a matchear y
hay que actualizar la lista en `transforms.py`.

**Alternativas descartadas:**

- Match case-insensitive con `.upper()`: descartado por consistencia con
  el proyecto de Liquidación de Comisiones, donde tampoco se hace.
- Excluirlos solo en `metrics.py` y dejar que pasen por el resto del
  pipeline: descartado porque (a) ensucia el panel de salud con
  warnings irrelevantes (vendedores sin cartera, etc.), (b) consume
  procesamiento sin fin, y (c) si en el futuro se agrega una métrica
  nueva, hay que acordarse de excluirlos ahí también.
- Filtrarlos en `data_loader.py`: descartado porque el data loader
  tiene que ser agnóstico a las reglas de negocio. Su responsabilidad
  es leer y validar estructura, no decidir qué filas contar.

**Confirmado por:** Mariano, sesión 2026-04-10.
