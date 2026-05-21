# Fórmula de Compensación v1.2 — Especificación

**Estado**: Propuesta acordada, NO implementada todavía.
**Fecha**: 2026-05-21
**Aplica a**: Vendedores GSU activos (excluye operarios y vendedor sub-umbral con tratamiento separado).
**Motivación**: cambio normativo Uruguay — el salario mínimo se fijó en $49.855 UYU y por ley sólo el sueldo fijo cuenta para alcanzarlo (las comisiones quedan aparte). El esquema viejo ($10.000 fijo + 2,35% × venta neta + 3% × cobranza) deja de cumplir.

---

## Estructura general

```
COMPENSACIÓN MENSUAL = SUELDO_FIJO + COMISIÓN_MENSUAL
COMPENSACIÓN TRIMESTRAL = (3 × COMPENSACIÓN MENSUAL) + BONO_TRIMESTRAL
```

El bono trimestral se paga al cierre del trimestre calendario, en una única liquidación junto con la comisión del último mes del trimestre.

---

## 1. Sueldo fijo

`SUELDO_FIJO = 49.855 UYU` (salario mínimo nacional vigente).

Si el mínimo legal sube en el futuro, este valor debe actualizarse — debería leerse de un parámetro de configuración, no estar hardcoded en código.

---

## 2. Comisión mensual

Dos pilares independientes, cada uno con tres tramos sobre el excedente del umbral:

### Comisión por venta (sobre venta neta del mes, sin IVA)

| Tramo | Tasa | Aplicada sobre |
|---|---:|---|
| Hasta $600.000 | 0% | (cubierto por sueldo fijo) |
| Entre $600.000 y $1.500.000 | **2,35%** | el excedente sobre $600.000 |
| Por encima de $1.500.000 | **5%** | el excedente sobre $1.500.000 (tier acelerado) |

Fórmula:

```
si vn ≤ 600.000:         com_venta = 0
si 600.000 < vn ≤ 1.500.000: com_venta = (vn − 600.000) × 0,0235
si vn > 1.500.000:       com_venta = (1.500.000 − 600.000) × 0,0235
                                   + (vn − 1.500.000) × 0,05
```

### Comisión por cobranza (sobre el total cobrado del mes)

| Tramo | Tasa | Aplicada sobre |
|---|---:|---|
| Hasta $700.000 | 0% | (cubierto por sueldo fijo) |
| Entre $700.000 y $1.500.000 | **3%** | el excedente sobre $700.000 |
| Por encima de $1.500.000 | **4%** | el excedente sobre $1.500.000 (tier acelerado) |

Fórmula:

```
si c ≤ 700.000:          com_cobranza = 0
si 700.000 < c ≤ 1.500.000: com_cobranza = (c − 700.000) × 0,03
si c > 1.500.000:        com_cobranza = (1.500.000 − 700.000) × 0,03
                                       + (c − 1.500.000) × 0,04
```

### Notas operativas

- **Venta neta** = monto facturado / 1,22 (descuenta IVA 22%).
- Las dos comisiones se suman y se redondean al peso hacia arriba (`math.ceil`).
- Toda operación se asume en **UYU** (no procesar otras monedas en este cálculo).
- Las exclusiones de vendedores (OPJESICA, OPVALERIA, etc.) se mantienen iguales al esquema anterior.

---

## 3. Bono trimestral

Trimestre calendario:

| Trimestre | Meses | Pago del bono |
|---|---|---|
| Q1 | Enero – Marzo | Junto con la liquidación de marzo |
| Q2 | Abril – Junio | Junto con la liquidación de junio |
| Q3 | Julio – Septiembre | Junto con la liquidación de septiembre |
| Q4 | Octubre – Diciembre | Junto con la liquidación de diciembre |

### Evaluación independiente por pilar

Cada pilar (venta y cobranza) se evalúa **por separado**, con su propia categoría y porcentaje. El bono final del trimestre es la suma de ambos.

### Categorías y porcentajes

| Categoría | Criterio | Bono sobre comisión venta del trimestre | Bono sobre comisión cobranza del trimestre |
|---|---|---:|---:|
| **Cat A** | Los **3 meses** sobre el umbral pleno ($600k venta / $700k cobranza) | **10%** | **15%** |
| **Cat B** | Promedio ≥ 50% del umbral ($300k / $350k) **Y** al menos 1 mes pleno | **5%** | **8%** |
| **Sin categoría** | No cumple A ni B | 0% | 0% |

