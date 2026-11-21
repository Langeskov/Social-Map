"""Parser for the structured analysis result contract (Phase 13.1).

Pure Python: no Qt, no database, no LLM, no network. The parser never rewrites
the answer — the raw text is always preserved verbatim and unrecognised content
is collected in ``AnalysisResult.unparsed_content`` instead of being dropped.

Supported inputs: strict JSON, JSON inside surrounding prose / fenced blocks,
the legacy four-section Markdown format, plain prose and empty answers. Every
failure class is visible through ``parse_status`` + ``warnings``.
"""
import json
from uuid import UUID

from app.domain.entities.analysis_result import (EMPTY, LEGACY_MARKDOWN,
                                                 MAX_RESULT_CHARS, PARSED_JSON,
                                                 PARSED_JSON_FRAGMENT,
                                                 REFERENCE_TYPES, SCHEMA_VERSION,
                                                 SECTION_KEYS, TOO_LARGE,
                                                 UNSTRUCTURED_TEXT, AnalysisEntry,
                                                 AnalysisReference, AnalysisResult)

#: Bounded amount of unrecognised JSON text kept for diagnosis.
MAX_UNPARSED_CHARS = 20_000

_LEGACY_HEADING_KEYS = (("fact", "facts"), ("事实", "facts"),
                        ("observation", "observations"), ("观察", "observations"),
                        ("interpretation", "interpretations"), ("推断", "interpretations"),
                        ("uncertaint", "uncertainties"), ("不确定", "uncertainties"))


def parse_analysis_result(raw_text: str) -> AnalysisResult:
    """Parse one model answer into the structured contract (never mutates it)."""
    raw = raw_text if isinstance(raw_text, str) else ""
    if len(raw) > MAX_RESULT_CHARS:
        return AnalysisResult(raw_text=raw, schema_version=SCHEMA_VERSION,
                              parse_status=TOO_LARGE,
                              warnings=(f"输出超过解析上限（{MAX_RESULT_CHARS} 字符），"
                                        "只保留原文，未做结构化解析。",))
    if not raw.strip():
        return AnalysisResult(raw_text=raw, schema_version=SCHEMA_VERSION,
                              parse_status=EMPTY)
    payload, outside, in_fragment = _extract_json_object(raw)
    if payload is not None:
        return _parse_json_payload(raw, payload, outside, in_fragment)
    sections = split_legacy_sections(raw)
    found = [key for key in SECTION_KEYS if sections.get(key)]
    if found:
        entries = {key: _legacy_entries(sections.get(key, "")) for key in SECTION_KEYS}
        return AnalysisResult(
            raw_text=raw, schema_version="", parse_status=LEGACY_MARKDOWN,
            **{key: entries[key] for key in SECTION_KEYS},
            unparsed_content=sections.get("other", ""),
            warnings=("旧版 Markdown 四段格式：已按小节展示，但没有结构化引用。",))
    return AnalysisResult(raw_text=raw, schema_version="", parse_status=UNSTRUCTURED_TEXT,
                          unparsed_content=raw,
                          warnings=("输出不是结构化 JSON，也没有识别到四段标题；"
                                    "仅按原始文本展示。",))


# ---------------------------------------------------------------- JSON extraction
def _extract_json_object(raw: str):
    """Return (payload, outside_text, fragment) or (None, "", False)."""
    stripped = raw.strip()
    try:
        payload = json.loads(stripped)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict) and _looks_like_payload(payload):
        return payload, "", False
    # Fall back to the first '{' .. last '}' substring (fenced blocks, prose around).
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None, "", False
    chunk = raw[start:end + 1]
    try:
        payload = json.loads(chunk)
    except (ValueError, TypeError):
        return None, "", False
    if not isinstance(payload, dict) or not _looks_like_payload(payload):
        return None, "", False
    outside = (raw[:start] + "\n" + raw[end + 1:]).strip()
    return payload, outside, True


def _looks_like_payload(payload: dict) -> bool:
    return any(isinstance(payload.get(key), (list, str)) for key in SECTION_KEYS)


# ------------------------------------------------------------------ JSON payload
def _parse_json_payload(raw, payload, outside, fragment) -> AnalysisResult:
    warnings = []
    if fragment:
        warnings.append("JSON 前后存在未识别的文本；已保留在未解析内容中。")
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version.strip():
        schema_version = SCHEMA_VERSION
        warnings.append("缺少 schema_version，按 schema 1 解释。")
    entries, unparsed_parts = {}, []
    for key in SECTION_KEYS:
        items = payload.get(key)
        if items is None:
            entries[key] = ()
            continue
        if not isinstance(items, list):
            warnings.append(f"{key} 不是数组，内容已保留在未解析内容中。")
            unparsed_parts.append(f"{key}: {json.dumps(items, ensure_ascii=False)}")
            entries[key] = ()
            continue
        parsed = []
        for index, item in enumerate(items):
            entry = _parse_entry(key, index, item, warnings, unparsed_parts)
            if entry is not None:
                parsed.append(entry)
        entries[key] = tuple(parsed)
        seen = set()
        for entry in parsed:
            if entry.identity() in seen:
                warnings.append(f"{key} 存在重复条目（内容与引用相同），已保留原文并标记。")
                break
            seen.add(entry.identity())
    unknown = sorted(set(payload) - set(SECTION_KEYS) - {"schema_version"})
    if unknown:
        warnings.append("存在未识别字段：" + "、".join(unknown))
        unparsed_parts.append("未知字段：" + ", ".join(
            f"{name}={json.dumps(payload[name], ensure_ascii=False)}" for name in unknown))
    unparsed = "\n".join(part for part in [outside] + unparsed_parts if part)
    if len(unparsed) > MAX_UNPARSED_CHARS:
        unparsed = unparsed[:MAX_UNPARSED_CHARS] + "\n…（未解析内容已截断）"
        warnings.append("未解析内容超过上限，已截断保存。")
    return AnalysisResult(raw_text=raw, schema_version=schema_version,
                          parse_status=(PARSED_JSON_FRAGMENT if fragment else PARSED_JSON),
                          **{key: entries[key] for key in SECTION_KEYS},
                          unparsed_content=unparsed, warnings=tuple(warnings))


