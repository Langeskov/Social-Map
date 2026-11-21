"""Load and validate versioned evaluation datasets.

Pure file parsing: no database, no network, no Qt, no LLM. Validation is
fail-closed and collects *every* issue before raising `DatasetError`, so a broken
dataset is diagnosable in one pass. The loader also enforces the provenance rule
of Phase 13.0: only explicitly synthetic fixtures are accepted, so real personal
data can never sneak into the evaluation corpus by accident.
"""
import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from uuid import UUID

from app.domain.entities.analysis import ANALYSIS_TYPE_FOR_TARGET
from app.domain.entities.reference_validation import CITABLE_TYPES, STATUSES as REFERENCE_STATUSES
from app.evaluation.models import (ANALYSIS_TYPES, BULK_LIMIT, DATASET_VERSION,
                                   EXPECTED_ERROR_CONTEXT_LIMIT, METRIC_DEFINITIONS,
                                   SCHEMA_VERSION, SOURCE_TYPES, TIME_SCOPES,
                                   TARGET_TYPES, AliasSpec, BulkEventSpec,
                                   BulkEvidenceSpec, ContextRef, DatasetError,
                                   DatasetIssue, EvaluationDataset,
                                   EvaluationScenario, EventSpec, EvidenceSpec,
                                   ExpectedContext, ExpectedFact,
                                   ExpectedReferenceOutcome, ExpectedUncertainty,
                                   ForbiddenClaim, FixtureSpec, HistorySpec,
                                   HumanReviewItem, MediaSpec, MemorySpec,
                                   ParticipantSpec, PersonSpec, RelationshipSpec,
                                   RequestSpec, SampleOutput, SourceRef)

MANIFEST_NAME = "dataset.json"
#: ``<<TYPE:key>>`` placeholders in sample outputs, resolved at run time.
PLACEHOLDER_PATTERN = re.compile(r"<<(PERSON|RELATIONSHIP|EVENT|EVIDENCE):([^>]+)>>")


# ------------------------------------------------------------------ small tools
def _issue(issues, path, message):
    issues.append(DatasetIssue(path, message))


def _mapping(raw, path, issues):
    if not isinstance(raw, dict):
        _issue(issues, path, "必须是 JSON 对象。")
        return None
    return raw


def _check_keys(raw, allowed, path, issues):
    for key in raw:
        if key not in allowed:
            _issue(issues, f"{path}.{key}", "未声明的字段（拒绝以防拼写错误混入）。")


def _text(raw, key, path, issues, *, required=True, default=""):
    value = raw.get(key, None)
    if value is None:
        if required:
            _issue(issues, f"{path}.{key}", "缺少必填字段。")
        return default
    if not isinstance(value, str):
        _issue(issues, f"{path}.{key}", "必须是字符串。")
        return default
    if required and not value.strip():
        _issue(issues, f"{path}.{key}", "不能为空。")
    return value


def _boolean(raw, key, path, issues, *, default=False):
    value = raw.get(key, None)
    if value is None:
        return default
    if not isinstance(value, bool):
        _issue(issues, f"{path}.{key}", "必须是布尔值。")
        return default
    return value


def _integer(raw, key, path, issues, *, default=0, minimum=None, maximum=None):
    value = raw.get(key, None)
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        _issue(issues, f"{path}.{key}", "必须是整数。")
        return default
    if minimum is not None and value < minimum:
        _issue(issues, f"{path}.{key}", f"不能小于 {minimum}。")
    if maximum is not None and value > maximum:
        _issue(issues, f"{path}.{key}", f"不能大于 {maximum}。")
    return value


def _sequence(raw, key, path, issues, *, required=False):
    value = raw.get(key, None)
    if value is None:
        if required:
            _issue(issues, f"{path}.{key}", "缺少必填字段。")
        return []
    if not isinstance(value, list):
        _issue(issues, f"{path}.{key}", "必须是数组。")
        return []
    return value


def _datetime(raw, key, path, issues, *, required=False):
    value = raw.get(key, None)
    if value is None:
        if required:
            _issue(issues, f"{path}.{key}", "缺少必填字段。")
        return None
    if not isinstance(value, str):
        _issue(issues, f"{path}.{key}", "必须是 ISO-8601 字符串。")
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _issue(issues, f"{path}.{key}", f"无法解析的时间：{value!r}。")
        return None
    if parsed.tzinfo is None:
        _issue(issues, f"{path}.{key}", "时间必须带时区（例如 2026-06-15T12:00:00+00:00）。")
        return None
    return parsed


def _date(raw, key, path, issues, *, required=False):
    value = raw.get(key, None)
    if value is None:
        if required:
            _issue(issues, f"{path}.{key}", "缺少必填字段。")
        return None
    if not isinstance(value, str):
        _issue(issues, f"{path}.{key}", "必须是 YYYY-MM-DD 字符串。")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        _issue(issues, f"{path}.{key}", f"无法解析的日期：{value!r}。")
        return None


