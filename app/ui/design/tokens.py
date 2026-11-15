"""Design tokens: the single source of every colour, size and label in the UI.

Nothing outside ``app/ui/design`` may contain a colour literal. The palette is
deliberately small: neutrals, ONE accent, ONE warning colour, ONE archived/deleted
colour, plus the five *layer identity* colours (low saturation, border/label use
only, never a large fill).

Layer chain (every page uses the same identity for the same kind of object)::

    Entity (Person) -> Link (Relationship) -> Occurrence (Event)
        -> Proof (Evidence) -> Inference (Analysis)

Epistemic marking is orthogonal to the layer::

    FACT     original recorded fact          solid border, read-only
    USER     user produced / user confirmed  user marker, editable state explicit
    DERIVED  model output                    dashed border + DERIVED + Sources

Contrast was verified against WCAG: every text / accent / layer colour is >= 4.5:1
on ``surface`` and the layer + semantic colours are pairwise distinct (the test
suite re-checks both, so editing a colour here is guarded).
"""
from dataclasses import dataclass

# --------------------------------------------------------------------- palette
COLORS = {
    # neutrals
    "base": "#12161c",
    "sidebar": "#0e1217",
    "surface": "#1a2028",
    "raised": "#242c37",
    "border": "#3a4553",
    "text": "#e6ebf1",
    "muted": "#9aa7b5",
    # the only accent / warning / archived colours
    "accent": "#5fb0d8",
    "selection": "#1f3c4d",
    "warning": "#f0b429",
    "archived": "#8793a1",
    # layer identity colours
    "entity": "#9b8fe0",
    "link": "#4fb3a5",
    "occurrence": "#a3b85a",
    "proof": "#d98770",
    "inference": "#d283b5",
}

# ---------------------------------------------------------------------- layers
@dataclass(frozen=True)
class Layer:
    key: str
    label: str          # short UI tag
    name: str           # domain object it represents
    color_token: str
    icon: str

    @property
    def color(self) -> str:
        return COLORS[self.color_token]


ENTITY = Layer("ENTITY", "人物", "Person", "entity", "person")
LINK = Layer("LINK", "关系", "Relationship", "link", "link")
OCCURRENCE = Layer("OCCURRENCE", "事件", "Event", "occurrence", "event")
PROOF = Layer("PROOF", "证据", "Evidence", "proof", "evidence")
INFERENCE = Layer("INFERENCE", "分析", "Analysis", "inference", "analysis")

# The chain order is part of the contract: detail pages follow it top to bottom.
LAYERS = (ENTITY, LINK, OCCURRENCE, PROOF, INFERENCE)
LAYER_BY_KEY = {layer.key: layer for layer in LAYERS}

# ------------------------------------------------------------------- epistemic
@dataclass(frozen=True)
class Epistemic:
    key: str
    tag: str
    caption: str
    border_style: str       # QSS border-style
    color_token: str

    @property
    def color(self) -> str:
        return COLORS[self.color_token]


FACT = Epistemic("FACT", "FACT", "原始事实 · 只读", "solid", "muted")
USER = Epistemic("USER", "USER", "用户产出 · 可编辑", "solid", "accent")
DERIVED = Epistemic("DERIVED", "DERIVED", "派生推断 · 非事实", "dashed", "inference")
EPISTEMICS = (FACT, USER, DERIVED)
EPISTEMIC_BY_KEY = {item.key: item for item in EPISTEMICS}

# ---------------------------------------------------------------------- status
# status -> (colour token, label). Only the three semantic colours are used.
STATUSES = {
    "ACTIVE": ("accent", "ACTIVE"),
    "PAUSED": ("warning", "PAUSED"),
    "ENDED": ("archived", "ENDED"),
    "ARCHIVED": ("archived", "ARCHIVED"),
    "DELETED": ("archived", "DELETED"),
    "UPCOMING": ("warning", "UPCOMING"),
    "PAST": ("muted", "PAST"),
}

# ---------------------------------------------------------------- state matrix
NORMAL, EMPTY, LOADING, ERROR, CONFLICT, ARCHIVED, DELETED, UNAVAILABLE = (
    "NORMAL", "EMPTY", "LOADING", "ERROR", "CONFLICT", "ARCHIVED", "DELETED", "UNAVAILABLE")
PAGE_STATES = (NORMAL, EMPTY, LOADING, ERROR, CONFLICT, ARCHIVED, DELETED, UNAVAILABLE)

# state -> (title, default message, colour token)
STATE_COPY = {
    EMPTY: ("暂无记录", "这里还没有记录。", "muted"),
    LOADING: ("正在加载", "正在读取数据……", "muted"),
    ERROR: ("操作未完成", "发生技术错误，请查看本地错误日志。", "warning"),
    CONFLICT: ("记录冲突", "当前记录已经被其他窗口修改，请刷新后重试。", "warning"),
    ARCHIVED: ("ARCHIVED", "该记录已归档；历史引用仍可查看。", "archived"),
    DELETED: ("DELETED", "该记录已删除；仅供只读查看。", "archived"),
    UNAVAILABLE: ("不可用", "对应服务尚未装配，此功能不可用。", "muted"),
}
SEARCH_EMPTY_MESSAGE = "没有找到匹配记录。"
CONFLICT_MESSAGE = STATE_COPY[CONFLICT][1]

# --------------------------------------------------------------------- spacing
# 4 / 8 base grid. Values are logical pixels (Qt scales them with the screen DPI).
SPACE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}
RADIUS = 4
BORDER = 1
LAYER_STRIPE = 3

# ------------------------------------------------------------------------ fonts
# Every family is listed; Qt falls back down the list and the CJK families make
# sure Chinese text never renders as tofu. All of them ship with Windows.
SANS_FAMILIES = ("Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", "sans-serif")
MONO_FAMILIES = ("Cascadia Mono", "Consolas", "Microsoft YaHei UI", "Courier New", "monospace")
# Point sizes scale with DPI; they are the only font sizes the application uses.
FONT_PT = {"body": 10, "small": 9, "title": 17, "brand": 12}
