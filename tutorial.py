"""
tutorial.py — Contenido del tutorial del dashboard.

Se pinta dentro de un st.dialog (modal) cuando el usuario hace click
en el botón "Tutorial" del header del app.

Está pensado para que cualquier persona del equipo de Suprabond pueda
abrir el dashboard por primera vez y entender, sin que nadie le
explique nada en vivo:

  1. Qué es la app y para qué sirve.
  2. Cómo sincronizar datos (modo primario: API de Contabilium).
  3. Cuándo usar el Modo Manual Secundario (fallback con xlsx).
  4. Qué muestra cada tab y cada tabla específica.
  5. Cómo exportar la agenda personal de un vendedor.
  6. Cómo cerrar sesión.

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

        Es el **dashboard semanal del Jefe de Ventas de GSU**. Lee los
        datos de facturación, clientes y productos directamente desde
        **Contabilium** (vía su API oficial) y muestra cómo viene la
        performance de cada vendedor y su cartera de clientes durante
        la semana, el mes y el trimestre en curso.

        Está pensado para usarse en la **reunión comercial semanal**:
        el Jefe de Ventas y el equipo abren el dashboard juntos y
        discuten sobre los datos en tiempo real.
        """
    )

    st.divider()

    # ----- Cómo empezar (modo API) -----
    st.markdown(
        """
        ### Cómo empezar — Modo primario (API de Contabilium)

        1. En la **barra lateral** (columna gris a la izquierda) vas a
           ver la sección **"Sincronizar desde Contabilium"**.
        2. Elegí el **mes** que querés analizar (por default está el
           mes actual).
        3. Elegí el rango de fechas de la **semana** a revisar (por
           default, lunes de esta semana hasta hoy).
        4. Elegí el **trimestre** — es una **ventana móvil de 3 meses
           consecutivos** definida por un "mes final". Por default el
           mes final es el mes actual (o sea, ventana = últimos 3
           meses terminando hoy). Si elegís otro mes final, la ventana
           se mueve: mes final abril 2026 → feb+mar+abr 2026.
        5. Tocá el botón **Sincronizar**. Tarda ~1-3 minutos la primera
           vez: trae los maestros de clientes y productos + toda la
           facturación del mes, la semana y el trimestre.
        6. Cuando termine, aparece un mensaje "Sincronizado" y el
           dashboard se llena con los datos. A partir de ahí podés
           navegar entre las 5 tabs.

        > **Mes en curso recortado al día**: si el mes (o el último
        > mes del trimestre) es el mes actual, el rango termina
        > **hoy**, no el último día del mes. Así no se comparan días
        > facturados vs días que todavía no transcurrieron.

        > **Si el sync tarda más de 10 minutos** o si Contabilium no
        > responde, el dashboard te avisa con el mensaje
        > *"Lamentablemente Contabilium está caído…"* y podés caer al
        > Modo Manual Secundario.

        > **Resync forzado**: si acabás de emitir una factura y querés
        > verla reflejada YA (sin esperar que venza la caché de 1 hora),
        > tocá el botón **"Resync forzado (bypass caché)"** que aparece
        > debajo del timestamp del último sync.
        """
    )

    st.divider()

    # ----- Modo Manual Secundario -----
    st.markdown(
        """
        ### Modo Manual Secundario (fallback)

        Si la API de Contabilium está **caída**, o si querés analizar
        una planilla puntual que no está en el sistema, hay un camino
        alternativo:

        1. En la sidebar, al final de la sección API, vas a ver un
           **expander colapsado** titulado **"Modo Manual Secundario"**.
           Hacé click en él para abrirlo.
        2. Adentro aparecen los **5 file uploaders** de siempre:
           `fc_semanal.xlsx`, `fc_mensual.xlsx`, `clientes.xlsx`,
           `productos.xlsx` y `combos.xlsx`.
        3. Arrastrá los 5 archivos (o clickealos para elegirlos).
        4. Tocá el botón **"Procesar planillas"**. El botón está
           deshabilitado hasta que los 5 estén cargados.
        5. A partir de ahí, el dashboard muestra los datos de esas
           planillas, exactamente como antes.

        > **Importante**: en Modo Manual **no hay vista de trimestre**
        > (porque los xlsx cubren solo semana + mes). Si necesitás
        > análisis trimestral, usá el Modo API.

        > **Cómo obtener cada xlsx**: al final de este tutorial hay una
        > sección detallada con los pasos desde Contabilium para cada
        > archivo.
        """
    )

    st.divider()

    # ----- La barra lateral -----
    st.markdown(
        """
        ### La barra lateral (sidebar)

        De arriba abajo tiene estos bloques:

        1. **Sincronizar desde Contabilium** (primario): selectores
           de mes / semana / trimestre + botón Sincronizar + botón
           Resync forzado.
        2. **Modo Manual Secundario** (expander colapsado): los 5
           file uploaders de siempre, como fallback.
        3. **Exportar agenda** (aparece cuando ya hay datos): permite
           descargar la agenda personal de cualquier vendedor
           (más detalle abajo).
        4. **Cerrar sesión**: todo abajo. La próxima vez que entres
           tenés que volver a poner la contraseña.
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
          equipo en cada período. Debajo de cada uno, el **número de
          tickets** y el **ticket promedio** del período (cuánto sale
          en promedio una factura).
        - **Tabla "Ventas por vendedor"** — para cada vendedor, su
          monto, unidades, tickets y ticket promedio de la semana y del
          mes lado a lado. Ordenada por monto del mes descendente.
        - **Tabla "Cobertura general por vendedor (mes)"** — para cada
          vendedor: `clientes_asignados`, `clientes_con_venta`,
          `cobertura_pct` (% de su cartera activa este mes),
          **Conc. 80%** (cuántos clientes concentran el 80% de su
          venta — cuanto más bajo, más dependiente de pocos) y
          **Mix top-3** (los 3 sub-rubros con mayor % de venta del
          vendedor).
        """
    )

    # Tab 2
    st.markdown(
        """
        #### Tab 2 — **Sub-rubro**

        Desglose de ventas por categoría de producto.

        Tiene:

        - Un **selector de período** arriba (Semana o Mes).
        - **Tres filtros opcionales lado a lado**:
          - **Familia** — agrupación más amplia del producto
            (ej. "ACC", "AFX"). Es una clasificación mantenida por
            Mariano como maestro estático, independiente del
            sub-rubro.
          - **Sub-rubro** — código corto histórico ("A", "BA", "PM",
            etc.) que viene del maestro de productos de Contabilium.
          - **SKU específico** — para aislar un producto puntual.
        - Los 3 filtros son **acumulativos**: elegí los que quieras y
          la tabla se filtra por todos a la vez.
        - **Tabla "Ventas por (vendedor, sub-rubro)"** — el monto y
          las unidades vendidas por cada combinación.

        Útil para responder preguntas tipo: "¿qué tan bien le está
        yendo a Mario con el sub-rubro Pinturas este mes?" o "¿cuánto
        vendimos de la familia AFX en el mes?".
        """
    )

    # Tab 3
    st.markdown(
        """
        #### Tab 3 — **Cobertura**

        Las métricas de "qué tan bien estás atendiendo a tu cartera".

        Tiene un **selector de período** arriba con 3 opciones:

        - **Semana** — típico para la reunión semanal.
        - **Mes** — foto del mes en curso.
        - **Trimestre** (sólo disponible en Modo API) — ventana móvil
          de 3 meses consecutivos que termina en el "mes final" que
          elegiste en la sidebar. Útil para QBR (Quarterly Business
          Review) y para detectar clientes que llevan ≥1 mes sin
          comprar. Si el mes final es el mes en curso, el rango se
          recorta a hoy.

        Y **4 bloques** de datos uno debajo del otro:

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
             Semana/Mes/Trimestre).
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

        Cuatro bloques de exploración profunda para identificar
        oportunidades concretas de venta. Todos operan sobre el
        **período seleccionado** (Mes / Semana / Trimestre, default
        Mes — la opción Trimestre sólo aparece en modo API).

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

        4. **Patrones temporales** — dos gráficos de barras lado a
           lado: **ventas por día de la semana** (lun-dom) y **ventas
           por quincena del mes** (1-15 vs 16-fin). Tiene su propio
           selector de vendedor. Útil para detectar patrones raros:
           un vendedor con toda la venta en la segunda quincena
           probablemente esté "empujando el cierre" artificialmente.
        """
    )

    # Tab 5
    st.markdown(
        """
        #### Tab 5 — **Salud**

        Diagnóstico de los datos cargados. Es la tab a la que
        recurrís cuando algo te llama la atención y querés entender
        si los datos están limpios.

        Arriba te muestra:

        - La **fuente activa** (API Contabilium o Manual Secundario).
        - Los **rangos exactos** que se sincronizaron (mes + semana +
          trimestre en modo API).
        - El **timestamp** del último sync exitoso.
        - Si el sync tuvo **errores de fetch** en algún comprobante
          individual (N+1 fallido), aparece un warning con la lista
          de IDs omitidos — útil para evaluar si los montos están
          subvaluados.

        Abajo, un panel para la semana y otro para el mes, cada uno
        con un **semáforo**:

        - **Verde** — todo OK, sin alertas.
        - **Amarillo** — warnings menores (NCF descuentos descartados,
          algunas filas no UYU, SKUs sin clasificar, clientes sin
          documento). Los datos siguen siendo confiables, solo te
          enteramos para que sepas.
        - **Rojo** — errores estructurales serios. Aparece arriba de
          todo el dashboard un **banner discreto**.
          Casos rojos típicos:
          - Vendedores con ventas pero sin cartera asignada en el
            maestro de clientes.
          - Documentos duplicados en clientes.
        """
    )

    st.divider()

    # ----- Exportar agenda -----
    st.markdown(
        """
        ### Exportar agenda personal por vendedor

        En la **barra lateral**, debajo del resto de los bloques, vas
        a encontrar (una vez que los datos estén cargados) un bloque
        **"Exportar agenda"** con:

        - Un **selector de vendedor** (sólo aparecen vendedores con
          cartera asignada; los IDs sin email mapeado y los vendedores
          vacíos se filtran automáticamente).
        - Un botón **"Descargar agenda.xlsx"**.

        Al hacer click, descargás un archivo Excel con **5 hojas** para
        que ese vendedor se lleve "su agenda" después de la reunión:

        | Hoja | Contenido |
        |---|---|
        | **Resumen** | Performance del período + cobertura + comparativa vs el promedio del equipo |
        | **Mi cartera** | Listado completo de sus clientes con monto del mes y la semana, unidades, y si compró este mes |
        | **Clientes dormidos** | Solo los clientes que no compraron este mes |
        | **Penetración** | El % de su cartera por sub-rubro (sus huecos de cross-sell) |
        | **Top 80%** | Los pocos clientes que generan el 80% de su venta — los que tiene que blindar |
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

    # ----- Cómo obtener cada planilla (solo Modo Manual) -----
    st.markdown(
        """
        ### Anexo — Cómo obtener cada xlsx (sólo Modo Manual Secundario)

        Esta sección es solo para cuando estás usando el **Modo Manual
        Secundario** (la API caída). En uso normal no necesitás
        descargar nada — el modo API lo hace todo automáticamente.

        > **Regla común**: al descargar cada archivo de Contabilium,
        > **tenés que renombrarlo manualmente** al nombre exacto que se
        > indica abajo. Si el archivo no se llama exactamente así, el
        > dashboard lo va a rechazar con un error.

        #### 1) `fc_semanal.xlsx` — facturación semanal

        1. Entrá a **Facturación** en Contabilium.
        2. Filtrá por las fechas de la última semana.
        3. Opción **"Detallada"** → **Descargar**.
        4. Renombrá el archivo a `fc_semanal.xlsx`.

        #### 2) `fc_mensual.xlsx` — facturación mensual

        1. Entrá a **Facturación**.
        2. Filtrá por el **mes en curso** (día 1 hasta hoy).
        3. Opción **"Detallada"** → **Descargar**.
        4. Renombrá el archivo a `fc_mensual.xlsx`.

        #### 3) `clientes.xlsx` — cartera de clientes

        1. Entrá a **Clientes**.
        2. Botón **Exportar** → **Descargar**.
        3. Renombrá a `clientes.xlsx`.

        #### 4) `productos.xlsx` — maestro de productos

        1. Entrá a **Productos y servicios**.
        2. Exportar → **"Simple"** → **Descargar**.
        3. Renombrá a `productos.xlsx`.

        #### 5) `combos.xlsx` — maestro de combos

        1. Entrá a **Productos y servicios**.
        2. Exportar → **"Detalle combos"** → **Descargar**.
        3. Renombrá a `combos.xlsx`.
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
        - Si estás en Modo API o Modo Manual

        Cualquier reporte detallado ayuda a corregirlo rápido.
        """
    )