def _enum(raw, key, path, issues, *, allowed, required=True, default=""):
    value = _text(raw, key, path, issues, required=required, default=default)
    if value and value not in allowed:
        _issue(issues, f"{path}.{key}", f"必须是 {', '.join(allowed)} 之一，得到 {value!r}。")
    return value


def _strings(raw, key, path, issues):
    out = []
    for index, item in enumerate(_sequence(raw, key, path, issues)):
        if not isinstance(item, str) or not item.strip():
            _issue(issues, f"{path}.{key}[{index}]", "必须是非空字符串。")
            continue
        out.append(item)
    return tuple(out)


def _ref(raw, path, issues, *, cls):
    data = _mapping(raw, path, issues)
    if data is None:
        return None
    _check_keys(data, {"type", "key"}, path, issues)
    ref_type = _enum(data, "type", path, issues, allowed=SOURCE_TYPES if cls is SourceRef
                     else SOURCE_TYPES + ("MEMORY",))
    key = _text(data, "key", path, issues)
    if not ref_type or not key:
        return None
    try:
        return cls(ref_type, key)
    except ValueError as exc:
        _issue(issues, path, str(exc))
        return None


# ------------------------------------------------------------ record parsers
def _parse_alias(raw, path, issues):
    data = _mapping(raw, path, issues)
    if data is None:
        return None
    _check_keys(data, {"alias", "alias_type", "note"}, path, issues)
    return AliasSpec(alias=_text(data, "alias", path, issues),
                     alias_type=_text(data, "alias_type", path, issues, required=False,
                                      default="OTHER") or "OTHER",
                     note=_text(data, "note", path, issues, required=False))


def _parse_person(raw, path, issues):
    data = _mapping(raw, path, issues)
    if data is None:
        return None
    _check_keys(data, {"key", "workspace", "display_name", "real_name", "gender",
                       "occupation", "organization", "location", "birthday",
                       "notes", "aliases"}, path, issues)
    aliases = tuple(item for index, item in enumerate(
        _parse_alias(value, f"{path}.aliases[{index}]", issues)
        for index, value in enumerate(_sequence(data, "aliases", path, issues))) if item)
    return PersonSpec(
        key=_text(data, "key", path, issues),
        workspace=_text(data, "workspace", path, issues),
        display_name=_text(data, "display_name", path, issues),
        real_name=_text(data, "real_name", path, issues, required=False),
        gender=_text(data, "gender", path, issues, required=False),
        occupation=_text(data, "occupation", path, issues, required=False),
        organization=_text(data, "organization", path, issues, required=False),
        location=_text(data, "location", path, issues, required=False),
        birthday=_date(data, "birthday", path, issues),
        notes=_text(data, "notes", path, issues, required=False),
        aliases=aliases)


def _parse_history(raw, path, issues):
    data = _mapping(raw, path, issues)
    if data is None:
        return None
    _check_keys(data, {"state", "effective_at", "note"}, path, issues)
    return HistorySpec(state=_enum(data, "state", path, issues,
                                   allowed=("ACTIVE", "PAUSED", "ENDED")),
                       effective_at=_datetime(data, "effective_at", path, issues, required=True)
                       or datetime.fromtimestamp(0, tz=None).astimezone(),
                       note=_text(data, "note", path, issues, required=False))


def _parse_relationship(raw, path, issues):
    data = _mapping(raw, path, issues)
    if data is None:
        return None
    _check_keys(data, {"key", "workspace", "person_a", "person_b", "relationship_type",
                       "directionality", "initial_state", "effective_at", "note",
                       "history"}, path, issues)
    history = tuple(item for index, item in enumerate(
        _parse_history(value, f"{path}.history[{index}]", issues)
        for index, value in enumerate(_sequence(data, "history", path, issues))) if item)
    return RelationshipSpec(
        key=_text(data, "key", path, issues),
        workspace=_text(data, "workspace", path, issues),
        person_a=_text(data, "person_a", path, issues),
        person_b=_text(data, "person_b", path, issues),
        relationship_type=_text(data, "relationship_type", path, issues),
        directionality=_enum(data, "directionality", path, issues, required=False,
                             allowed=("UNDIRECTED", "DIRECTED"), default="UNDIRECTED")
        or "UNDIRECTED",
        initial_state=_enum(data, "initial_state", path, issues, required=False,
                            allowed=("ACTIVE", "PAUSED", "ENDED"), default="ACTIVE") or "ACTIVE",
        effective_at=_datetime(data, "effective_at", path, issues),
        note=_text(data, "note", path, issues, required=False),
        history=history)


def _parse_participant(raw, path, issues):
    data = _mapping(raw, path, issues)
    if data is None:
        return None
    _check_keys(data, {"person", "role", "note"}, path, issues)
    return ParticipantSpec(person=_text(data, "person", path, issues),
                           role=_text(data, "role", path, issues, required=False),
                           note=_text(data, "note", path, issues, required=False))


