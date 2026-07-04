"""
tutorial_televentas.py — Manual completo (ELI5) del CRM de Televentas.

Se pinta en un st.dialog desde el botón "📖 Tutorial" de la sidebar de
`televentas_app.py`. Está pensado como manual integral: alguien que lo lee
una vez tiene que entender TODAS las funciones y poder explicárselas a otra
persona que nunca vio la herramienta. Por eso está dividido en secciones
(pestañas internas) y cada función se explica con: qué es, para qué sirve y
el paso a paso.
"""

import streamlit as st


def render() -> None:
    st.caption("Manual completo. Leelo una vez y vas a poder usar —y explicar— "
               "toda la herramienta. Está dividido en secciones 👇")

    (t_intro, t_arranque, t_buscar, t_ficha, t_pedido,
     t_otras, t_faq) = st.tabs([
        "1. Qué es", "2. Arrancar", "3. Buscar leads", "4. La ficha",
        "5. Cargar pedido", "6. Otras pestañas", "7. Dudas comunes"])

    # =================================================================
    with t_intro:
        st.markdown(
            """
            ### ¿Qué es esta herramienta? 📞

            Es el **centro de trabajo de la Vendedora Televentas**: la persona
            que vende por **teléfono y WhatsApp** (no yendo a la ferretería).

            La herramienta agarra **toda la base de clientes de Suprabond**
            (la misma que está en Contabilium) y la convierte en una **lista
            de a quién llamar**, con toda la información a mano para vender y,
            si el cliente quiere, **cargarle un pedido** en el momento.

            ### Las 3 cosas que hace, en criollo

            1. **Te dice a quién llamar** — filtra la base (ej. "los que hace
               más de 90 días que no compran", "los de Montevideo", "los que
               compran silicona"). Podés armar tu lista del día.
            2. **Te da todo para la llamada** — al abrir un cliente ves su
               teléfono, qué compra, hace cuánto no compra, si debe plata,
               qué conviene ofrecerle, y un botón para escribirle por WhatsApp.
            3. **Registra lo que hacés y te deja vender** — anotás el resultado
               de cada llamada (atendió, no atendió, interesado, etc.), agendás
               rellamados, y podés cargar el pedido y hasta dar de alta un
               cliente nuevo.

            ### Conceptos que conviene entender antes

            **🟢 Segmentos (colores del cliente según cuándo compró):**
            - **Activo** → compró hace menos de 90 días.
            - **Dormido** → hace entre 90 y 180 días que no compra.
            - **Dormido profundo** → hace más de 180 días.
            - **Sin compras** → no compró dentro del período que estás mirando.

            **✍️ Qué "toca" Contabilium y qué no:**
            - **Solo mira** (no cambia nada): buscar clientes, ver historial,
              filtrar, ver deuda.
            - **Escribe de verdad** (¡ojo!): **cargar un pedido** y **crear un
              cliente nuevo**. Las dos te piden escribir **CONFIRMAR** antes.
            - **Anotar gestiones y listas** se guarda en una planilla de Google
              (el "cuaderno" del CRM), no en Contabilium.

            **📒 El "cuaderno" (Google Sheet):** todo lo que registrás
            (llamadas, resultados, seguimientos, listas importadas) queda
            guardado en una planilla de Google. Por eso, si mañana volvés a
            abrir la app, tus notas y agenda **siguen ahí**.
            """
        )

    # =================================================================
    with t_arranque:
        st.markdown(
            """
            ### Entrar y preparar la sesión

            **1) Ingresás con la contraseña** de la app.

            **2) Poné tu nombre de agente** (barra izquierda, "Tu nombre").
            Es importante: **todo lo que registres queda a tu nombre**. Si no
            lo ponés, la app no te deja guardar gestiones.

            ### La barra de la izquierda (sidebar)

            - **📖 Tutorial** → abre este manual.
            - **Tu nombre (agente)** → quién sos (ver arriba).
            - **Meses de historial** → cuántos meses de compras mira la app
              para clasificar a los clientes. Más meses = clasifica mejor a
              los "dormidos", pero **la primera carga tarda más**. Default: 6.
            - **🔄 Resincronizar** → vuelve a bajar los datos frescos de
              Contabilium (clientes, compras, productos, y tus gestiones).
              Usalo si cargaste un cliente nuevo o hiciste cambios y querés
              verlos reflejados.

            ### ⏳ La primera carga tarda (es normal)

            La **primera vez** que abrís la app (o después de Resincronizar),
            tarda un rato en bajar el historial. **Es normal, no está colgada.**
            Después de esa primera carga, durante las siguientes horas anda
            **al toque** (queda guardado en memoria).

            ### El tablero de arriba (los números)

            Apenas entrás, arriba ves 5 números que resumen tu base:
            - **Clientes** → total en la base.
            - **Con teléfono** → cuántos tienen teléfono cargado (a esos podés
              llamar/escribir).
            - **Dormidos (>90d)** → cuántos hace más de 90 días que no compran
              (tu principal oportunidad de recuperar).
            - **Gestionados** → a cuántos ya les registraste alguna gestión.
            - **Seguimientos hoy** → cuántos tenés agendados para rellamar hoy.
            """
        )

    # =================================================================
    with t_buscar:
        st.markdown(
            """
            ### Pestaña 🎯 Leads — encontrar a quién llamar

            Esta es tu pantalla principal. De arriba hacia abajo:

            #### A) Trabajar con una lista importada (opcional)
            Arriba de todo hay un selector **"📋 Trabajar con lista
            importada"**. Sirve para trabajar **solo sobre una lista puntual**
            (ej. la que subió Ernesto). Si elegís **"(Toda la base)"**, ves
            todos los clientes. (Cómo subir una lista → pestaña 6 del manual.)

            #### B) Campañas rápidas
            Botones que arman una cola de llamado con un click:
            - **Todos** → sin filtro de segmento.
            - **Recuperar dormidos (>90d)** → los que hace rato no compran. La
              campaña estrella para reactivar clientes.
            - **Sin compras en la ventana** → no compraron en el período.
            - **Activos (profundizar)** → los que ya compran, para venderles
              más.

            #### C) Botón "🛒 Cargar detalle de productos"
            Por defecto la app carga rápido pero **sin** saber qué productos
            puntuales compra cada uno. Si tocás este botón, baja ese detalle
            (tarda un poco). Recién ahí se habilitan:
            - El filtro por **subgrupo** (compró / no compró tal familia).
            - Las **sugerencias de qué ofrecerle** en la ficha.

            #### D) Filtros avanzados (desplegable "🔎")
            Se combinan entre sí (todos juntos). Tenés:
            - **Departamento** y **Vendedor asignado** → por zona / por vendedor.
            - **Compró subgrupo** / **NO compró subgrupo** → ej. "compra
              silicona pero NO compra espuma" (ideal para venta cruzada).
            - **Solo con teléfono** → oculta los que no tienen cómo contactar.
            - **Buscar** → por nombre, RUT o código.
            - **🚧 Ocultar atendidos recién** → saca a los que compraron hace
              muy poco (porque probablemente su vendedor de calle ya los
              atendió — no conviene pisarlo).
            - **💰 Solo con deuda** → deja solo los que deben plata.

            #### E) La tabla de leads
            Muestra el resultado de tus filtros. Columnas: código, nombre,
            ciudad, departamento, teléfono, segmento, días sin comprar,
            ticket promedio, **deuda**, estado de gestión y próximo seguimiento.

            👉 **Para abrir un cliente: hacé click en su fila.** Se abre su
            ficha completa abajo.
            """
        )

    # =================================================================
    with t_ficha:
        st.markdown(
            """
            ### La ficha del cliente (360°) — todo para la llamada

            Cuando hacés click en una fila, abajo se abre la ficha. Tiene, de
            arriba a abajo:

            #### 1) Datos de contacto y perfil
            Razón social, RUT, código, vendedor asignado, dirección, teléfono
            y email. Más dos números clave: **días sin comprar** y **ticket
            promedio** (cuánto gasta en promedio por compra).

            #### 2) Avisos importantes (si aplican)
            - **🚧 Aviso de "atendido recién"** → compró hace poquito;
              probablemente su vendedor de calle ya pasó. Evitá pisarlo.
            - **💰 Aviso de deuda** → cuánta plata debe, y cuánto está
              **vencida** (en rojo). Útil para recordárselo con tacto.
            - **📝 Nota de entrega** → horarios/indicaciones del cliente
              (sale de Contabilium).

            #### 3) Sugerencias de qué ofrecerle
            (Aparecen solo si activaste "Cargar detalle de productos".)
            - **🔁 Volvé a ofrecerle** → cosas que **compraba y dejó** de
              comprar. Recordáselas.
            - **➕ Podés sumarle** → productos **populares entre lo que ya
              compra** que todavía no lleva. Venta cruzada.

            #### 4) 💬 Botón de WhatsApp
            Abre WhatsApp con el número del cliente **y un mensaje ya escrito**
            de presentación. Solo tenés que revisarlo y mandarlo. (Si el
            cliente no tiene un celular válido, el botón no aparece.)

            #### 5) 🛒 Historial de compras (desplegable)
            Sus **top productos**, los **subgrupos** que compra, y la lista de
            sus **comprobantes** (fecha y monto). Para saber de qué hablarle.

            #### 6) 📇 Gestiones anteriores (desplegable)
            Todo lo que vos u otra persona ya registró con ese cliente:
            cuándo, por qué canal, con qué resultado y qué nota. Para no
            repetir ni pisar lo hecho.

            #### 7) 📝 Registrar gestión — ¡el paso más importante!
            Después de cada llamada/WhatsApp, **anotá qué pasó**:
            1. Elegí el **Canal** (Llamada o WhatsApp).
            2. Elegí el **Resultado**: No atendió / Volver a llamar /
               Contactado - interesado / Contactado - no interesado / Pedido
               cargado / Datos actualizados / Número equivocado / No molestar.
            3. Escribí una **Nota** (qué se habló, qué quedó pendiente).
            4. Si hay que rellamar, poné la fecha de **Próximo seguimiento**.
            5. **Guardar gestión.** Queda en el cuaderno y arma tu agenda.

            > 💡 Registrar bien es lo que hace útil a la herramienta: alimenta
            > tu agenda, el tablero, y que la próxima llamada arranque sabiendo
            > lo anterior.
            """
        )

    # =================================================================
    with t_pedido:
        st.markdown(
            """
            ### 🧾 Cargar un pedido (dentro de la ficha del cliente)

            Si el cliente quiere comprar, cargás el pedido sin salir de la
            ficha. **Importante:** se cargan solo productos con **stock en el
            depósito VENTAS**, y esto **crea la orden en Contabilium** (reserva
            el stock). **No** genera factura ni cobranza — solo el pedido.

            **Paso a paso:**

            1. En **"Buscar producto"** escribí parte del **SKU o del nombre**.
               A medida que escribís, aparecen los resultados (solo con stock).
            2. **Hacé click en el producto** que querés. Se agrega a una
               **tabla de abajo**.
            3. Repetí con todos los productos del pedido.
            4. En la tabla de abajo, **poné las cantidades** en la columna
               "Cantidad" (podés editar cada fila). El precio sale del sistema.
               Si ponés cantidad 0, ese ítem no se carga.
            5. Mirá el **Total del pedido** que se calcula solo.
            6. Si te equivocaste, **🗑️ Vaciar** borra todo y empezás de nuevo.
            7. Cuando esté listo, escribí **CONFIRMAR** (en mayúsculas) en el
               casillero. Recién ahí se habilita el botón.
            8. Tocá **🚀 Cargar pedido en Contabilium**. Te avisa el **número
               de orden** que quedó, y registra solo una gestión "Pedido
               cargado" a tu nombre.

            > ⚠️ Empezá con pedidos chicos las primeras veces para agarrarle
            > la mano con tranquilidad. Y revisá bien las cantidades antes de
            > confirmar: la orden reserva stock de verdad.
            """
        )

    # =================================================================
    with t_otras:
        st.markdown(
            """
            ### 📋 Pestaña "Listas importadas"

            Para trabajar sobre una **selección puntual** de clientes (ej. una
            planilla que armó Ernesto o Valeria).

            **Cómo subir una lista:**
            1. Andá a la pestaña **📋 Listas importadas**.
            2. Subí un archivo **Excel (.xlsx / .xls) o CSV**. Tiene que tener
               una columna que identifique al cliente: **código** (ej.
               `04001-C`), **RUT/documento**, o el **número** del cliente.
            3. La app te muestra **cuántos encontró** y cuáles **no** (para que
               revises si hay algún código mal).
            4. Ponele un **nombre** a la lista, ej.
               `SELECCIONADOS POR ERNESTO 04 07 26`.
            5. **💾 Guardar lista.**

            **Cómo usarla después:** en la pestaña **Leads**, arriba, elegí la
            lista en el selector "Trabajar con lista importada". La app va a
            mostrar **solo esos clientes** (y podés combinarlo con campañas y
            filtros). Las listas quedan guardadas y las podés reusar **otro
            día**.

            ---
            ### 📅 Pestaña "Seguimientos de hoy"

            Tu **cola del día**: los clientes que agendaste para rellamar hoy
            (y los **atrasados** de días anteriores). Aparece la fecha, el
            cliente, su teléfono y cuál fue el último resultado. Trabajá esta
            lista para no perder ningún seguimiento.

            ---
            ### ➕ Pestaña "Cliente nuevo"

            Si conseguís una ferretería que **no está** en el sistema, la
            cargás acá y **se crea en Contabilium**.

            **Paso a paso:**
            1. Completá los datos. **Razón social es obligatoria**; cargá
               también teléfono, departamento y ciudad (así después la podés
               llamar y filtrar).
            2. Elegí el **Vendedor asignado** (importante para las comisiones).
            3. Escribí **CONFIRMAR** y tocá **Crear cliente en Contabilium**.
            4. Te da el **código** del cliente nuevo. Después tocá
               **🔄 Resincronizar** para que aparezca en tus leads.

            ---
            ### 📊 Pestaña "Actividad"

            Tu **tablero de gestión**: cuántas gestiones hiciste, cuántos
            **contactos efectivos** (hablaste con alguien), cuántos **pedidos**
            cargaste y **cuánta plata** generaste. Más dos gráficos: gestiones
            por día y por tipo de resultado. Sirve para ver tu propio ritmo y
            para que gerencia siga la operación.
            """
        )

    # =================================================================
    with t_faq:
        st.markdown(
            """
            ### Preguntas frecuentes

            **¿Abrir la app o buscar clientes cambia algo en Contabilium?**
            No. Buscar, filtrar y ver fichas es **solo mirar**. Lo único que
            escribe es **cargar un pedido** y **crear un cliente**, y ambos
            piden **CONFIRMAR**.

            **La primera carga tarda muchísimo. ¿Está rota?**
            No. La primera vez baja el historial y tarda. Después queda rápida
            por varias horas. Si de verdad quedó trabada mucho rato, avisá.

            **No encuentro un cliente que sé que existe.**
            Puede que su última compra quede fuera de la ventana. Subí los
            "Meses de historial" en la barra izquierda, o usá el buscador por
            nombre/RUT.

            **No me aparecen las sugerencias de productos ni el filtro por
            subgrupo.** Tenés que tocar **"🛒 Cargar detalle de productos"** en
            la pestaña Leads (baja un detalle extra que por defecto no se carga
            para que la app abra rápido).

            **Registré una gestión y no la veo / no puedo guardar.**
            Asegurate de haber puesto **tu nombre de agente** en la barra
            izquierda. Y que esté configurada la planilla del CRM (si no, avisá
            a Mariano).

            **Cargué un cliente nuevo y no aparece en la lista.**
            Tocá **🔄 Resincronizar**. Los clientes nuevos aparecen después de
            volver a bajar los datos.

            **¿Puedo deshacer un pedido cargado?**
            Desde la app no. Como en Contabilium normal, se anula la orden a
            mano. Por eso conviene revisar las cantidades antes de CONFIRMAR.

            **¿La deuda que muestra es toda la deuda del cliente?**
            Es la deuda de los comprobantes dentro de la ventana de meses que
            estás mirando. Una factura impaga muy vieja podría no contarse; si
            necesitás el total exacto, ampliá los meses o confirmá en
            Contabilium.
            """
        )
        st.success("Cualquier duda o algo que no cuadre, hablá con Mariano. "
                   "Mejor preguntar antes de cargar o crear algo. 🙌")