def _parse_entry(section, index, item, warnings, unparsed_parts):
    if isinstance(item, str):
        content = item.strip()
        if not content:
            warnings.append(f"{section}[{index}] 是空字符串，已跳过。")
            return None
        return AnalysisEntry(content=content)
    if not isinstance(item, dict):
        warnings.append(f"{section}[{index}] 不是对象或字符串，已保留在未解析内容中。")
        unparsed_parts.append(f"{section}[{index}]: {json.dumps(item, ensure_ascii=False)}")
        return None
    content = None
    for field in ("content", "text", "summary", "statement"):
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            content = value.strip()
            if field != "content":
                warnings.append(f"{section}[{index}] 使用非标准字段 {field!r} 作为内容。")
            break
    if content is None:
        warnings.append(f"{section}[{index}] 缺少 content，已保留在未解析内容中。")
        unparsed_parts.append(f"{section}[{index}]: {json.dumps(item, ensure_ascii=False)}")
        return None
    references = _parse_references(section, index, item.get("references"), warnings)
    confidence = item.get("confidence_description")
    if confidence is not None and not isinstance(confidence, str):
        warnings.append(f"{section}[{index}].confidence_description 不是字符串，已忽略。")
        confidence = ""
    notes = item.get("notes")
    if notes is not None and not isinstance(notes, str):
        warnings.append(f"{section}[{index}].notes 不是字符串，已忽略。")
        notes = ""
    known = {"content", "text", "summary", "statement", "references",
             "confidence_description", "notes"}
    extra = sorted(set(item) - known)
    if extra:
        warnings.append(f"{section}[{index}] 含未识别字段：" + "、".join(extra))
    return AnalysisEntry(content=content, references=references,
                         confidence_description=(confidence or "").strip(),
                         notes=(notes or "").strip())


def _parse_references(section, index, value, warnings):
    if value is None:
        return ()
    if not isinstance(value, list):
        warnings.append(f"{section}[{index}].references 不是数组，已忽略。")
        return ()
    references, seen = [], set()
    for position, item in enumerate(value):
        if not isinstance(item, dict):
            warnings.append(f"{section}[{index}].references[{position}] 不是对象，已忽略。")
            continue
        entity_type = item.get("type")
        identifier = item.get("id")
        if not isinstance(entity_type, str) or entity_type.strip().upper() not in REFERENCE_TYPES:
            warnings.append(f"{section}[{index}].references[{position}] 类型无效，已忽略。")
            continue
        if not isinstance(identifier, str):
            warnings.append(f"{section}[{index}].references[{position}] 缺少 id，已忽略。")
            continue
        try:
            entity_id = UUID(identifier.strip())
        except ValueError:
            warnings.append(f"{section}[{index}].references[{position}] id 不是合法 UUID，已忽略。")
            continue
        reference = AnalysisReference(entity_type.strip().upper(), entity_id)
        key = (reference.entity_type, str(reference.entity_id))
        if key in seen:
            continue
        seen.add(key)
        references.append(reference)
    return tuple(references)


# --------------------------------------------------------------- legacy markdown
def split_legacy_sections(text: str) -> dict:
    """Best-effort split of the legacy four-heading answer (kept for old runs)."""
    sections = {key: "" for key in SECTION_KEYS}
    sections["other"] = ""
    current = "other"
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        key = _legacy_heading_key(line)
        if key is not None:
            current = key
            continue
        if line:
            sections[current] += (line + "\n")
    return {name: value.strip() for name, value in sections.items()}


def _legacy_heading_key(line: str):
    lowered = line.lstrip("#").strip().lower().rstrip("：:")
    if not lowered:
        return None
    for prefix, key in _LEGACY_HEADING_KEYS:
        if lowered.startswith(prefix):
            return key
    return None


def _legacy_entries(text: str) -> tuple:
    entries = []
    for line in (text or "").splitlines():
        content = line.strip()
        for bullet in ("- ", "• ", "* "):
            if content.startswith(bullet):
                content = content[len(bullet):].strip()
                break
        if content:
            entries.append(AnalysisEntry(content=content))
    return tuple(entries)