def _parse_event(raw, path, issues):
    data = _mapping(raw, path, issues)
    if data is None:
        return None
    _check_keys(data, {"key", "workspace", "title", "event_type", "record_kind",
                       "occurred_on", "started_at", "ended_at", "location",
                       "description", "participants"}, path, issues)
    participants = tuple(item for index, item in enumerate(
        _parse_participant(value, f"{path}.participants[{index}]", issues)
        for index, value in enumerate(_sequence(data, "participants", path, issues))) if item)
    return EventSpec(
        key=_text(data, "key", path, issues),
        workspace=_text(data, "workspace", path, issues),
        title=_text(data, "title", path, issues),
        event_type=_text(data, "event_type", path, issues),
        record_kind=_enum(data, "record_kind", path, issues, required=False,
                          allowed=("FACT", "OBSERVATION"), default="FACT") or "FACT",
        occurred_on=_date(data, "occurred_on", path, issues),
        started_at=_datetime(data, "started_at", path, issues),
        ended_at=_datetime(data, "ended_at", path, issues),
        location=_text(data, "location", path, issues, required=False),
        description=_text(data, "description", path, issues, required=False),
        participants=participants)


def _parse_media(raw, path, issues):
    data = _mapping(raw, path, issues)
    if data is None:
        return None
    _check_keys(data, {"key", "filename", "content"}, path, issues)
    return MediaSpec(key=_text(data, "key", path, issues),
                     filename=_text(data, "filename", path, issues),
                     content=_text(data, "content", path, issues, required=False,
                                   default="synthetic evaluation attachment payload"))


def _parse_evidence(raw, path, issues):
    data = _mapping(raw, path, issues)
    if data is None:
        return None
    _check_keys(data, {"key", "workspace", "event", "kind", "title", "source_type",
                       "content_text", "annotation", "source_label",
                       "source_reference", "captured_at", "media"}, path, issues)
    media = tuple(item for index, item in enumerate(
        _parse_media(value, f"{path}.media[{index}]", issues)
        for index, value in enumerate(_sequence(data, "media", path, issues))) if item)
    return EvidenceSpec(
        key=_text(data, "key", path, issues),
        workspace=_text(data, "workspace", path, issues),
        event=_text(data, "event", path, issues),
        kind=_enum(data, "kind", path, issues,
                   allowed=("TEXT", "IMAGE", "FILE", "LINK", "MANUAL", "OTHER")),
        title=_text(data, "title", path, issues),
        source_type=_enum(data, "source_type", path, issues, required=False,
                          default="MANUAL",
                          allowed=("CHAT", "EMAIL", "DOCUMENT", "IMAGE", "FILE",
                                   "WEB", "MANUAL", "OTHER")) or "MANUAL",
        content_text=_text(data, "content_text", path, issues, required=False),
        annotation=_text(data, "annotation", path, issues, required=False),
        source_label=_text(data, "source_label", path, issues, required=False),
        source_reference=_text(data, "source_reference", path, issues, required=False),
        captured_at=_datetime(data, "captured_at", path, issues),
        media=media)


def _parse_memory(raw, path, issues):
    data = _mapping(raw, path, issues)
    if data is None:
        return None
    _check_keys(data, {"key", "workspace", "content", "kind", "person",
                       "valid_from", "valid_until"}, path, issues)
    return MemorySpec(
        key=_text(data, "key", path, issues),
        workspace=_text(data, "workspace", path, issues),
        content=_text(data, "content", path, issues),
        kind=_enum(data, "kind", path, issues, required=False,
                   allowed=("FACT", "PREFERENCE", "CONTEXT"), default="CONTEXT") or "CONTEXT",
        person=_text(data, "person", path, issues, required=False),
        valid_from=_datetime(data, "valid_from", path, issues),
        valid_until=_datetime(data, "valid_until", path, issues))


def _parse_bulk_events(raw, path, issues):
    data = _mapping(raw, path, issues)
    if data is None:
        return None
    _check_keys(data, {"key_prefix", "title_prefix", "workspace", "person", "count",
                       "description_template", "event_type", "record_kind",
                       "occurred_on_base", "occurred_on_step_days"}, path, issues)
    return BulkEventSpec(
        key_prefix=_text(data, "key_prefix", path, issues),
        title_prefix=_text(data, "title_prefix", path, issues),
        workspace=_text(data, "workspace", path, issues),
        person=_text(data, "person", path, issues),
        count=_integer(data, "count", path, issues, default=0, minimum=1, maximum=BULK_LIMIT),
        description_template=_text(data, "description_template", path, issues),
        event_type=_text(data, "event_type", path, issues, required=False, default="Meeting")
        or "Meeting",
        record_kind=_enum(data, "record_kind", path, issues, required=False,
                          allowed=("FACT", "OBSERVATION"), default="FACT") or "FACT",
        occurred_on_base=_date(data, "occurred_on_base", path, issues),
        occurred_on_step_days=_integer(data, "occurred_on_step_days", path, issues,
                                       default=-1))


