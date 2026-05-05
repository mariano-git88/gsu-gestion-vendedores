# Gestión de Vendedores GSU

Dashboard semanal en Streamlit para la reunión comercial del Jefe de
Ventas de GSU. Cruza facturación semanal/mensual contra cartera asignada
y muestra métricas de venta y cobertura por vendedor.

> El manual operativo completo del proyecto vive en
> [`claude.md.txt`](./claude.md.txt). Leer ahí las reglas de carga,
> filtrado, joins y clasificación. Este README es solo para arrancar el
> entorno local.

## Cómo correr localmente

Desde la terminal, parado en la carpeta del proyecto:

```bash
# 1. Crear y activar virtualenv (la primera vez)
python3 -m venv .venv
source .venv/bin/activate

# 2. Instalar dependencias (la primera vez y cuando cambie requirements.txt)
pip install -r requirements.txt

# 3. Configurar secrets locales (la primera vez)
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# editar .streamlit/secrets.toml y poner la password real

# 4. Correr el app
streamlit run app.py
```

Se abre el navegador en `http://localhost:8501`. Ingresar con la password
del `secrets.toml` y subir las 5 planillas para empezar.

## Las 5 planillas que pide el app

1. `fc_semanal.xlsx` — Facturación de la última semana (hoja `Comprobantes`)
2. `fc_mensual.xlsx` — Facturación del mes en curso (hoja `Comprobantes`)
3. `clientes.xlsx` — Cartera asignada (hoja `Clientes`)
4. `productos.xlsx` — Maestro de productos (hoja `Productos`)
5. `combos.xlsx` — Maestro de combos (hoja `Combos`)

Las columnas relevantes y reglas de validación están en el `claude.md.txt`.

## Estructura del proyecto

```
.
├── claude.md.txt          # Manual operativo — leer siempre al arrancar
├── README.md              # Este archivo
├── requirements.txt       # Dependencias Python
├── .gitignore             # Bloquea datos sensibles, secrets, venvs
├── .streamlit/
│   ├── secrets.toml.example   # Plantilla pública (en git)
│   └── secrets.toml           # Local con password real (gitignored)
├── app.py                 # Entry point Streamlit
├── auth.py                # Login con password único
├── data_loader.py         # Carga y validación de las 5 planillas
├── transforms.py          # Filtrado, joins, clasificación SKU
├── metrics.py             # Cálculo de ventas y cobertura
├── views/                 # Componentes del dashboard
│   ├── resumen.py
│   ├── sub_rubro.py
│   └── cobertura.py
├── tests/                 # Tests unitarios
│   ├── fixtures/
│   └── test_transforms.py
├── _learning/             # Memoria del proyecto (en git)
│   ├── decisions.md       # Decisiones de diseño, append-only
│   └── errors.md          # Errores pasados a no repetir
└── _session-log.md        # Bitácora de sesiones (NO en git)
```

> Nota: en este momento la mayoría de los archivos `.py` todavía no
> existen — se crean en las siguientes tandas (data_loader, transforms,
> metrics, auth, views, app). Ver la propuesta de orden de trabajo en el
> `_session-log.md`.

## Reglas críticas (resumen)

- **`Documento` siempre como string** (RUT del cliente, llave canónica de joins).
- **Joins solo por documento, nunca por razón social.**
- **NCF**: conservar solo si tienen SKU (devoluciones reales). Descartar
  las sin SKU (descuentos comerciales).
- **Moneda**: solo se procesan filas en UYU.
- **Clasificación SKU**: productos primero, combos después, sin asignar como fallback.
- **Sin persistencia entre sesiones**: las planillas se procesan en
  memoria y se descartan al cerrar el navegador.

Ver el `claude.md.txt` para el detalle completo.

## Deploy

GitHub público + Streamlit Community Cloud. Las passwords y credenciales
de producción se configuran en Streamlit Cloud → Settings → Secrets,
no en el repo.

## Apps adicionales del mismo repo

El repo expone tres apps Streamlit independientes que comparten codebase
(`api_loader.py`, `theme.py`, etc.) pero se deployan por separado con
URLs y passwords distintas:

| Entry point | Rol | Password en secrets |
|---|---|---|
| `app.py` | Dashboard semanal del Jefe de Ventas (read-only) | `app_password` |
| `comisiones_app.py` | Liquidación mensual de Comisiones (read API + write Sheet) | `comisiones_password` |
| `facturador_app.py` | **Facturación masiva desde órdenes de venta** (write Contabilium) | `facturador_password` |

### Facturador — uso local

```bash
# Mismo entorno que el dashboard, agregar el password en secrets.toml
# (ver bloque comentado en .streamlit/secrets.toml.example)
streamlit run facturador_app.py
```

Sidebar: rango de fechas, condición de venta, punto de venta, depósito.
Botón "Buscar pendientes" → tabla con 3 buckets (facturables, ya
facturadas vía API, no facturables por línea libre). Selección con
checkbox + gate `FACTURAR` + run secuencial respetando throttling UY
(15 req/10s). El log de cada emisión se persiste en Google Sheet
(reutiliza el mismo `[gsheets]` block que Comisiones — tab nueva
`log_facturacion`). Ver `facturador.py` para los detalles del workflow
oficial validado contra la API REST UY.
