"""
theme.py — Tema visual estilo Dieter Rams / Vitsoe.

Inspiración: vitsoe.com y los principios de diseño de Dieter Rams
("as little design as possible").

Decisiones:
  - Paleta restringida: negro casi puro, blanco, grises, un solo acento
    naranja-óxido usado con disciplina.
  - Tipografía dominante: sans-serif del sistema, jerarquía clara,
    letter-spacing apretado en headings.
  - Sin border-radius (esquinas rectas), sin sombras (o casi).
  - Bordes finísimos para separar contenido, en lugar de fondos.
  - Whitespace generoso.
  - Hide Streamlit chrome (menú hamburger, footer, toolbar) para que el
    app se sienta como producto, no como demo de Streamlit.
  - Logo de grupo Suprabond arriba en la sidebar (vía `st.logo()`).

Llamar `apply_theme()` UNA vez al principio de app.py, justo después de
`st.set_page_config()`.
"""

from pathlib import Path

import streamlit as st

# Path absoluto al logo. Usar Path(__file__).parent para que funcione
# tanto local como en Streamlit Cloud, sin depender del cwd.
# IMPORTANTE: el directorio en disco es `assets` (lowercase), no `Assets`.
# Linux es case-sensitive, así que el case tiene que coincidir exacto con
# lo que git tiene tracked, sino falla en Streamlit Cloud aunque funcione
# en WSL/Windows (que son case-insensitive).
LOGO_PATH = Path(__file__).parent / "assets" / "logo.png"

# ----- Paleta -----
INK = "#1A1A1A"           # casi negro, texto principal
TEXT_SOFT = "#767676"     # gris medio, texto secundario / labels
LINE = "#E0E0E0"          # gris claro, divisores y bordes
LINE_SOFT = "#FAFAFA"     # casi blanco, fondos sutiles (sidebar)
ACCENT = "#C8552F"        # naranja-óxido tipo Vitsoe, usado con disciplina
ACCENT_DARK = "#A8451F"   # variante más oscura para hovers/active
WHITE = "#FFFFFF"