def _parse_bulk_evidence(raw, path, issues):
    data = _mapping(raw, path, issues)
    if data is None:
        return None
    _check_keys(data, {"key_prefix", "title_prefix", "workspace", "event_key_prefix",
                       "count", "content_template", "kind", "source_type"}, path, issues)
    return BulkEvidenceSpec(
        key_prefix=_text(data, "key_prefix", path, issues),
        title_prefix=_text(data, "title_prefix", path, issues),
        workspace=_text(data, "workspace", path, issues),
        event_key_prefix=_text(data, "event_key_prefix", path, issues),
        count=_integer(data, "count", path, issues, default=0, minimum=1, maximum=BULK_LIMIT),
        content_template=_text(data, "content_template", path, issues),
        kind=_enum(data, "kind", path, issues, required=False,
                   allowed=("TEXT", "IMAGE", "FILE", "LINK", "MANUAL", "OTHER"),
                   default="TEXT") or "TEXT",
        source_type=_text(data, "source_type", path, issues, required=False,
                          default="DOCUMENT") or "DOCUMENT")


def _parse_fixture(raw, path, issues):
    data = _mapping(raw, path, issues)
    if data is None:
        return None
    _check_keys(data, {"schema_version", "fixture_id", "fixture_version", "description",
                       "synthetic", "workspaces", "people", "relationships", "events",
                       "evidence", "memories", "bulk_events", "bulk_evidence"}, path, issues)
    _schema_version(data, path, issues)
    if _boolean(data, "synthetic", path, issues, default=False) is not True:
        _issue(issues, f"{path}.synthetic",
               "必须显式声明 synthetic=true；评估数据集只允许完全虚构的数据。")
    workspaces = []
    for index, item in enumerate(_sequence(data, "workspaces", path, issues, required=True)):
        entry = _mapping(item, f"{path}.workspaces[{index}]", issues)
        if entry is None:
            continue
        _check_keys(entry, {"key", "name"}, f"{path}.workspaces[{index}]", issues)
        workspaces.append((_text(entry, "key", f"{path}.workspaces[{index}]", issues),
                           _text(entry, "name", f"{path}.workspaces[{index}]", issues)))

    def collect(key, parser):
        return tuple(item for index, item in enumerate(
            parser(value, f"{path}.{key}[{index}]", issues)
            for index, value in enumerate(_sequence(data, key, path, issues))) if item)

    return FixtureSpec(
        fixture_id=_text(data, "fixture_id", path, issues),
        fixture_version=_text(data, "fixture_version", path, issues),
        description=_text(data, "description", path, issues),
        workspaces=tuple(workspaces),
        people=collect("people", _parse_person),
        relationships=collect("relationships", _parse_relationship),
        events=collect("events", _parse_event),
        evidence=collect("evidence", _parse_evidence),
        memories=collect("memories", _parse_memory),
        bulk_events=collect("bulk_events", _parse_bulk_events),
        bulk_evidence=collect("bulk_evidence", _parse_bulk_evidence),
        synthetic=_boolean(data, "synthetic", path, issues, default=False),
        source_path=path)


def _schema_version(data, path, issues):
    version = _text(data, "schema_version", path, issues)
    if version and version != SCHEMA_VERSION:
        _issue(issues, f"{path}.schema_version",
               f"不支持的 schema_version={version!r}（本加载器支持 {SCHEMA_VERSION}）。")
    return version


def _parse_request(raw, path, issues):
    data = _mapping(raw, path, issues)
    if data is None:
        return None
    _check_keys(data, {"target_type", "target_key", "analysis_type", "as_of",
                       "time_scope", "include", "selected_events",
                       "selected_evidence", "selected_memories", "custom_start",
                       "custom_end", "max_context_tokens"}, path, issues)
    include = _mapping(data.get("include", {}), f"{path}.include", issues) or {}
    _check_keys(include, {"relationships", "history", "observations", "evidence",
                          "memories"}, f"{path}.include", issues)
    custom_start = _datetime(data, "custom_start", path, issues)
    custom_end = _datetime(data, "custom_end", path, issues)
    if custom_start is not None and custom_end is not None and custom_start > custom_end:
        _issue(issues, f"{path}.custom_start", "不能晚于 custom_end。")
    return RequestSpec(
        target_type=_enum(data, "target_type", path, issues, allowed=TARGET_TYPES),
        target_key=_text(data, "target_key", path, issues),
        analysis_type=_enum(data, "analysis_type", path, issues, allowed=ANALYSIS_TYPES),
        as_of=_datetime(data, "as_of", path, issues, required=True)
        or datetime.fromtimestamp(0, tz=None).astimezone(),
        time_scope=_enum(data, "time_scope", path, issues, required=False,
                         default="LAST_30_DAYS", allowed=TIME_SCOPES) or "LAST_30_DAYS",
        include_relationships=_boolean(include, "relationships", f"{path}.include",
                                       issues, default=True),
        include_history=_boolean(include, "history", f"{path}.include", issues, default=True),
        include_observations=_boolean(include, "observations", f"{path}.include",
                                      issues, default=True),
        include_evidence=_boolean(include, "evidence", f"{path}.include", issues),
        include_memories=_boolean(include, "memories", f"{path}.include", issues),
        selected_event_keys=_strings(data, "selected_events", path, issues),
        selected_evidence_keys=_strings(data, "selected_evidence", path, issues),
        selected_memory_keys=_strings(data, "selected_memories", path, issues),
        custom_start=custom_start,
        custom_end=custom_end,
        max_context_tokens=_integer(data, "max_context_tokens", path, issues,
                                    default=24000, minimum=1))


