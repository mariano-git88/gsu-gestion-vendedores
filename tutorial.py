"""
tutorial.py — Contenido del tutorial del dashboard.

Se pinta dentro de un st.dialog (modal) cuando el usuario hace click
en el botón "Tutorial" del header del app.

Está pensado para que cualquier persona del equipo de Suprabond pueda
abrir el dashboard por primera vez y entender, sin que nadie le
explique nada en vivo:

  1. Qué es la app y para qué sirve.
  2. Cómo arrancar (las 5 planillas que hay que cargar).
  3. Qué muestra cada tab y cada tabla específica.
  4. Cómo exportar la agenda personal de un vendedor.
  5. Cómo cerrar sesión.

El contenido vive solo en este módulo. Si hay que actualizarlo, se
edita acá sin tocar app.py ni el resto del código.
"""

import streamlit as st


def render() -> None:
    """Renderiza el contenido completo del tutorial dentro del modal."""

    # ----- Intro -----
    st.markdown(
        """
        ### ¿Qué es esta aplicación?

        Es el **dashboard semanal del Jefe de Ventas de GSU**. Cruza las
        planillas exportadas de Contabilium y muestra cómo viene la
        performance de cada vendedor y su cartera de clientes durante
        la semana y el mes en curso.

        Está pensado para usarse en la **reunión comercial semanal**:
        el Jefe de Ventas y el equipo abren el dashboard juntos y discuten
        sobre los datos en tiempo real.
        """
    )

    st.divider()

    # ----- Cómo empezar -----
    st.markdown(
        """
        ### Cómo empezar (3 pasos)

        1. En la **barra lateral** (a la izquierda), cargá las **5 planillas
           `.xlsx`** exportadas de Contabilium.
        2. Esperá unos segundos a que el dashboard procese los datos.
           Vas a ver indicadores de carga.
        3. Navegá entre las **5 tabs** de arriba (Resumen / Sub-rubro /
           Cobertura / Análisis / Salud) para ver las distintas vistas.

        > Si todavía no cargaste las 5 planillas, el dashboard te muestra
        > un mensaje de "Cargá las 5 planillas para empezar" y no
        > muestra nada más. Es normal — es para evitar errores.
        """
    )

    st.divider()

    # ----- Las 5 planillas (resumen) -----
    st.markdown(
        """
        ### Las 5 planillas que tenés que cargar

        Las 5 son **obligatorias** y vienen de Contabilium. Tienen que
        tener exactamente estos nombres y estas hojas adentro:

        | # | Archivo | Hoja interna | Qué contiene |
        |---|---|---|---|
        | 1 | `fc_semanal.xlsx` | `Comprobantes` | Facturación de la última semana |
        | 2 | `fc_mensual.xlsx` | `Comprobantes` | Facturación del mes en curso |
        | 3 | `clientes.xlsx` | `Clientes` | Cartera asignada a cada vendedor |
        | 4 | `productos.xlsx` | `Productos` | Maestro de productos (SKU + sub-rubro) |
        | 5 | `combos.xlsx` | `Combos` | Maestro de combos (SKU compuesto) |

        Si alguna planilla tiene un nombre distinto o una hoja con otro
        nombre, el dashboard te va a avisar con un error claro indicando
        qué falta. **No procesa nada si falta o está mal una columna** —
        es a propósito, para que las cifras nunca sean dudosas.
        """
    )

    st.divider()

    # ----- Cómo obtener cada planilla -----
    st.markdown(
        """
        ### Cómo obtener cada planilla desde Contabilium

        > **IMPORTANTE — regla común a las 5 planillas**: cuando
        > descargues cada archivo desde Contabilium, **tenés que
        > renombrarlo manualmente** al nombre exacto que se indica abajo
        > (`fc_semanal.xlsx`, `fc_mensual.xlsx`, `clientes.xlsx`,
        > `productos.xlsx`, `combos.xlsx`). Si el archivo no se llama
        > exactamente así, el dashboard lo va a rechazar con un error.

        #### 1) `fc_semanal.xlsx` — facturación semanal

        1. Entrá a la sección **Facturación** en Contabilium.
        2. Filtrá por las fechas de la última semana (desde el lunes
           anterior hasta el domingo).
        3. Seleccioná la opción **"Detallada"**.
        4. Hacé click en **Descargar**.
        5. **Renombrá el archivo descargado a `fc_semanal.xlsx`**.

        > **Nota sobre la cantidad de días**: aunque se llame "semanal",
        > en realidad podés filtrar por la cantidad de días que quieras
        > (3 días, 10 días, lo que sea). El dashboard va a mostrar
        > exactamente los datos correspondientes a los días que filtraste
        > en Contabilium. La columna "Total semana" en el Resumen es
        > literalmente "el rango que vos cargaste como semanal".

        #### 2) `fc_mensual.xlsx` — facturación mensual

        1. Entrá a la sección **Facturación** en Contabilium.
        2. Filtrá por las fechas del **mes en curso** (desde el día 1
           hasta hoy).
        3. Seleccioná la opción **"Detallada"**.
        4. Hacé click en **Descargar**.
        5. **Renombrá el archivo descargado a `fc_mensual.xlsx`**.

        > **Decisión importante — mes en curso vs últimos 30 días**:
        > tenés que elegir un criterio y mantenerlo:
        >
        > - **Mes en curso**: filtrás desde el día 1 del mes hasta hoy.
        >   Es la opción más natural para una reunión semanal alineada
        >   con el calendario contable.
        > - **Últimos 30 días**: filtrás una ventana móvil de 30 días
        >   hacia atrás, sin importar en qué mes caigan.
        >
        > **Atención al caveat de "Semana mayor que Mes"**: si elegís
        > "mes en curso" y la semana analizada en `fc_semanal.xlsx`
        > cruza dos meses (por ejemplo, los últimos días del mes anterior
        > más los primeros del mes en curso), puede pasar que las cifras
        > de **"Total semana" sean mayores a las de "Total mes"** —
        > simplemente porque el mes recién está empezando y el rango
        > semanal incluye días del mes anterior que no entraron al
        > rango mensual. **Eso no es un bug del dashboard**; es la
        > consecuencia de cómo definiste los rangos en Contabilium.

        #### 3) `clientes.xlsx` — cartera de clientes

        1. Entrá a la sección **Clientes** en Contabilium.
        2. Hacé click en el botón **Exportar**.
        3. Descargá la planilla.
        4. **Renombrá el archivo descargado a `clientes.xlsx`**.

        #### 4) `productos.xlsx` — maestro de productos

        1. Entrá a la sección **Productos y servicios** en Contabilium.
        2. Al lado del botón **Exportar**, hacé click en **"Simple"**.
        3. Hacé click en el botón **Descargar**.
        4. **Renombrá el archivo descargado a `productos.xlsx`**.

        #### 5) `combos.xlsx` — maestro de combos

        1. Entrá a la sección **Productos y servicios** en Contabilium.
        2. Al lado del botón **Exportar**, hacé click en **"Detalle combos"**.
        3. Hacé click en el botón **Descargar**.
        4. **Renombrá el archivo descargado a `combos.xlsx`**.
        """
    )

    st.divider()

    # ----- La barra lateral -----
    st.markdown(
        """
        ### La barra lateral (sidebar)

        Es la columna gris claro a la izquierda. Tiene 3 bloques:

        1. **Planillas del período** — los 5 file uploaders donde
           arrastrás cada planilla.
        2. **Exportar agenda** — aparece automáticamente cuando ya cargaste
           las 5 planillas. Te permite descargar la agenda personal de
           cualquier vendedor (más detalle abajo).
        3. **Cerrar sesión** — todo abajo del todo. Te saca del dashboard.
           La próxima vez que entres, tenés que volver a poner la contraseña.
        """
    )

    st.divider()

    # ----- Las 5 tabs -----
    st.markdown(
        """
        ### Las 5 tabs (de izquierda a derecha)

        Cada tab es una vista distinta de los datos. Las primeras 3 son
        del uso diario en la reunión. La 4ta es exploración estratégica.
        La 5ta es diagnóstico de los datos.
        """
    )

    # Tab 1
    st.markdown(
        """
        #### Tab 1 — **Resumen**

        La foto rápida del período. Es la primera tab que ves cuando
        cargás los datos.

        Contiene:

        - **Total semana (UYU)** y **Total mes (UYU)** — los dos números
          grandes arriba. Es el monto total facturado por todo el
          equipo en cada período.
          - **Importante**: estos totales **no descuentan los descuentos
            comerciales** (NCF sin SKU). Si hay descuentos, los vas a ver
            mencionados como una línea aparte debajo del total. Eso es
            para que puedas cuadrar contra Excel manualmente.

        - **Tabla "Ventas por vendedor"** — para cada vendedor, su monto
          y unidades de la semana y del mes lado a lado. Ordenada por
          monto del mes descendente.

        - **Tabla "Cobertura general por vendedor (mes)"** — para cada
          vendedor, cuántos clientes asignados tiene en cartera y a
          cuántos les vendió al menos algo este mes. La columna
          `cobertura_pct` te dice qué porcentaje de su cartera está
          activa este mes.
        """
    )

    # Tab 2
    st.markdown(
        """
        #### Tab 2 — **Sub-rubro**

        Desglose de ventas por categoría de producto.

        Tiene:

        - Un **selector de período** arriba (Semana o Mes).
        - **Dos filtros opcionales**: filtrar por sub-rubro específico
          o por SKU específico.
        - **Tabla "Ventas por (vendedor, sub-rubro)"** — el monto y las
          unidades vendidas por cada combinación de vendedor y sub-rubro.
          Es el desglose más fino del mix de productos.

        Útil para responder preguntas tipo: "¿qué tan bien le está
        yendo a Mario con el sub-rubro Pinturas este mes?".
        """
    )

    # Tab 3
    st.markdown(
        """
        #### Tab 3 — **Cobertura**

        Las métricas de "qué tan bien estás atendiendo a tu cartera".
        Tiene **4 bloques** uno debajo del otro:

        1. **Cobertura general por vendedor** — cuántos clientes
           asignados recibieron al menos una venta tipo factura en el
           período. El porcentaje te dice cuántos de tu cartera "están
           activos".

        2. **Cobertura por sub-rubro** — lo mismo desagregado por
           categoría de producto. Te dice por ejemplo "Mario cubre el
           80% de su cartera con Ferretería pero solo el 20% con
           Pinturas". Eso indica oportunidades de cross-sell.

        3. **Cobertura por SKU específico** — un selector donde elegís
           un producto específico, y ves para cada vendedor cuántos
           de sus clientes lo compraron este período.

        4. **Clientes que NO compraron este SKU en el mes** — la lista
           detallada de los huecos para el SKU seleccionado arriba.
           Te muestra la razón social y el vendedor de cada uno, para
           que puedas asignar visitas.
           - **Siempre sobre el mes** (no afectada por el selector
             Semana/Mes).
           - **Match estricto**: si el cliente le compró el SKU a otro
             vendedor distinto al asignado, igual aparece — porque la
             oportunidad de venta para el vendedor asignado sigue
             abierta.
        """
    )

    # Tab 4
    st.markdown(
        """
        #### Tab 4 — **Análisis** (la parte estratégica)

        Tres bloques de exploración profunda para identificar
        oportunidades concretas de venta.

        1. **Penetración por sub-rubro** — una **matriz coloreada**
           con vendedores en filas y sub-rubros en columnas. El color
           de cada celda va de **rojo (mal)** a **amarillo** a
           **verde (bien)** según el % de cartera que ese vendedor
           cubre con ese sub-rubro. Las celdas rojas son los huecos
           más grandes — los que conviene atacar primero.

        2. **Heatmap cliente × sub-rubro** — una **matriz monocromática**
           (escala de grises) para un vendedor específico. Tenés que
           elegir el vendedor y los **top N clientes** (default 30).
           Te muestra cuánto le compró cada cliente en cada sub-rubro.
           - Las **celdas en blanco** son los huecos a explorar para
             cada cliente: "este cliente te compra Ferretería pero
             nunca Pinturas — andá a ofrecerle Pinturas".

        3. **Pareto de clientes** — la **regla 80/20**. Te dice cuáles
           son los pocos clientes que generan el **80% de la venta**
           (el "core 80%") y cuáles son el resto. Las filas del core
           se resaltan con un fondo crema. Estos son los clientes que
           tenés que **blindar antes de salir a buscar nuevos**.
           - Tiene 3 métricas arriba: total clientes, cuántos están
             en el core 80%, y cuánto monto representa el core 80%
             del total.
        """
    )

    # Tab 5
    st.markdown(
        """
        #### Tab 5 — **Salud**

        Diagnóstico de los datos cargados. Es la tab a la que
        recurrís cuando algo te llama la atención y querés entender
        si los datos están limpios.

        Tiene un panel para la semana y otro para el mes, cada uno
        con un **semáforo**:

        - **Verde** — todo OK, sin alertas.
        - **Amarillo** — warnings menores (NCF descuentos descartados,
          algunas filas no UYU, SKUs sin clasificar, clientes sin
          documento). Los datos siguen siendo confiables, solo te
          enteramos para que sepas.
        - **Rojo** — errores estructurales serios. Aparece arriba de
          todo el dashboard un **banner discreto** diciendo "Hay
          alertas estructurales en los datos. Revisar la pestaña Salud".
          Casos rojos típicos:
          - **Vendedores con ventas pero sin cartera asignada** en
            `clientes.xlsx`. Esto suele indicar que el maestro de
            clientes está desactualizado.
          - **Documentos duplicados** en `clientes.xlsx`.

        Cada panel también te muestra cuántas filas tenía cada planilla
        originalmente y cuántas quedaron después de aplicar los
        filtros. Es trazabilidad pura.
        """
    )

    st.divider()

    # ----- Exportar agenda -----
    st.markdown(
        """
        ### Exportar agenda personal por vendedor

        En la **barra lateral**, debajo de los 5 file uploaders, vas a
        encontrar (una vez cargadas las planillas) un bloque
        **"Exportar agenda"** con:

        - Un **selector de vendedor**.
        - Un botón **"Descargar agenda.xlsx"**.

        Al hacer click, descargás un archivo Excel con **5 hojas** para
        que ese vendedor se lleve "su agenda" después de la reunión:

        | Hoja | Contenido |
        |---|---|
        | **Resumen** | Performance del período + cobertura + comparativa vs el promedio del equipo |
        | **Mi cartera** | Listado completo de sus clientes con monto del mes y la semana, unidades, y si compró este mes |
        | **Clientes dormidos** | Solo los clientes que no compraron este mes (los que tiene que ir a visitar) |
        | **Penetración** | El % de su cartera por sub-rubro (sus huecos de cross-sell) |
        | **Top 80%** | Los pocos clientes que generan el 80% de su venta — los que tiene que blindar |

        Idea: el Jefe de Ventas selecciona cada vendedor, descarga su
        agenda, y se la pasa al vendedor para que la lleve consigo durante
        la semana.
        """
    )

    st.divider()

    # ----- Cerrar sesión -----
    st.markdown(
        """
        ### Cerrar sesión

        En la **barra lateral**, todo abajo del todo, hay un botón
        **"Cerrar sesión"**. Al hacerle click, te saca del dashboard
        y la próxima vez que entres tenés que volver a poner la
        contraseña.

        Conviene cerrar sesión si vas a dejar la computadora desatendida.
        """
    )

    st.divider()

    # ----- Cierre -----
    st.markdown(
        """
        ### ¿Dudas o algo no funciona?

        Si encontrás un error, una cifra que no cuadra, o algo que no
        entendés, **avisale a Mariano**. Anotá:

        - Qué tab estabas mirando
        - Qué hiciste antes de que apareciera el error
        - El mensaje exacto que te apareció (si hay)

        Cualquier reporte detallado ayuda a corregirlo rápido.
        """
    )