### Algoritmo de clasificación (pseudocódigo)

```
clasificar_pilar(volumenes_3_meses, umbral_pleno):
    umbral_half = umbral_pleno × 0,5
    meses_plenos = cantidad de meses con volumen ≥ umbral_pleno
    promedio = suma(volumenes) / 3

    si meses_plenos == 3:
        return "A"
    si promedio ≥ umbral_half  Y  meses_plenos ≥ 1:
        return "B"
    return "—"
```

### Cálculo del bono

```
com_venta_trim    = suma de comisión por venta de los 3 meses
com_cobranza_trim = suma de comisión por cobranza de los 3 meses

cat_venta    = clasificar_pilar(ventas_netas_mensuales, 600.000)
cat_cobranza = clasificar_pilar(cobranzas_mensuales, 700.000)

bono_venta    = com_venta_trim    × pct[cat_venta]    (10% / 5% / 0%)
bono_cobranza = com_cobranza_trim × pct[cat_cobranza] (15% / 8% / 0%)

bono_trimestral = ceil(bono_venta) + ceil(bono_cobranza)
```

---

## 4. Reglas especiales

### Licencia por vacaciones (Ley UY)

Si el vendedor tomó licencia por vacaciones durante un mes del trimestre:

- Ese mes se computa con el **promedio de venta neta y cobranza de los otros 2 meses** del mismo trimestre.
- El promedio sintético aplica **tanto para la clasificación Cat A/B como para el cálculo del monto del bono**.
- Justificación: por normativa laboral uruguaya, las vacaciones se pagan como si hubiera trabajado.

### Retros (ajustes retroactivos)

Los ajustes retroactivos (ventas o cobranzas que aparecen tarde y se incorporan al mes siguiente al de origen):

- Se calculan con la fórmula **vigente al momento del cálculo del retro**, no con la fórmula del período original.
- Esto evita rastrear regímenes históricos al hacer ajustes.

### Vendedor con compensación viejo por debajo del nuevo fijo

Si un vendedor venía cobrando menos de $49.855/mes en el esquema viejo:

- Por ley se le paga al menos el nuevo fijo de $49.855.
- La fórmula completa (con sus tramos y bono) se le aplica igual; típicamente caerá en "Sin categoría" en el bono hasta que su actividad crezca.
- Es un costo regulatorio inevitable.

### Vendedores excluidos del cálculo

Las cuentas operativas (OPJESICA, OPVALERIA, etc.) **NO** entran al esquema de comisiones — son funciones administrativas, no comerciales. Misma regla que el esquema viejo.

---

## 5. Constantes para implementar en `commissions.py`

```python
# Compensación base
SUELDO_FIJO_UYU = 49_855          # antes era 10_000

# Tramos de comisión venta
UMBRAL_VENTA_PLENO = 600_000      # arranca a comisionar
UMBRAL_VENTA_TIER_ALTO = 1_500_000  # arranca tier acelerado
TASA_VENTA_TRAMO_MEDIO = 0.0235
TASA_VENTA_TRAMO_ALTO = 0.05

# Tramos de comisión cobranza
UMBRAL_COBRANZA_PLENO = 700_000
UMBRAL_COBRANZA_TIER_ALTO = 1_500_000
TASA_COBRANZA_TRAMO_MEDIO = 0.03
TASA_COBRANZA_TRAMO_ALTO = 0.04

# Bono trimestral
PCT_BONO_CAT_A_VENTA = 0.10
PCT_BONO_CAT_A_COBRANZA = 0.15
PCT_BONO_CAT_B_VENTA = 0.05
PCT_BONO_CAT_B_COBRANZA = 0.08

# IVA (igual que el esquema anterior)
DIVISOR_IVA = 1.22
```

---

## 6. Comparativa ilustrativa con esquema antiguo

Para un perfil promedio mensual (venta neta ~$1.000.000 y cobranza ~$1.000.000):

| Concepto | Esquema antiguo | v1.2 |
|---|---:|---:|
| Sueldo fijo | $10.000 | **$49.855** |
| Comisión venta (2,35%) | $23.500 | $9.400 (sobre excedente sobre $600k) |
| Comisión cobranza (3%) | $30.000 | $9.000 (sobre excedente sobre $700k) |
| Bono trimestral prorrateado /3 | — | ~$1.870 |
| **Compensación mensual** | **$63.500** | **$70.125** |
| Diferencia para el vendedor | — | **+$6.625 (+10,4%)** |