def _parse_expected_context(raw, path, issues):
    data = _mapping(raw, path, issues) if raw is not None else {}
    if data is None:
        return ExpectedContext()
    _check_keys(data, {"included", "excluded", "required_text", "forbidden_text",
                       "expected_error"}, path, issues)

    def refs(key):
        out = []
        for index, value in enumerate(_sequence(data, key, path, issues)):
            item = _ref(value, f"{path}.{key}[{index}]", issues, cls=ContextRef)
            if item is not None:
                out.append(item)
        return tuple(out)

    expected_error = data.get("expected_error")
    if expected_error is not None and expected_error != EXPECTED_ERROR_CONTEXT_LIMIT:
        _issue(issues, f"{path}.expected_error",
               f"只支持 {EXPECTED_ERROR_CONTEXT_LIMIT!r} 或 null，得到 {expected_error!r}。")
    return ExpectedContext(included=refs("included"), excluded=refs("excluded"),
                           required_text=_strings(data, "required_text", path, issues),
                           forbidden_text=_strings(data, "forbidden_text", path, issues),
                           expected_error=expected_error)


def _parse_sample_output(raw, path, issues):
    if raw is None:
        return None
    data = _mapping(raw, path, issues)
    if data is None:
        return None
    _check_keys(data, {"label", "content", "expected_references",
                       "expected_missing_fact_references"}, path, issues)
    expected = []
    for index, value in enumerate(_sequence(data, "expected_references", path, issues)):
        item_path = f"{path}.expected_references[{index}]"
        entry = _mapping(value, item_path, issues)
        if entry is None:
            continue
        _check_keys(entry, {"ref_type", "expected_status", "source", "literal_id"},
                    item_path, issues)
        ref_type = _enum(entry, "ref_type", item_path, issues, allowed=CITABLE_TYPES)
        expected_status = _enum(entry, "expected_status", item_path, issues,
                                allowed=REFERENCE_STATUSES)
        source = None
        literal_id = None
        if "source" in entry:
            source = _ref(entry.get("source"), f"{item_path}.source", issues, cls=SourceRef)
        elif "literal_id" in entry:
            raw_literal = entry.get("literal_id")
            try:
                literal_id = UUID(str(raw_literal))
            except (ValueError, TypeError):
                _issue(issues, f"{item_path}.literal_id", "必须是合法 UUID 字符串。")
        else:
            _issue(issues, item_path, "必须提供 source 或 literal_id。")
        if ref_type and expected_status and (source or literal_id):
            expected.append(ExpectedReferenceOutcome(
                ref_type=ref_type, expected_status=expected_status,
                source=source, literal_id=literal_id))
    return SampleOutput(
        label=_text(data, "label", path, issues, required=False, default="样例输出")
        or "样例输出",
        content=_text(data, "content", path, issues),
        expected_references=tuple(expected),
        expected_missing_fact_references=_integer(
            data, "expected_missing_fact_references", path, issues, default=0, minimum=0))


