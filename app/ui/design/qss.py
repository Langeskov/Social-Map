"""The one stylesheet. Generated from tokens, so no page carries its own colours.

Widgets pick up their look through ``objectName`` and *dynamic properties*
(``layer``, ``epistemic``, ``status``, ``state``); a property-driven rule keeps the
layer / FACT-USER-DERIVED identity identical everywhere. There is no transition,
animation or effect rule in this sheet.
"""
from app.ui.design import tokens
from app.ui.design.tokens import COLORS as C, EPISTEMICS, LAYERS, RADIUS, SPACE, STATUSES


def _layer_rules() -> str:
    rules = []
    for layer in LAYERS:
        # id + property: must out-rank the ``QFrame#layerCard`` border shorthand.
        rules.append(f'QFrame#layerCard[layer="{layer.key}"] {{ '
                     f'border-left: {tokens.LAYER_STRIPE}px solid {layer.color}; }}')
        rules.append(f'QLabel#layerBadgeText[layer="{layer.key}"] {{ color: {layer.color}; }}')
    return "\n".join(rules)


def _epistemic_rules() -> str:
    rules = []
    for item in EPISTEMICS:
        rules.append(f'QFrame#epistemicFrame[epistemic="{item.key}"] {{ '
                     f'border: {tokens.BORDER}px {item.border_style} {item.color}; }}')
        rules.append(f'QLabel#epistemicTag[epistemic="{item.key}"] {{ color: {item.color}; '
                     f'border: {tokens.BORDER}px {item.border_style} {item.color}; }}')
    return "\n".join(rules)


def _status_rules() -> str:
    rules = []
    for status, (token, _label) in STATUSES.items():
        rules.append(f'QLabel#statusPill[status="{status}"] {{ color: {C[token]}; '
                     f'border: {tokens.BORDER}px solid {C[token]}; }}')
    return "\n".join(rules)


def _state_rules() -> str:
    rules = []
    for state, (_title, _message, token) in tokens.STATE_COPY.items():
        rules.append(f'QFrame#stateNotice[state="{state}"] {{ border: {tokens.BORDER}px solid {C[token]}; '
                     f'border-left: {tokens.LAYER_STRIPE}px solid {C[token]}; }}')
        rules.append(f'QFrame#stateNotice[state="{state}"] QLabel#stateTitle {{ color: {C[token]}; }}')
    return "\n".join(rules)