Para perfiles distintos la mejora varía entre **+10% y +13%** mensual. El costo extra mensual para la empresa por vendedor es **aproximadamente $7.500 a $8.000**.

---

## 7. Impacto operativo

### Lo que cambia respecto al cálculo actual

1. **El sueldo fijo no se calcula** dentro de `commissions.py` — vive en la liquidación general. Si esta planilla incluye el fijo, hay que actualizarlo a $49.855.
2. **Las fórmulas de comisión cambian**: dejan de ser lineales (`tasa × monto_total`) y pasan a tramos sobre excedente. Hay que reemplazar las funciones que computan comisión por venta y cobranza.
3. **Aparece un componente trimestral nuevo (bono)** que no existía antes. Requiere:
   - Mantener historial trimestral por vendedor (ya está en el Sheet histórico).
   - Calcular el bono al cierre del trimestre.
   - Manejar la regla de licencia (sustituir mes de licencia por promedio de los otros 2).

### Lo que NO cambia

- Vendedores excluidos (`VENDEDORES_EXCLUIDOS_OP`).
- Estados de comprobante excluidos (`ESTADOS_EXCLUIDOS` con excepción RefExterna).
- Tratamiento de cobranzas con cliente inexistente.
- Cliente existente sin Vendedor Asignado → descartar.
- Solo UYU.
- Redondeo `math.ceil` al peso.

### Regla firme: smoke test ANTES de tocar `commissions.py`

Es **plata real** a personas. Cualquier cambio en `commissions.py` o `comisiones_data.py` debe:

1. Correr el smoke test invariante (`_exploracion-api-contabilium/smoke_comisiones_refexterna.py`) **antes** del cambio.
2. Agregar nuevos casos al smoke test que cubran:
   - Cálculo con la nueva fórmula sobre datos conocidos.
   - Caso de mes con licencia (sustitución por promedio).
   - Caso de Cat A, Cat B, Sin categoría para cada pilar.
   - Caso de retros bajo la nueva fórmula.
3. Implementar parámetros opcionales con default que preserven el comportamiento viejo, para poder hacer un run dual y comparar antes de cambiar el default.

---

## 8. Variantes consideradas y descartadas

Como parte del proceso de diseño se evaluaron otras opciones. Las dos principales:

### v1.1 — v1 + microcomisión bajo umbral (descartada en favor de v1.2)

Agregaba 0,3% sobre venta neta hasta $600k y 0,5% sobre cobranza hasta $700k. Producía un "plus" parejo de $5.300/mes/vendedor cuando ambos umbrales se cruzaban. Más cara que v1.2 (~$15.900/mes empresa vs ~$9.300/mes), repartía plano (no proporcional al desempeño), y no incentivaba la consistencia trimestral.

### v1.2 con umbrales aumentados $100k (700/800)

Ajuste evaluado: mover el umbral pleno de venta a $700k y de cobranza a $800k. Producía ahorro de ~$17k/mes para la empresa pero recortaba la compensación de los vendedores en $4-6k/mes cada uno respecto de v1.2 con umbrales 600/700. **Mantenida la versión 600/700** porque alinea mejor con el principio "al mismo volumen mismo total" respecto del esquema viejo.

---

## 9. Decisiones pendientes antes de implementar

- [ ] Confirmación final de Mariano luego de la conversación con los vendedores.
- [ ] Cómo se reporta el bono trimestral en el panel del dashboard (¿una pestaña nueva? ¿agregado a la pestaña de Comisiones?).
- [ ] Política para vendedores que arrancan a mitad de trimestre (¿bono pro-rata o esperan al trimestre completo siguiente?).
- [ ] Política para cambios de empleado a mitad de trimestre (alta/baja).

---

## Referencias

- `commissions.py` — implementa la fórmula vigente (esquema antiguo).
- `comisiones_data.py` — carga de datos para el cálculo.
- `_exploracion-api-contabilium/smoke_comisiones_refexterna.py` — smoke test invariante a correr antes/después del cambio.
- `_learning/decisions.md` — historial de decisiones del proyecto.
- `_learning/errors.md` — gotchas conocidos.