def _parse_scenario(raw, path, issues):
    data = _mapping(raw, path, issues)
    if data is None:
        return None
    _check_keys(data, {"schema_version", "dataset_version", "scenario_id",
                       "scenario_version", "title", "description", "fixture",
                       "request", "expected_facts", "required_sources",
                       "forbidden_claims", "expected_uncertainties", "human_review",
                       "evaluation_dimensions", "expected_context", "sample_output",
                       "notes"}, path, issues)
    _schema_version(data, path, issues)

    def ref_list(key, cls):
        out = []
        for index, value in enumerate(_sequence(data, key, path, issues)):
            item = _ref(value, f"{path}.{key}[{index}]", issues, cls=cls)
            if item is not None:
                out.append(item)
        return tuple(out)

    expected_facts = []
    for index, value in enumerate(_sequence(data, "expected_facts", path, issues)):
        entry = _mapping(value, f"{path}.expected_facts[{index}]", issues)
        if entry is None:
            continue
        item_path = f"{path}.expected_facts[{index}]"
        _check_keys(entry, {"fact_id", "source", "statement", "note"}, item_path, issues)
        source = _ref(entry.get("source"), f"{item_path}.source", issues, cls=SourceRef)
        if source is None:
            _issue(issues, f"{item_path}.source", "缺少合法的来源引用 {type, key}。")
            continue
        expected_facts.append(ExpectedFact(
            fact_id=_text(entry, "fact_id", item_path, issues),
            source=source,
            statement=_text(entry, "statement", item_path, issues),
            note=_text(entry, "note", item_path, issues, required=False)))

    def claims(key, cls):
        out = []
        for index, value in enumerate(_sequence(data, key, path, issues)):
            entry = _mapping(value, f"{path}.{key}[{index}]", issues)
            if entry is None:
                continue
            item_path = f"{path}.{key}[{index}]"
            if cls is ForbiddenClaim:
                _check_keys(entry, {"claim_id", "description", "rationale"}, item_path, issues)
                out.append(ForbiddenClaim(
                    claim_id=_text(entry, "claim_id", item_path, issues),
                    description=_text(entry, "description", item_path, issues),
                    rationale=_text(entry, "rationale", item_path, issues, required=False)))
            elif cls is ExpectedUncertainty:
                _check_keys(entry, {"uncertainty_id", "description", "reason"}, item_path, issues)
                out.append(ExpectedUncertainty(
                    uncertainty_id=_text(entry, "uncertainty_id", item_path, issues),
                    description=_text(entry, "description", item_path, issues),
                    reason=_text(entry, "reason", item_path, issues, required=False)))
            else:
                _check_keys(entry, {"review_id", "question", "rationale"}, item_path, issues)
                out.append(HumanReviewItem(
                    review_id=_text(entry, "review_id", item_path, issues),
                    question=_text(entry, "question", item_path, issues),
                    rationale=_text(entry, "rationale", item_path, issues, required=False)))
        return tuple(out)

    request = _parse_request(data.get("request"), f"{path}.request", issues)
    if request is None:
        request = RequestSpec(target_type="PERSON", target_key="", analysis_type="PERSON_PROFILE",
                              as_of=datetime.fromtimestamp(0, tz=None).astimezone())
    return EvaluationScenario(
        scenario_id=_text(data, "scenario_id", path, issues),
        scenario_version=_text(data, "scenario_version", path, issues),
        dataset_version=_text(data, "dataset_version", path, issues),
        title=_text(data, "title", path, issues),
        description=_text(data, "description", path, issues),
        fixture_id=_text(data, "fixture", path, issues),
        request=request,
        expected_facts=tuple(expected_facts),
        required_sources=ref_list("required_sources", SourceRef),
        forbidden_claims=claims("forbidden_claims", ForbiddenClaim),
        expected_uncertainties=claims("expected_uncertainties", ExpectedUncertainty),
        human_review=claims("human_review", HumanReviewItem),
        evaluation_dimensions=_strings(data, "evaluation_dimensions", path, issues),
        expected_context=_parse_expected_context(data.get("expected_context"),
                                                 f"{path}.expected_context", issues),
        sample_output=_parse_sample_output(data.get("sample_output"),
                                           f"{path}.sample_output", issues),
        notes=_text(data, "notes", path, issues, required=False),
        source_path=path)


# ------------------------------------------------------------ cross references
def _validate_fixture(fixture, issues):
    path = fixture.source_path
    workspace_keys = {key for key, _ in fixture.workspaces}
    if not workspace_keys:
        _issue(issues, f"{path}.workspaces", "至少需要一个 Workspace。")
    if len(workspace_keys) != len(fixture.workspaces):
        _issue(issues, f"{path}.workspaces", "workspace key 必须唯一。")

    def require_workspace(owner_path, key):
        if key and key not in workspace_keys:
            _issue(issues, owner_path, f"workspace {key!r} 未在 workspaces 中声明。")

    people_keys = {}
    for person in fixture.people:
        if person.key in people_keys:
            _issue(issues, f"{path}.people", f"人物 key 重复：{person.key!r}。")
        people_keys[person.key] = person
        require_workspace(f"{path}.people[{person.key}]", person.workspace)
    for relationship in fixture.relationships:
        require_workspace(f"{path}.relationships[{relationship.key}]", relationship.workspace)
        for side in (relationship.person_a, relationship.person_b):
            person = people_keys.get(side)
            if person is None:
                _issue(issues, f"{path}.relationships[{relationship.key}]",
                       f"人物 key 不存在：{side!r}。")
            elif person.workspace != relationship.workspace:
                _issue(issues, f"{path}.relationships[{relationship.key}]",
                       f"端点 {side!r} 与关系不在同一 Workspace。")
        if relationship.person_a == relationship.person_b:
            _issue(issues, f"{path}.relationships[{relationship.key}]", "关系端点不能是同一人物。")
    events = fixture.all_events()
    event_keys = {}
    for event in events:
        if event.key in event_keys:
            _issue(issues, f"{path}.events", f"事件 key 重复：{event.key!r}。")
        event_keys[event.key] = event
        require_workspace(f"{path}.events[{event.key}]", event.workspace)
        if event.occurred_on is None and event.started_at is None:
            _issue(issues, f"{path}.events[{event.key}]", "需要 occurred_on 或 started_at。")
        for participant in event.participants:
            person = people_keys.get(participant.person)
            if person is None:
                _issue(issues, f"{path}.events[{event.key}]",
                       f"参与人 key 不存在：{participant.person!r}。")
            elif person.workspace != event.workspace:
                _issue(issues, f"{path}.events[{event.key}]",
                       f"参与人 {participant.person!r} 与事件不在同一 Workspace。")
    evidence_keys = set()
    for item in fixture.all_evidence():
        if item.key in evidence_keys:
            _issue(issues, f"{path}.evidence", f"证据 key 重复：{item.key!r}。")
        evidence_keys.add(item.key)
        require_workspace(f"{path}.evidence[{item.key}]", item.workspace)
        event = event_keys.get(item.event)
        if event is None:
            _issue(issues, f"{path}.evidence[{item.key}]", f"事件 key 不存在：{item.event!r}。")
        elif event.workspace != item.workspace:
            _issue(issues, f"{path}.evidence[{item.key}]", "证据与其事件不在同一 Workspace。")
    memory_keys = set()
    for memory in fixture.memories:
        if memory.key in memory_keys:
            _issue(issues, f"{path}.memories", f"Memory key 重复：{memory.key!r}。")
        memory_keys.add(memory.key)
        require_workspace(f"{path}.memories[{memory.key}]", memory.workspace)
        person = people_keys.get(memory.person) if memory.person else None
        if memory.person and person is None:
            _issue(issues, f"{path}.memories[{memory.key}]",
                   f"人物 key 不存在：{memory.person!r}。")
        elif person is not None and person.workspace != memory.workspace:
            _issue(issues, f"{path}.memories[{memory.key}]", "Memory 与其人物不在同一 Workspace。")
        if memory.valid_from and memory.valid_until and memory.valid_until < memory.valid_from:
            _issue(issues, f"{path}.memories[{memory.key}]", "valid_until 不能早于 valid_from。")
    bulk_prefixes = {bulk.key_prefix for bulk in fixture.bulk_events}
    for bulk in fixture.bulk_evidence:
        if bulk.event_key_prefix not in bulk_prefixes:
            _issue(issues, f"{path}.bulk_evidence",
                   f"event_key_prefix {bulk.event_key_prefix!r} 没有对应的 bulk_events。")
        for bulk_events in fixture.bulk_events:
            if bulk_events.key_prefix == bulk.event_key_prefix and bulk.count > bulk_events.count:
                _issue(issues, f"{path}.bulk_evidence", "count 超过对应 bulk_events 的数量。")