def build_qss() -> str:
    sm, md, lg, xs = SPACE["sm"], SPACE["md"], SPACE["lg"], SPACE["xs"]
    pt = tokens.FONT_PT
    return f"""
QToolTip {{ background: {C['raised']}; color: {C['text']}; border: {tokens.BORDER}px solid {C['border']}; padding: {xs}px; }}
QLabel {{ background: transparent; }}
QCheckBox, QRadioButton {{ background: transparent; spacing: {sm}px; }}

/* ---- shell ---- */
QFrame#topBar {{ background: {C['sidebar']}; border-bottom: {tokens.BORDER}px solid {C['border']}; }}
QLabel#brand {{ font-size: {pt['brand']}pt; font-weight: 600; color: {C['accent']}; }}
QLabel#workspaceBadge {{ background: {C['raised']}; border: {tokens.BORDER}px solid {C['border']};
    border-radius: {RADIUS}px; padding: {xs}px {sm}px; color: {C['text']}; }}
QLabel#topStatus {{ color: {C['muted']}; }}
QFrame#sidebar {{ background: {C['sidebar']}; border-right: {tokens.BORDER}px solid {C['border']}; }}
QFrame#contextBar {{ background: {C['surface']}; border-bottom: {tokens.BORDER}px solid {C['border']}; }}
QLabel#crumb {{ color: {C['muted']}; }}
QLabel#crumbCurrent {{ color: {C['text']}; font-weight: 600; }}
QListWidget#mainNavigation {{ background: {C['sidebar']}; border: none; outline: none; }}
QListWidget#mainNavigation::item {{ padding: {md}px {sm}px; margin: {xs}px 0; border-radius: {RADIUS}px;
    border-left: {tokens.LAYER_STRIPE}px solid transparent; color: {C['muted']}; }}
QListWidget#mainNavigation::item:hover {{ background: {C['surface']}; color: {C['text']}; }}
QListWidget#mainNavigation::item:selected {{ background: {C['selection']}; color: {C['text']};
    border-left: {tokens.LAYER_STRIPE}px solid {C['accent']}; }}
QListWidget#mainNavigation::item:focus {{ border: {tokens.BORDER}px solid {C['accent']}; }}

/* ---- typography ---- */
QLabel#pageTitle {{ font-size: {pt['title']}pt; font-weight: 600; color: {C['text']}; }}
QLabel#subtitle {{ color: {C['muted']}; }}
QLabel#sectionTitle {{ font-weight: 600; color: {C['text']}; }}

/* ---- surfaces ---- */
QFrame#emptyPanel {{ background: {C['surface']}; border: {tokens.BORDER}px solid {C['border']}; border-radius: {RADIUS}px; }}
QFrame#emptyPanel QLabel {{ background: transparent; }}
QGroupBox {{ border: {tokens.BORDER}px solid {C['border']}; border-radius: {RADIUS}px; margin-top: {lg}px; padding: {md}px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: {md}px; padding: 0 {xs}px; color: {C['muted']}; }}
QGraphicsView#graphCanvas {{ background: {C['base']}; border: {tokens.BORDER}px solid {C['border']}; border-radius: {RADIUS}px; }}
QStatusBar {{ color: {C['muted']}; }}
QLineEdit, QTextEdit, QPlainTextEdit {{ background: {C['surface']}; border: {tokens.BORDER}px solid {C['border']};
    border-radius: {RADIUS}px; padding: {xs}px; selection-background-color: {C['selection']}; }}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{ border: {tokens.BORDER}px solid {C['accent']}; }}
QListWidget, QTableWidget {{ background: {C['surface']}; border: {tokens.BORDER}px solid {C['border']};
    border-radius: {RADIUS}px; alternate-background-color: {C['raised']}; }}
QHeaderView::section {{ background: {C['raised']}; color: {C['muted']}; padding: {xs}px {sm}px;
    border: none; border-right: {tokens.BORDER}px solid {C['border']}; }}
QPushButton {{ background: {C['raised']}; border: {tokens.BORDER}px solid {C['border']}; border-radius: {RADIUS}px;
    padding: {xs}px {md}px; }}
QPushButton:hover {{ border-color: {C['accent']}; }}
QPushButton:focus {{ border: {tokens.BORDER}px solid {C['accent']}; }}
QPushButton:disabled {{ color: {C['archived']}; background: {C['surface']}; }}
QScrollArea {{ border: none; background: transparent; }}
QSplitter::handle {{ background: {C['border']}; }}

/* ---- shared widgets ---- */
QFrame#layerCard {{ background: {C['surface']}; border: {tokens.BORDER}px solid {C['border']}; border-radius: {RADIUS}px; }}
{_layer_rules()}
QFrame#epistemicFrame {{ background: {C['surface']}; border-radius: {RADIUS}px; }}
QLabel#epistemicTag {{ border-radius: {RADIUS}px; padding: 0 {xs}px; }}
QLabel#epistemicCaption {{ color: {C['muted']}; }}
{_epistemic_rules()}
QLabel#statusPill {{ border-radius: {RADIUS}px; padding: 0 {xs}px; }}
{_status_rules()}
QFrame#stateNotice {{ background: {C['surface']}; border-radius: {RADIUS}px; }}
QFrame#stateNotice QLabel#stateMessage {{ color: {C['text']}; }}
{_state_rules()}
"""