CUSTOM_CSS = f"""
<style>
/* ==============================================================
   Dieter Rams / Vitsoe theme — Gestión de Vendedores GSU
   ============================================================== */

/* ----- Tipografía global ----- */
html, body, [data-testid="stAppViewContainer"], [class*="css"] {{
    font-family: -apple-system, BlinkMacSystemFont, "Inter", "Helvetica Neue",
                 Helvetica, Arial, sans-serif;
    color: {INK};
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
}}

/* ----- Headings: tipografía como elemento dominante ----- */
h1 {{
    color: {INK} !important;
    font-weight: 600 !important;
    font-size: 2.1rem !important;
    letter-spacing: -0.025em !important;
    line-height: 1.15 !important;
    margin: 0 0 0.5rem 0 !important;
}}
h2 {{
    color: {INK} !important;
    font-weight: 500 !important;
    font-size: 1.45rem !important;
    letter-spacing: -0.015em !important;
    line-height: 1.25 !important;
    margin: 2.25rem 0 1rem 0 !important;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid {LINE};
}}
h3 {{
    color: {INK} !important;
    font-weight: 500 !important;
    font-size: 1.05rem !important;
    letter-spacing: -0.005em !important;
    margin: 1.75rem 0 0.85rem 0 !important;
}}

p, div, span, li {{
    line-height: 1.6;
}}

/* ----- Logo en la sidebar (st.logo) ----- */
/* max-height 280px = 5x el default (56px). max-width 100% evita que
   el logo se salga de la sidebar si la imagen es más ancha que alta. */
[data-testid="stLogo"] img,
[data-testid="stSidebarHeader"] img {{
    max-height: 280px !important;
    max-width: 100% !important;
    width: auto !important;
    height: auto !important;
    margin: 0.5rem 0 1rem 0 !important;
}}

/* El contenedor del logo (stSidebarHeader y stLogo) tiene una altura
   fija heredada de Streamlit — hay que destrabarla para que el logo
   grande no se clippee por arriba. */
[data-testid="stSidebarHeader"],
[data-testid="stLogo"],
[data-testid="stSidebarHeader"] > div,
[data-testid="stLogoSpacer"] {{
    height: auto !important;
    min-height: 0 !important;
    max-height: none !important;
    overflow: visible !important;
    padding-top: 0.5rem !important;
    padding-bottom: 0.5rem !important;
}}

/* ----- Sidebar: limpio, casi blanco ----- */
[data-testid="stSidebar"] {{
    background-color: {LINE_SOFT};
    border-right: 1px solid {LINE};
}}
[data-testid="stSidebar"] h2 {{
    border: none !important;
    padding: 0 !important;
    font-size: 0.78rem !important;
    text-transform: uppercase;
    letter-spacing: 0.12em !important;
    color: {TEXT_SOFT} !important;
    margin-top: 1.75rem !important;
    margin-bottom: 0.75rem !important;
    font-weight: 500 !important;
}}
[data-testid="stSidebar"] h1 {{
    font-size: 1.3rem !important;
    margin-bottom: 1rem !important;
}}

/* ----- Botones: planos, sin radius, hover acento ----- */
.stButton > button,
.stDownloadButton > button,
[data-testid="stFormSubmitButton"] > button {{
    background-color: {INK};
    color: {WHITE};
    border: 1px solid {INK};
    border-radius: 0;
    padding: 0.7rem 1.6rem;
    font-weight: 500;
    font-size: 0.92rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    transition: background-color 0.18s ease, border-color 0.18s ease;
    box-shadow: none;
}}
.stButton > button:hover,
.stDownloadButton > button:hover,
[data-testid="stFormSubmitButton"] > button:hover {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    color: {WHITE};
    transform: none;
}}
.stButton > button:active,
.stDownloadButton > button:active,
[data-testid="stFormSubmitButton"] > button:active {{
    background-color: {ACCENT_DARK};
    border-color: {ACCENT_DARK};
}}

/* ----- File uploaders: borde fino, sin fill ----- */
[data-testid="stFileUploader"] section {{
    border: 1px solid {LINE} !important;
    border-radius: 0 !important;
    background-color: {WHITE} !important;
    padding: 1.1rem !important;
    transition: border-color 0.18s ease;
}}
[data-testid="stFileUploader"] section:hover {{
    border-color: {INK} !important;
    background-color: {WHITE} !important;
}}
[data-testid="stFileUploader"] small {{
    color: {TEXT_SOFT};
    font-size: 0.78rem;
}}
[data-testid="stFileUploader"] label {{
    font-size: 0.82rem !important;
    font-weight: 500 !important;
    color: {INK} !important;
}}

/* ----- Inputs de texto: línea inferior estilo formulario clásico ----- */
.stTextInput input,
.stNumberInput input,
.stTextArea textarea {{
    border-radius: 0 !important;
    border: 1px solid {LINE} !important;
    border-bottom-width: 2px !important;
    padding: 0.7rem 0.85rem !important;
    font-size: 0.95rem !important;
    background-color: {WHITE} !important;
    transition: border-color 0.15s ease;
}}
.stTextInput input:focus,
.stNumberInput input:focus,
.stTextArea textarea:focus {{
    border-color: {INK} !important;
    border-bottom-color: {ACCENT} !important;
    box-shadow: none !important;
    outline: none !important;
}}

/* ----- Métricas: sin card, solo tipografía ----- */
[data-testid="stMetric"] {{
    background-color: transparent;
    border: none;
    border-top: 1px solid {LINE};
    border-radius: 0;
    padding: 1.1rem 0 0.5rem 0;
    box-shadow: none;
}}
[data-testid="stMetricValue"] {{
    color: {INK} !important;
    font-weight: 600 !important;
    font-size: 2.4rem !important;
    letter-spacing: -0.02em !important;
    line-height: 1.1 !important;
}}
[data-testid="stMetricLabel"] {{
    color: {TEXT_SOFT} !important;
    font-weight: 500 !important;
    text-transform: uppercase;
    letter-spacing: 0.10em;
    font-size: 0.74rem !important;
}}
[data-testid="stMetricDelta"] {{
    color: {TEXT_SOFT} !important;
    font-weight: 500 !important;
}}

/* ----- Dataframes: bordes finos, sin radius ----- */
[data-testid="stDataFrame"] {{
    border-radius: 0 !important;
    border: 1px solid {LINE} !important;
    box-shadow: none !important;
}}

/* ----- Tabs: planas, subrayado naranja en activa ----- */
[data-testid="stTabs"] {{
    border-bottom: 1px solid {LINE};
    margin-bottom: 1.5rem;
}}
[data-testid="stTabs"] button[role="tab"] {{
    padding: 0.7rem 1.4rem !important;
    background-color: transparent !important;
    border-radius: 0 !important;
    color: {TEXT_SOFT} !important;
    font-weight: 500 !important;
    font-size: 0.92rem !important;
    letter-spacing: 0.02em;
    border: none !important;
    border-bottom: 2px solid transparent !important;
    margin-bottom: -1px;
}}
[data-testid="stTabs"] button[role="tab"]:hover {{
    color: {INK} !important;
    background-color: transparent !important;
}}
[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {{
    color: {INK} !important;
    border-bottom: 2px solid {ACCENT} !important;
    background-color: transparent !important;
}}

/* ----- Form (login): card mínima ----- */
[data-testid="stForm"] {{
    background-color: {WHITE};
    border: 1px solid {LINE};
    border-radius: 0;
    padding: 2.25rem 2rem;
    box-shadow: none;
}}

/* ----- Alertas: bordes finos, sin radius, accent en error ----- */
[data-testid="stAlert"] {{
    border-radius: 0 !important;
    border: 1px solid {LINE} !important;
    border-left-width: 3px !important;
    box-shadow: none !important;
    padding: 0.85rem 1.1rem !important;
    background-color: {WHITE} !important;
}}
[data-testid="stAlert"][data-baseweb="notification"] {{
    background-color: {WHITE} !important;
}}

/* ----- Captions: discretas ----- */
[data-testid="stCaptionContainer"], .stCaption {{
    color: {TEXT_SOFT};
    font-size: 0.84rem;
    line-height: 1.55;
}}

/* ----- Expanders: bordes finos, header limpio ----- */
[data-testid="stExpander"] {{
    border: 1px solid {LINE} !important;
    border-radius: 0 !important;
    background-color: {WHITE} !important;
    box-shadow: none !important;
}}
[data-testid="stExpander"] summary {{
    padding: 0.85rem 1.1rem !important;
    font-weight: 500 !important;
    font-size: 0.92rem !important;
    background-color: transparent !important;
}}
[data-testid="stExpander"] summary:hover {{
    background-color: {LINE_SOFT} !important;
}}

/* ----- Radio buttons: planos ----- */
.stRadio > div {{
    gap: 1rem !important;
}}
.stRadio label {{
    font-size: 0.9rem !important;
    color: {INK} !important;
}}

/* ----- Selectbox: bordes rectos ----- */
.stSelectbox div[data-baseweb="select"] > div {{
    border-radius: 0 !important;
    border: 1px solid {LINE} !important;
    background-color: {WHITE} !important;
}}
.stSelectbox div[data-baseweb="select"]:hover > div {{
    border-color: {INK} !important;
}}

/* ----- Divider más sutil ----- */
hr {{
    border-color: {LINE} !important;
    margin: 2rem 0 !important;
}}

/* ----- Hide Streamlit chrome ----- */
#MainMenu {{ visibility: hidden; }}
footer {{ visibility: hidden; }}
[data-testid="stToolbar"] {{ visibility: hidden; }}
header [data-testid="stDecoration"] {{ display: none; }}
header {{ background-color: transparent !important; }}

/* ----- Container principal: whitespace generoso, max-width ----- */
[data-testid="stAppViewContainer"] > .main > .block-container {{
    padding-top: 3rem !important;
    padding-bottom: 5rem !important;
    padding-left: 3rem !important;
    padding-right: 3rem !important;
    max-width: 1200px;
}}

/* Texto fuerte (strong, b) un pelín más oscuro */
strong, b {{
    font-weight: 600;
    color: {INK};
}}
</style>
"""


def apply_theme() -> None:
    """
    Aplica el theme visual al app de Streamlit.

    Llamar UNA SOLA vez al principio de app.py, después de
    `st.set_page_config()` y antes de cualquier otro elemento. Hace dos
    cosas:

      1. Inyecta CSS custom que estiliza todos los componentes nativos
         de Streamlit según los principios de Dieter Rams / Vitsoe.
      2. Si existe `Assets/logo.png`, lo registra con `st.logo()` para
         que aparezca arriba a la izquierda en la sidebar.

    Si el archivo del logo no existe, sigue funcionando sin él (no rompe).
    """
    if LOGO_PATH.exists():
        st.logo(str(LOGO_PATH))
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
