"""Structured analysis result parser (Phase 13.1 / 7.4, 7.7).

The parser is pure Python: these tests only touch text. They pin the contract
that the raw answer is always preserved, every unsupported input degrades with a
visible warning, and nothing is silently deleted.
"""
import json
from uuid import uuid4

from app.domain.entities.analysis_result import (EMPTY, LEGACY_MARKDOWN,
                                                 PARSED_JSON,
                                                 PARSED_JSON_FRAGMENT,
                                                 TOO_LARGE,
                                                 UNSTRUCTURED_TEXT,
                                                 AnalysisResult)
from app.services.analysis_result_parser import (parse_analysis_result,
                                                 split_legacy_sections)

EVENT_ID = str(uuid4())
PERSON_ID = str(uuid4())

CONTRACT = {
    "schema_version": "1",
    "facts": [
        {"content": "2026-06-12 有一场深夜通话。",
         "references": [{"type": "EVENT", "id": EVENT_ID}]},
    ],
    "observations": [
        {"content": "用户记录：她更主动。"},
    ],
    "interpretations": [
        {"content": "双方可能仍在维持协作。",
         "references": [{"type": "PERSON", "id": PERSON_ID}],
         "confidence_description": "依据不足，仅为可能。"},
    ],
    "uncertainties": [
        {"content": "通话内容缺少证据支持。"},
    ],
}


def raw_json() -> str:
    return json.dumps(CONTRACT, ensure_ascii=False)


# ------------------------------------------------------------------ JSON contract
def test_valid_json_parses_into_entries_and_references():
    raw = raw_json()
    result = parse_analysis_result(raw)
    assert result.parse_status == PARSED_JSON
    assert result.raw_text == raw                       # never rewritten
    assert [entry.content for entry in result.facts] == ["2026-06-12 有一场深夜通话。"]
    assert result.facts[0].references[0].entity_type == "EVENT"
    assert str(result.facts[0].references[0].entity_id) == EVENT_ID
    assert result.interpretations[0].confidence_description == "依据不足，仅为可能。"
    assert result.warnings == ()
    assert result.unparsed_content == ""
    assert result.entry_count() == 4


def test_json_inside_fence_with_surrounding_prose_is_kept_as_fragment():
    raw = "以下是分析结果：\n```json\n" + raw_json() + "\n```\n以上。"
    result = parse_analysis_result(raw)
    assert result.parse_status == PARSED_JSON_FRAGMENT
    assert result.facts and result.facts[0].content.startswith("2026-06-12")
    assert "以下是分析结果" in result.unparsed_content
    assert any("JSON 前后" in warning for warning in result.warnings)
    assert result.raw_text == raw


def test_missing_sections_default_to_empty_tuples():
    raw = json.dumps({"facts": [{"content": "只有事实"}], "schema_version": "1"},
                     ensure_ascii=False)
    result = parse_analysis_result(raw)
    assert result.parse_status == PARSED_JSON
    assert len(result.facts) == 1
    assert result.observations == () and result.interpretations == ()
    assert result.uncertainties == ()


def test_missing_schema_version_is_warned_not_fatal():
    raw = json.dumps({"facts": [{"content": "只有事实"}]}, ensure_ascii=False)
    result = parse_analysis_result(raw)
    assert result.parse_status == PARSED_JSON
    assert result.schema_version == "1"
    assert any("schema_version" in warning for warning in result.warnings)


def test_wrong_types_are_kept_as_unparsed_content():
    raw = json.dumps({"schema_version": "1", "facts": "not-a-list",
                      "observations": [{"content": 42}, {"no_content": True}]},
                     ensure_ascii=False)
    result = parse_analysis_result(raw)
    assert result.parse_status == PARSED_JSON
    assert result.facts == ()
    assert result.observations == ()
    assert "not-a-list" in result.unparsed_content
    assert any("facts 不是数组" in warning for warning in result.warnings)
    assert any("content" in warning for warning in result.warnings)


