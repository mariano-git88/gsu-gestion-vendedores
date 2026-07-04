"""
tutorial_televentas.py — Tutorial ELI5 del CRM de Televentas.

Se pinta en un st.dialog desde el botón "Tutorial" de la sidebar de
`televentas_app.py`. Tono simple para la Vendedora Televentas.
"""

import streamlit as st


def render() -> None:
    st.markdown(
        """
        ### ¿Qué es esta herramienta? 📞

        Es tu **centro de trabajo de televentas**. Toma la base de clientes
        de Suprabond y te la arma como una **lista de a quién llamar**, con
        toda la info a mano para vender por teléfono o WhatsApp y cargar
        pedidos.
        """
    )
    st.divider()
    st.markdown(
        """
        ### El día a día, en 4 pasos

        **1) Poné tu nombre** (barra izquierda) — así quedan registradas
        tus gestiones.

        **2) Elegí una campaña** (arriba en la pestaña *Leads*):
        - **Recuperar dormidos** → clientes que compraban y hace +90 días
          que no.
        - **Sin compras** → no compraron en el período.
        - **Activos** → para profundizar / sumarles productos.

        Podés afinar con **Filtros avanzados** (departamento, vendedor,
        "compró tal subgrupo", etc.).

        **3) Abrí la ficha** de un cliente. Vas a ver todo:
        teléfono, **botón de WhatsApp** con mensaje listo, qué compra,
        hace cuánto no compra, y las gestiones anteriores.

        **4) Registrá la gestión** después de llamar: canal, resultado
        (atendió / no atendió / interesado / pedido…), una nota, y si
        corresponde, **cuándo rellamar**. Eso arma tu agenda.
        """
    )
    st.divider()
    st.markdown(
        """
        ### Cargar un pedido 🧾

        Desde la ficha del cliente, en **Cargar pedido**: buscás el
        producto, ponés la cantidad, lo agregás, y cuando está el pedido
        completo escribís **CONFIRMAR** y lo cargás. Se crea la orden en
        Contabilium (reserva stock) y queda registrado como gestión.

        > **No** genera factura ni cobranza — solo el pedido.
        """
    )
    st.markdown(
        """
        ### Seguimientos y clientes nuevos

        - **📅 Seguimientos de hoy**: los clientes que quedaron para
          rellamar hoy (o atrasados). Tu cola del día.
        - **➕ Cliente nuevo**: si conseguís una ferretería que no está en
          el sistema, la cargás acá y se crea en Contabilium.
        - **📊 Actividad**: tu tablero — cuántas llamadas, contactos y
          pedidos generaste.
        """
    )
    st.info(
        "Todo lo que escribe en Contabilium (pedido, cliente nuevo) te pide "
        "escribir **CONFIRMAR** antes. Ante la duda, preguntá a Mariano. 🙌"
    )