def _validate_scenario(scenario, fixture, issues):
    path = scenario.source_path
    if scenario.dataset_version != DATASET_VERSION:
        _issue(issues, f"{path}.dataset_version",
               f"场景声明 dataset_version={scenario.dataset_version!r}，"
               f"与数据集 {DATASET_VERSION!r} 不一致。")
    request = scenario.request
    if request.time_scope == "CUSTOM" and request.custom_start is None and request.custom_end is None:
        _issue(issues, f"{path}.request", "CUSTOM 时间范围必须提供 custom_start 或 custom_end。")
    if request.analysis_type != ANALYSIS_TYPE_FOR_TARGET.get(request.target_type):
        _issue(issues, f"{path}.request.analysis_type",
               f"analysis_type 与 target_type={request.target_type!r} 不匹配。")

    def known(kind, key):
        try:
            fixture.workspace_key_of(kind, key)
            return True
        except KeyError:
            return False

    if not known(request.target_type, request.target_key):
        _issue(issues, f"{path}.request.target_key",
               f"目标 {request.target_type}:{request.target_key!r} 不在 fixture 中。")
    for key in request.selected_event_keys:
        if not known("EVENT", key):
            _issue(issues, f"{path}.request.selected_events", f"事件 key 不存在：{key!r}。")
    for key in request.selected_evidence_keys:
        if not known("EVIDENCE", key):
            _issue(issues, f"{path}.request.selected_evidence", f"证据 key 不存在：{key!r}。")
    for key in request.selected_memory_keys:
        if not known("MEMORY", key):
            _issue(issues, f"{path}.request.selected_memories", f"Memory key 不存在：{key!r}。")

    fact_ids = set()
    for fact in scenario.expected_facts:
        if fact.fact_id in fact_ids:
            _issue(issues, f"{path}.expected_facts", f"fact_id 重复：{fact.fact_id!r}。")
        fact_ids.add(fact.fact_id)
        if not known(fact.source.type, fact.source.key):
            _issue(issues, f"{path}.expected_facts[{fact.fact_id}]",
                   f"来源 {fact.source.token!r} 不在 fixture 中。")
    for source in scenario.required_sources:
        if not known(source.type, source.key):
            _issue(issues, f"{path}.required_sources", f"来源 {source.token!r} 不在 fixture 中。")
    for collection, attribute in ((scenario.forbidden_claims, "claim_id"),
                                  (scenario.expected_uncertainties, "uncertainty_id"),
                                  (scenario.human_review, "review_id")):
        seen = set()
        for item in collection:
            identifier = getattr(item, attribute)
            if identifier in seen:
                _issue(issues, path, f"{attribute} 重复：{identifier!r}。")
            seen.add(identifier)
    for ref in scenario.expected_context.included + scenario.expected_context.excluded:
        if not known(ref.type, ref.key):
            _issue(issues, f"{path}.expected_context", f"引用 {ref.token!r} 不在 fixture 中。")
    for dimension in scenario.evaluation_dimensions:
        if dimension not in METRIC_DEFINITIONS:
            _issue(issues, f"{path}.evaluation_dimensions",
                   f"未知指标 {dimension!r}；必须来自指标注册表。")
    sample = scenario.sample_output
    if sample is not None:
        for match in PLACEHOLDER_PATTERN.finditer(sample.content):
            ref_type, key = match.group(1), match.group(2)
            if not known(ref_type, key):
                _issue(issues, f"{path}.sample_output.content",
                       f"占位符 <<{ref_type}:{key}>> 引用的来源不在 fixture 中。")
        for expected in sample.expected_references:
            if expected.source is not None:
                if not known(expected.source.type, expected.source.key):
                    _issue(issues, f"{path}.sample_output.expected_references",
                           f"来源 {expected.source.token!r} 不在 fixture 中。")
                    continue
                token = f"<<{expected.source.type}:{expected.source.key}>>"
                if token not in sample.content:
                    _issue(issues, f"{path}.sample_output.expected_references",
                           f"样例内容没有出现预期引用 {token!r}。")
            elif expected.literal_id is not None:
                if str(expected.literal_id) not in sample.content:
                    _issue(issues, f"{path}.sample_output.expected_references",
                           f"样例内容没有出现预期引用 {expected.literal_id}。")


