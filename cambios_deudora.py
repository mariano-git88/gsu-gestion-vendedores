"""
cambios_deudora.py — Registro de actualizaciones de Cuenta Corriente.

Se pinta en un modal, igual que el tutorial, cuando se aprieta el botón
"Novedades". Está pensado para que quien usa la app se entere de qué cambió
sin tener que leer commits.

**El contenido vive en `cambios_deudora.json`, no en este archivo, y es a
propósito.** Streamlit Cloud a veces se queda con una copia vieja de los
módulos importados aunque el push haya entrado bien. Un archivo de datos se lee
**en cada corrida**, así que no se puede quedar viejo. Ese es todo el motivo
del JSON separado. (Mismo criterio que `cambios.py` del repo de MercadoLibre,
donde el problema se vio dos veces en un día.)

**Cómo mantenerlo:** cada vez que se pushea algo que el usuario nota, agregar
una entrada ARRIBA de la lista en `cambios_deudora.json`. La fecha y la hora
son las del push. Escribir en criollo lo que cambia para quien usa la app, no
lo que cambió en el código: "ahora los clientes que arrastran restos viejos no
caen en rojo" y no "se agregó marcar_residuos()".

Cada entrada:
    fecha   'YYYY-MM-DD'
    hora    'HH:MM'
    titulo  una linea
    tipo    'nuevo' | 'mejora' | 'arreglo'
    puntos  lista de bullets, opcional

Ojo con los importes: Streamlit toma lo que va entre dos `$` como fórmula
LaTeX. En el JSON van escapados con barra invertida.
"""

import json
from pathlib import Path

import streamlit as st

ARCHIVO = Path(__file__).resolve().parent / "cambios_deudora.json"

ICONO = {"nuevo": "🆕", "mejora": "⬆️", "arreglo": "🔧"}


def cargar():
    """
    Lee el registro del disco. **Sin cachear, a propósito** — ver la nota de
    arriba: cachearlo reintroduce el problema que este archivo vino a evitar.

    Nunca lanza: si el archivo falta o está roto devuelve vacío, y el modal
    muestra un aviso en vez de tumbar la app.
    """
    try:
        datos = json.loads(ARCHIVO.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return datos if isinstance(datos, list) else []


def _dma(fecha):
    """'2026-08-14' -> '14/08/2026'. Vacío si la fecha no tiene la forma."""
    partes = str(fecha or "").split("-")
    if len(partes) != 3:
        return ""
    a, m, d = partes
    return f"{d}/{m}/{a}"


def ultima_actualizacion():
    """'14/08/2026 11:40' de la entrada más reciente, para el encabezado."""
    datos = cargar()
    if not datos:
        return ""
    return f"{_dma(datos[0].get('fecha'))} {datos[0].get('hora', '')}".strip()


def cuantos_desde(fecha=None):
    """Cuántas entradas hay desde una fecha (para un badge, si algún día va)."""
    datos = cargar()
    if not fecha:
        return len(datos)
    return sum(1 for c in datos if str(c.get("fecha", "")) > fecha)


def render() -> None:
    """Pinta el registro completo dentro del modal."""
    datos = cargar()
    if not datos:
        st.warning(
            f"No pude leer el registro de novedades (`{ARCHIVO.name}`).",
            icon="⚠️",
        )
        return

    st.caption(
        "Qué fue cambiando en la app, de lo más nuevo a lo más viejo. "
        "Las fechas son las de cada actualización."
    )

    dia_anterior = None
    for c in datos:
        if c.get("fecha") != dia_anterior:
            st.markdown(f"### {_dma(c.get('fecha'))}")
            dia_anterior = c.get("fecha")

        st.markdown(
            f"**{ICONO.get(c.get('tipo'), '•')} {c.get('hora', '')} — "
            f"{c.get('titulo', '')}**"
        )
        for p in c.get("puntos", []):
            st.markdown(f"- {p}")
        st.markdown("")
