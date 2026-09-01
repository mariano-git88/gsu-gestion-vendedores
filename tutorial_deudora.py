"""
tutorial_deudora.py — Contenido del tutorial de Cuenta Corriente.

Se pinta dentro de un st.dialog (modal) cuando el usuario hace click en
"Tutorial". Está escrito para Ernesto y Valeria, no para quien programa:
explica qué hace cada sección, qué significa cada número y —sobre todo— en
qué NO hay que confiar.

Si hay que actualizar el contenido, editar acá sin tocar `deudora_app.py`.
El registro de cambios va aparte, en `cambios_deudora.json`.
"""

import streamlit as st


def render() -> None:
    """Renderiza el tutorial completo dentro del modal."""

    st.markdown(
        """
### ¿Qué hace esta app?

Muestra **cuánto debe cada cliente y desde cuándo**, agrupado por vendedor.
Es la "deudora": lo mismo que en ERPA se bajaba del Summa, armado con los
datos de Contabilium.

Sirve para tres cosas:

1. **Hablar con un vendedor de su cartera** — cuánto tiene afuera y cuánto
   está vencido.
2. **Ir a visitar un cliente** sabiendo qué facturas tiene abiertas, sin
   llevar la copia de cada una.
3. **Contestar cuando el cliente discute** — la cuenta completa con
   facturas, notas de crédito y pagos en orden.

---

### Las tres secciones

**Por vendedor.** Una fila por vendedor: cuántos clientes con saldo tiene,
cuánta plata, cuánto vencido y cómo se reparte por antigüedad. Abajo se
descarga el PDF de uno solo, que es el que se le manda.

**Clientes.** El listado completo, filtrable por vendedor y por antigüedad,
con buscador por nombre o RUT. Es la vista para preparar la semana: filtrás
"Más de 90" y tenés la lista de a quién hay que llamar.

**Cuenta de un cliente.** El extracto. Por defecto muestra solo las facturas
con saldo, que es lo que hay que cobrar. Apagando el candado *"Solo facturas
con saldo"* aparece la cuenta completa: cada factura, cada nota de crédito y
cada pago, en orden de fecha.

---

### Qué significa cada número

**Deuda** — todo lo que el cliente debe hoy, esté vencido o no. Ya tiene
descontadas las notas de crédito que le quedaron sin aplicar.

**Vencido** — la parte de esa deuda que ya pasó su fecha. Es el número que
importa para salir a cobrar.

**A favor** — notas de crédito que el cliente tiene sin usar. Es plata que
ya no debe. Si tiene más crédito que deuda, la Deuda le aparece en negativo:
no es un error, le debemos nosotros a él.

**Días de la más vieja** — cuántos días hace que venció la factura más
antigua que sigue abierta. Es el mejor indicador de si un cliente arrastra
una cola vieja o está simplemente al día con lo del mes.

**Tramos (Al día / 1 a 30 / 31 a 60 / 61 a 90 / Más de 90)** — cómo se
reparte la deuda según cuánto hace que venció cada factura.

---

### En qué NO confiar

**El vencimiento de acá no es el que dice la factura impresa.** Contabilium
imprime siempre 30 días, aunque la venta sea a 60 o a 90. La app calcula el
vencimiento real a partir de la condición de pago. Esto es a propósito: se
decidió no cambiar lo que ve el cliente, porque al ver 90 días empezaría a
pagar a los 90. Si un cliente reclama con la factura en la mano, tiene razón
él en lo que dice el papel, y tiene razón la app en lo que se pactó.

**Las ventas en cuotas vienen como una sola factura, hasta que alguien las
divide.** Contabilium emite un solo comprobante por una venta a 30/60/90,
con un solo saldo: no sabe qué parte vence en cada cuota, así que por
defecto la app le pone el vencimiento promedio. Se arregla dividiéndola a
mano una sola vez — ver más abajo.

**No están los cheques en cartera.** El "Saldo Documentos" del informe viejo
no existe en Contabilium: un cheque queda registrado como forma de pago de
un recibo, pero no hay una lista de cheques a cobrar con sus fechas.

**Solo se ve lo que entra en la ventana de meses.** Con 12 meses (el valor
por defecto) una factura impaga de hace dos años no aparece. Si sospechan
de un cliente muy viejo, subir el control de "Meses de historia" en el
panel de la izquierda.

---

### Dividir una factura en cuotas

En **Cuenta de un cliente**, al final, está *"Dividir una factura en cuotas"*.
Sirve para las ventas a 30/60/90 y también para el cliente al que se le
cobra de a poco aunque la condición no lo diga.

Cómo funciona:

1. Elegís la factura. La app propone las cuotas leyendo la condición de
   pago: una venta a 30/60/90 emitida el 1° de junio propone tres cuotas
   que vencen el 1° de julio, el 31 de julio y el 30 de agosto.
2. Corregís fechas e importes si el acuerdo real fue otro. Podés agregar o
   sacar filas.
3. Ponés tu nombre y guardás.

**Se carga una sola vez y queda hasta que la factura se cobre.** No hay que
ir marcando qué cuota se pagó: la app reparte el saldo que informa
Contabilium entre las cuotas, de la más vieja a la más nueva. Si la factura
era de $3.000 en tres cuotas y el saldo bajó a $2.000, la primera cuota
aparece cobrada sola.

Tres cosas importantes:

- **Dividir no cambia lo que el cliente debe.** Reparte el mismo saldo entre
  varias fechas. Lo que sí cambia es la antigüedad: cada cuota cae en el
  tramo que le corresponde en vez de amontonarse toda en uno.
- **La app no deja guardar una división que no cierra** con el total de la
  factura. Si las cuotas suman de más o de menos, avisa y no guarda.
- **Se puede corregir**: volvés a entrar, cambiás y guardás de nuevo. Queda
  el historial de quién cargó qué. Con *"Deshacer la división"* la factura
  vuelve a mostrarse entera.

---

### Cosas que conviene saber

**Los clientes sin vendedor asignado aparecen agrupados aparte.** Son unos
190 y tienen deuda real. No se esconden a propósito: si no estuvieran, esa
plata no aparecería en la pantalla de nadie. Cuando se le asigne vendedor a
uno en Contabilium, pasa solo a la cartera que corresponde.

**Las grandes superficies cuentan distinto.** Sodimac, Geant, Disco, Devoto
y Mosca juntan las facturas del mes y recién ahí arrancan a contar los días.
La app ya lo tiene contemplado. Tienda Inglesa está pendiente de confirmar.

**Los datos se bajan una vez por día.** La primera persona que abre la app
espera unos minutos mientras se traen las facturas y los recibos; el resto
del día entra al instante. Con el botón *"Recargar datos"* se fuerza una
bajada nueva si acaba de entrar una cobranza importante.
        """
    )
