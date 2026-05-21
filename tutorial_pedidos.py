"""
tutorial_pedidos.py — Contenido del tutorial del módulo de Carga de
Pedidos.

Se pinta dentro de un st.dialog (modal) cuando el usuario hace click en
el botón "Tutorial" del sidebar de `pedidos_app.py`. Está pensado para
las operadoras de Suprabond: explica qué hace cada parte, cómo se usa
paso a paso, y qué hacer si algo falla. Sin tecnicismos innecesarios.

Si hay que actualizar el contenido, editar acá sin tocar `pedidos_app.py`.
"""

import streamlit as st


def render() -> None:
    """Renderiza el tutorial completo dentro del modal."""

    st.markdown(
        """
### ¿Qué hace esta app?

Reemplaza el trabajo manual de cargar pedidos en Contabilium por un
flujo guiado. Subís el Excel `NOTA DE PEDIDO G.S.U.` tal cual lo manda
el vendedor por mail — **no hace falta desproteger ni descombinar
nada** — y la app:

1. Lee los pedidos del Excel y deja solo lo que pidió el cliente
   (descarta el catálogo de relleno).
2. Controla que el total cuadre con lo declarado.
3. Identifica el cliente en Contabilium.
4. Muestra si el cliente tiene **deuda vencida** y necesita
   autorización.
5. Crea las **órdenes de venta** en Contabilium con los controles que
   correspondan, y guarda un registro de auditoría.
"""
    )

    st.markdown(
        """
### Cómo entrar

Con la contraseña que te pasó Mariano. Es propia de esta app — no es
la del Facturador ni la del Dashboard.
"""
    )

    st.markdown(
        """
### Paso 1 — Subir el Excel

Arrastrá o seleccioná el `.xlsx` del mail del vendedor. La app abre el
archivo aunque esté **protegido** y con celdas **combinadas** — no
toques nada, subilo tal como lo recibís.

Si el archivo no es la plantilla `NOTA DE PEDIDO G.S.U.`, te avisa con
un error claro.
"""
    )

    st.markdown(
        """
### Paso 2 — Lector y control de totales

Apenas subís el archivo aparecen los pedidos:

- **Pedidos**: cantidad de hojas con pedido del Excel.
- **Control OK** 🟢: los pedidos donde la suma cuadra con el
  "TOTAL CON IVA".
- **A revisar** 🔴: los que NO cuadran. Si hay alguno, **no avances** —
  el Excel está pifiado y conviene volver al vendedor.

Cada pedido se expande con sus ítems. Abajo del todo hay un botón
**⬇️ Descargar pedidos en CSV** para auditar.
"""
    )

    st.markdown(
        """
### Paso 3 — Identificación del cliente

La app cruza el **Nro. Cliente** del Excel contra Contabilium. Acepta
dos fuentes:

- el número del campo "Nro. Cliente" (ej. `4060`), o
- un número escrito al principio del campo CLIENTE
  (ej. `4016-barraca pirata` → reconoce `4016`).

Estados posibles:

- 🟢 **OK**: identificado y el nombre del Excel coincide con la razón
  social de Contabilium.
- 🟠 **Revisar nombre**: el código existe, pero el nombre no se parece
  — puede ser un Nro. equivocado, mirá con cuidado.
- 🔴 **No encontrado**: el código no existe en Contabilium.
- ⚪ **Sin Nro. Cliente**: el Excel no trae número.

Para los **no encontrados / sin Nro.**, en el paso 5 aparece un
**buscador** que te permite **asignar el cliente a mano** (tipeás parte
del nombre o del código y filtra). Una vez asignado, el pedido se
vuelve cargable.
"""
    )

    st.markdown(
        """
### Paso 4 — Chequear deuda vencida

Tocá **«🔎 Chequear deuda vencida»**. La app trae la facturación de los
últimos 12 meses y arma, por cliente, la deuda vencida (mismo cálculo
que la tab "Cobranzas" del Dashboard).

- La **primera consulta del día** puede tardar 1–2 minutos. Después
  queda cacheado y es instantáneo.
- Te muestra una lista con los pedidos cuyo cliente tiene deuda
  vencida — esa lista es lo que pasa a Valeria para liberar.

**Importante**: la carga de órdenes (paso 5) queda **bloqueada** hasta
que corras este chequeo.
"""
    )

    st.markdown(
        """
### Paso 5 — Carga de órdenes en Contabilium

Acá ocurre la escritura real. Cada pedido se expande con su ficha:

**1) Cliente.** Si no se identificó, asignalo con el buscador
(*"Buscar y asignar cliente"*).

**2) Aprobaciones (gates).** Aparecen sólo cuando hace falta:

- ✅ **APROBADO — deuda**: aparece si el cliente tiene deuda vencida.
  Lo tildás vos **sólo cuando Valeria ya autorizó**.
- ✅ **APROBADO — precio**: aparece si el vendedor escribió un
  comentario en "Cond. de Pago" (texto libre). Tildalo cuando leíste
  el comentario y, si corresponde, ajustaste precios o descuentos.

Sin las aprobaciones que correspondan, el pedido **NO se carga**.

**3) Desglosar / editar ítems** (expander, siempre disponible). Por
cada ítem podés editar:

- **Precio U.** — arranca con el precio del Excel; lo cambiás si hace
  falta.
- **Desc %** — descuento porcentual; va al campo Bonificación de
  Contabilium. Ejemplo: precio 100, Desc 32 → se carga **68**.

**4) Incluir en la carga**: tildá los pedidos que querés cargar **en
este run**. Por defecto está apagado — nada se carga por accidente.
Esto te permite cargar **un solo pedido de prueba** la primera vez.
"""
    )

    st.markdown(
        """
### Paso 6 — Confirmar y cargar

Abajo de todo:

1. La tabla de **Confirmación** te muestra el resumen.
2. El expander **"Ver exactamente qué se va a mandar"** muestra los
   datos que se enviarán a Contabilium por cada orden tildada. Última
   oportunidad de revisar.
3. Escribí **tu nombre o iniciales** (para el registro de auditoría).
4. Escribí **`CARGAR PEDIDOS`** (tal cual, mayúsculas) para habilitar
   el botón.
5. Apretá **🚀 CARGAR PEDIDOS EN CONTABILIUM**.

La app las carga **una por una**, respetando los límites de la API.

Al final aparece la sección **"Resultados de la última carga"**, con
una tabla que incluye **el Nº de Orden de Contabilium** de cada pedido,
el cliente, los ítems y el total. Esta tabla **queda visible** mientras
estés en la app, incluso si tocás otras cosas — no se pierde con un
rerun.

Además, abajo de la tabla aparece:

- **⬇️ Descargar resultados en CSV** — para sumar a tu planilla externa
  (entregas a la nave, fletes, archivo). Incluye el Nº de Contabilium
  y el ID interno por si necesitás buscar después.
- **📄 Descargar PDF combinado** — un PDF por orden con los datos para
  el depósito (ya lo conocés).
- Mensaje del **audit log a Google Sheet** — si está configurado,
  guarda automáticamente cada carga en el Sheet
  (tab `log_carga_pedidos`). Si no está configurado, te dice cómo
  activarlo.
"""
    )

    st.markdown(
        """
### Reglas y casos especiales

- **Pedido con 0 ítems o total $0,00** → no se carga. La app te lo
  marca con la razón.
- **Combos** (ej. `COM SLT`): se cargan como un ítem más, pero la
  **primera vez** probá uno aparte para confirmar que Contabilium lo
  procesa como esperás.
- **Precio**: el Excel ya trae el precio correcto (las planillas de
  interior ya incluyen el +5%). No hace falta tocarlo salvo que el
  vendedor lo pida.
- **Vendedor**: la orden se asigna al vendedor que tiene el cliente en
  Contabilium. No usamos el "Nro. Vend" del Excel.
- **Depósito**: siempre **VENTAS**.
- **Crear la orden reserva stock al instante** en Contabilium (es lo
  mismo que pasa cuando la cargás a mano). Por eso conviene revisar
  bien antes.
"""
    )

    st.markdown(
        """
### Auditoría

Cada vez que cargás órdenes, queda una fila por pedido en un Google
Sheet (tab `log_carga_pedidos`) con:

- fecha y hora, **tu nombre**, qué pedido,
- cliente, RUT, ID, vendedor,
- si hubo aprobación de deuda o de precio,
- descuentos aplicados, total,
- resultado (OK / ERROR) y número de orden.

Si en el reporte ves *"Audit log a Sheet deshabilitado"*, decile a
Mariano — falta configurar un secret y se arregla en dos minutos.
"""
    )

    st.markdown(
        """
### Si algo falla

- **"Contraseña incorrecta"** → revisá la clave (es distinta de la del
  Facturador).
- **"No pude leer el archivo"** → no es la plantilla `NOTA DE PEDIDO
  G.S.U.`; fijate el adjunto.
- **"Cliente no identificado"** → asignalo con el buscador, o avisale
  al vendedor que corrija el Nro. Cliente.
- **El chequeo de deuda no termina** → recargá. Si insiste, avisar.
- **Una orden devuelve error en Contabilium** → el mensaje aparece en
  el reporte y queda en el audit log. Copialo y mandalo a Mariano para
  ajustar.
"""
    )

    st.markdown(
        """
### Lo que esta app NO hace todavía

- No emite la factura — eso lo hace el **Facturador** después.
- No aplica automáticamente los descuentos del campo *"Cond. de Pago"*
  cuando están en prosa (ej. *"32% en burletes"*); lo tipeás vos en el
  desglose si corresponde.
- No registra condiciones especiales (30/60/90 días, fletes
  tercerizados, etc.) automáticamente — siguen siendo nota manual en
  la orden por ahora.

Ante cualquier duda, escribirle a Mariano.
"""
    )