# ------------------------------------------------------------------- public API
def load_dataset(path: str | Path) -> EvaluationDataset:
    """Load and fully validate one dataset directory (fail-closed)."""
    root = Path(path)
    issues: list[DatasetIssue] = []
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise DatasetError([DatasetIssue(str(manifest_path), "缺少数据集清单文件。")])
    manifest_raw = _read_json(manifest_path, issues)
    manifest = _mapping(manifest_raw, MANIFEST_NAME, issues) or {}
    _check_keys(manifest, {"schema_version", "dataset_id", "dataset_version", "title",
                           "description", "synthetic", "fixtures", "scenarios"},
                MANIFEST_NAME, issues)
    _schema_version(manifest, MANIFEST_NAME, issues)
    if _boolean(manifest, "synthetic", MANIFEST_NAME, issues, default=False) is not True:
        _issue(issues, f"{MANIFEST_NAME}.synthetic",
               "必须显式声明 synthetic=true；评估数据集只允许完全虚构的数据。")
    dataset_version = _text(manifest, "dataset_version", MANIFEST_NAME, issues)
    if dataset_version != DATASET_VERSION:
        _issue(issues, f"{MANIFEST_NAME}.dataset_version",
               f"必须等于 {DATASET_VERSION!r}（本次发布的合成数据集版本）。")

    file_digests: dict[str, str] = {MANIFEST_NAME: _sha256(manifest_path.read_bytes())}
    fixtures: dict[str, FixtureSpec] = {}
    for index, name in enumerate(_sequence(manifest, "fixtures", MANIFEST_NAME, issues,
                                           required=True)):
        rel = str(name)
        raw = _read_json(root / name, issues)
        fixture = _parse_fixture(raw, rel, issues)
        if fixture is None:
            continue
        if fixture.fixture_id in fixtures:
            _issue(issues, rel, f"fixture_id 重复：{fixture.fixture_id!r}。")
            continue
        fixtures[fixture.fixture_id] = fixture
        file_digests[rel] = _sha256((root / name).read_bytes())

    scenarios: list[EvaluationScenario] = []
    seen_ids = set()
    for name in _sequence(manifest, "scenarios", MANIFEST_NAME, issues, required=True):
        rel = str(name)
        raw = _read_json(root / name, issues)
        scenario = _parse_scenario(raw, rel, issues)
        if scenario is None:
            continue
        if scenario.scenario_id in seen_ids:
            _issue(issues, rel, f"scenario_id 重复：{scenario.scenario_id!r}。")
            continue
        seen_ids.add(scenario.scenario_id)
        scenarios.append(scenario)
        file_digests[rel] = _sha256((root / name).read_bytes())

    for fixture in fixtures.values():
        _validate_fixture(fixture, issues)
    for scenario in scenarios:
        fixture = fixtures.get(scenario.fixture_id)
        if fixture is None:
            _issue(issues, scenario.source_path,
                   f"fixture {scenario.fixture_id!r} 未在清单中声明。")
            continue
        _validate_scenario(scenario, fixture, issues)

    if issues:
        raise DatasetError(issues)
    digest = _sha256("".join(f"{name}:{file_digests[name]}\n"
                             for name in sorted(file_digests)).encode("utf-8"))
    return EvaluationDataset(
        dataset_id=manifest["dataset_id"],
        dataset_version=manifest["dataset_version"],
        title=manifest.get("title", ""),
        description=manifest.get("description", ""),
        fixtures=fixtures,
        scenarios=tuple(scenarios),
        source_dir=str(root),
        digest=digest,
        file_digests=file_digests)


def scenario_digest(dataset: EvaluationDataset, scenario: EvaluationScenario) -> str:
    """Identify the exact scenario input (content digest, not a signature)."""
    return dataset.file_digests.get(scenario.source_path, "")


def _read_json(path: Path, issues) -> object:
    if not path.is_file():
        _issue(issues, str(path), "文件不存在。")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _issue(issues, str(path), f"无法读取 JSON：{exc}")
        return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