def test_invalid_references_are_dropped_with_warning_but_entry_kept():
    raw = json.dumps({"schema_version": "1",
                      "facts": [{"content": "有引用但格式错误",
                                 "references": [
                                     {"type": "MEMORY", "id": EVENT_ID},     # not citable
                                     {"type": "EVENT", "id": "not-a-uuid"},
                                     {"type": "EVENT"},
                                     "not-an-object",
                                     {"type": "event", "id": EVENT_ID}]}]},
                     ensure_ascii=False)
    result = parse_analysis_result(raw)
    entry = result.facts[0]
    assert entry.content == "有引用但格式错误"
    assert len(entry.references) == 1
    assert entry.references[0].entity_type == "EVENT"
    assert len([w for w in result.warnings if "references" in w]) >= 3


def test_duplicate_entries_are_reported_but_kept():
    item = {"content": "重复的推断", "references": [{"type": "EVENT", "id": EVENT_ID}]}
    raw = json.dumps({"schema_version": "1", "interpretations": [item, item]},
                     ensure_ascii=False)
    result = parse_analysis_result(raw)
    assert len(result.interpretations) == 2
    assert any("重复条目" in warning for warning in result.warnings)


def test_unknown_top_level_fields_are_preserved():
    raw = json.dumps({"schema_version": "1", "facts": [{"content": "事实"}],
                      "extra_field": {"keep": "me"}}, ensure_ascii=False)
    result = parse_analysis_result(raw)
    assert "extra_field" in result.unparsed_content
    assert any("未识别字段" in warning for warning in result.warnings)


def test_string_entries_and_legacy_field_names_are_accepted():
    raw = json.dumps({"schema_version": "1",
                      "facts": ["字符串形式的事实"],
                      "uncertainties": [{"text": "非标准字段名"}]}, ensure_ascii=False)
    result = parse_analysis_result(raw)
    assert [entry.content for entry in result.facts] == ["字符串形式的事实"]
    assert [entry.content for entry in result.uncertainties] == ["非标准字段名"]
    assert any("非标准字段" in warning for warning in result.warnings)


# ---------------------------------------------------------------- legacy / degrade
def test_legacy_markdown_is_recognised_and_split():
    raw = ("## Facts（事实）\n- 2026-04-01 有一场摄影活动。\n\n"
           "## Observations（观察）\n- 用户记录：她更主动。\n\n"
           "## Interpretations（推断）\n- 两人可能保持联系。\n\n"
           "## Uncertainties（不确定与缺失）\n- 证据不足以确认关系强度。")
    result = parse_analysis_result(raw)
    assert result.parse_status == LEGACY_MARKDOWN
    assert [entry.content for entry in result.facts] == ["2026-04-01 有一场摄影活动。"]
    assert [entry.content for entry in result.observations] == ["用户记录：她更主动。"]
    assert result.raw_text == raw
    assert any("旧版" in warning for warning in result.warnings)
    # The legacy splitter keeps the old best-effort shape (bullets included) that
    # the result dialog uses for old runs; entry content strips the bullet.
    assert split_legacy_sections(raw)["interpretations"] == "- 两人可能保持联系。"


def test_plain_prose_degrades_to_raw_text():
    raw = "no headings at all, just prose"
    result = parse_analysis_result(raw)
    assert result.parse_status == UNSTRUCTURED_TEXT
    assert result.unparsed_content == raw
    assert all(result.section(key) == () for key in
               ("facts", "observations", "interpretations", "uncertainties"))
    assert result.raw_text == raw


def test_empty_and_oversized_answers():
    assert parse_analysis_result("").parse_status == EMPTY
    assert parse_analysis_result("   \n  ").parse_status == EMPTY
    huge = "x" * 200_001
    result = parse_analysis_result(huge)
    assert result.parse_status == TOO_LARGE
    assert result.raw_text == huge
    assert result.facts == ()


def test_parse_is_deterministic_and_does_not_mutate_the_contract():
    raw = raw_json()
    first = parse_analysis_result(raw)
    second = parse_analysis_result(raw)
    assert first == second
    assert isinstance(first, AnalysisResult)
    assert first.raw_text == raw


def test_display_text_marks_references_as_unvalidated():
    result = parse_analysis_result(raw_json())
    text = result.display_text("interpretations")
    assert "双方可能仍在维持协作。" in text
    assert "引用（未校验）" in text
    assert "不确定说明：依据不足，仅为可能。" in text
