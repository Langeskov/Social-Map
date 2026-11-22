"""Structured analysis result contract (Phase 13.1).

These DTOs are the parse product of one model answer. They live **beside** the
raw answer, never instead of it: ``AnalysisResult.raw_text`` is always the exact
text the provider returned, and the parsed sections are derived in memory.

The four objects of Phase 13 stay distinct: raw output (here), parsed analysis
(here), validated references (Phase 13.2) and evaluated analysis (Phase 13).
References parsed here are structurally valid at best — nothing in this module
proves a citation exists, belongs to the run, or supports a sentence.
"""
from dataclasses import dataclass
from uuid import UUID

#: Contract version of the structured output the prompt asks for.
SCHEMA_VERSION = "1"

#: Fact-entity kinds a reference may name (AnalysisSource kinds).
REFERENCE_TYPES = ("PERSON", "RELATIONSHIP", "EVENT", "EVIDENCE")
SECTION_KEYS = ("facts", "observations", "interpretations", "uncertainties")
SECTION_TITLES = {"facts": "Facts · 事实", "observations": "Observations · 观察",
                  "interpretations": "Interpretations · 推断",
                  "uncertainties": "Uncertainties · 不确定"}

#: Parse outcomes. Only the first two are structured; the rest degrade to text.
PARSED_JSON = "PARSED_JSON"
PARSED_JSON_FRAGMENT = "PARSED_JSON_FRAGMENT"
LEGACY_MARKDOWN = "LEGACY_MARKDOWN"
UNSTRUCTURED_TEXT = "UNSTRUCTURED_TEXT"
EMPTY = "EMPTY"
TOO_LARGE = "TOO_LARGE"
PARSE_STATUSES = (PARSED_JSON, PARSED_JSON_FRAGMENT, LEGACY_MARKDOWN,
                  UNSTRUCTURED_TEXT, EMPTY, TOO_LARGE)
STRUCTURED_STATUSES = (PARSED_JSON, PARSED_JSON_FRAGMENT)

#: Upper bound on the stored answer a parser will process (raw text is kept as-is).
MAX_RESULT_CHARS = 200_000


@dataclass(frozen=True)
class AnalysisReference:
    """One structural citation parsed from the model answer (not yet validated)."""

    entity_type: str
    entity_id: UUID

    def __post_init__(self):
        if self.entity_type not in REFERENCE_TYPES:
            raise ValueError(f"未知引用类型：{self.entity_type!r}")
        if not isinstance(self.entity_id, UUID):
            raise ValueError("引用 id 必须是 UUID")

    def to_mapping(self) -> dict:
        return {"type": self.entity_type, "id": str(self.entity_id)}


@dataclass(frozen=True)
class AnalysisEntry:
    """One item of one section: content + structural references + wording notes."""

    content: str
    references: tuple[AnalysisReference, ...] = ()
    confidence_description: str = ""
    notes: str = ""

    def to_mapping(self) -> dict:
        return {"content": self.content,
                "references": [item.to_mapping() for item in self.references],
                "confidence_description": self.confidence_description,
                "notes": self.notes}

    def identity(self) -> tuple:
        """Equality key used for duplicate reporting (never for merging)."""
        return (self.content, tuple((r.entity_type, str(r.entity_id)) for r in self.references),
                self.confidence_description, self.notes)


@dataclass(frozen=True)
class AnalysisResult:
    """Parsed view over one raw model answer."""

    raw_text: str
    schema_version: str
    parse_status: str
    facts: tuple[AnalysisEntry, ...] = ()
    observations: tuple[AnalysisEntry, ...] = ()
    interpretations: tuple[AnalysisEntry, ...] = ()
    uncertainties: tuple[AnalysisEntry, ...] = ()
    unparsed_content: str = ""
    warnings: tuple[str, ...] = ()
    #: Which of the four contract sections were actually present in the answer
    #: (JSON keys, or non-empty legacy headings). Empty for unparseable answers.
    sections_present: tuple[str, ...] = ()

    def __post_init__(self):
        if self.parse_status not in PARSE_STATUSES:
            raise ValueError(f"未知解析状态：{self.parse_status!r}")
        for name in ("facts", "observations", "interpretations", "uncertainties"):
            object.__setattr__(self, name, tuple(getattr(self, name) or ()))

    @property
    def structured(self) -> bool:
        return self.parse_status in STRUCTURED_STATUSES

    def section(self, key: str) -> tuple[AnalysisEntry, ...]:
        if key not in SECTION_KEYS:
            raise KeyError(key)
        return getattr(self, key)

    def entry_count(self) -> int:
        return sum(len(self.section(key)) for key in SECTION_KEYS)

    def display_text(self, key: str) -> str:
        """Readable rendering of one section (references marked as unvalidated)."""
        blocks = []
        for entry in self.section(key):
            lines = [entry.content]
            if entry.references:
                lines.append("  ↳ 引用（未校验）：" + "、".join(
                    f"{item.entity_type} {item.entity_id}" for item in entry.references))
            if entry.confidence_description:
                lines.append(f"  ↳ 不确定说明：{entry.confidence_description}")
            if entry.notes:
                lines.append(f"  ↳ 备注：{entry.notes}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)
