"""Dataset schema, uniqueness, versioning and referential integrity (Phase 13.0 / 6.7).

Every negative test mutates a *copy* of the bundled dataset, so the canonical
synthetic corpus stays untouched. Validation is fail-closed and reports every
issue it finds.
"""
import json
import shutil
from pathlib import Path

import pytest

from app.evaluation import DATASET_VERSION, SCHEMA_VERSION, DatasetError, load_dataset

DATASET_DIR = Path(__file__).parent / "dataset"

EXPECTED_SCENARIO_IDS = {
    "SCENARIO_A_COMPLETE", "SCENARIO_B_SPARSE", "SCENARIO_C_NO_SOURCE",
    "SCENARIO_D_CONFLICT", "SCENARIO_E_EPISTEMIC", "SCENARIO_F_RELATIONSHIP_CHANGE",
    "SCENARIO_G_LAST_7_DAYS", "SCENARIO_G_LAST_30_DAYS", "SCENARIO_G_LAST_90_DAYS",
    "SCENARIO_G_ALL", "SCENARIO_G_CUSTOM", "SCENARIO_H_EVIDENCE_OFF",
    "SCENARIO_H_EVIDENCE_ON", "SCENARIO_I_MEMORY_DEFAULT", "SCENARIO_I_MEMORY_ON",
    "SCENARIO_J_NEAR_LIMIT", "SCENARIO_J_OVER_LIMIT",
}


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(DATASET_DIR)


# ------------------------------------------------------------------- schema ok
def test_dataset_loads_and_covers_scenarios_a_to_j(dataset):
    assert dataset.dataset_version == DATASET_VERSION
    assert {item.scenario_id for item in dataset.scenarios} == EXPECTED_SCENARIO_IDS
    assert len(dataset.scenarios) == 17
    # Scenario families A–J are all represented.
    for family in "ABCDEFGHIJ":
        assert any(item.scenario_id.startswith(f"SCENARIO_{family}_") for item in dataset.scenarios)


def test_scenario_ids_and_fixture_ids_are_unique(dataset):
    ids = [item.scenario_id for item in dataset.scenarios]
    assert len(ids) == len(set(ids))
    assert len(dataset.fixtures) == 10


def test_every_fixture_declares_synthetic_provenance(dataset):
    assert all(fixture.synthetic for fixture in dataset.fixtures.values())


def test_bulk_expansion_is_deterministic(dataset):
    fixture = dataset.fixtures["fixture-j-large"]
    first = [event.key for event in fixture.all_events()]
    second = [event.key for event in fixture.all_events()]
    assert first == second
    bulk_keys = [key for key in first if key.startswith("bulk-ev-")]
    assert bulk_keys[:3] == ["bulk-ev-000", "bulk-ev-001", "bulk-ev-002"]
    assert len(bulk_keys) == 85
    assert len([item.key for item in fixture.all_evidence()
                if item.key.startswith("bulk-evd-")]) == 25


def test_dataset_digest_is_stable_and_content_sensitive(dataset, tmp_path):
    assert dataset.digest == load_dataset(DATASET_DIR).digest
    tampered = _copy_dataset(tmp_path)
    scenario_path = tampered / "scenarios" / "scenario-b-sparse.json"
    data = json.loads(scenario_path.read_text(encoding="utf-8"))
    data["description"] = data["description"] + "（改动）"
    scenario_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    changed = load_dataset(tampered)
    assert changed.digest != dataset.digest


# ---------------------------------------------------------------- rejections
def test_unknown_schema_version_is_rejected(tmp_path):
    target = _copy_dataset(tmp_path)
    manifest = target / "dataset.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["schema_version"] = "99"
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(DatasetError) as failure:
        load_dataset(target)
    assert any("schema_version" in issue.path for issue in failure.value.issues)


def test_duplicate_scenario_id_is_rejected(tmp_path):
    target = _copy_dataset(tmp_path)
    manifest = target / "dataset.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["scenarios"].append("scenarios/scenario-a-complete.json")
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(DatasetError) as failure:
        load_dataset(target)
    assert any("scenario_id 重复" in issue.message for issue in failure.value.issues)


def test_dataset_version_mismatch_between_scenario_and_manifest(tmp_path):
    target = _copy_dataset(tmp_path)
    path = target / "scenarios" / "scenario-b-sparse.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["dataset_version"] = "2.0"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(DatasetError) as failure:
        load_dataset(target)
    assert any("dataset_version" in issue.path for issue in failure.value.issues)


def test_expected_fact_with_unknown_source_key_is_rejected(tmp_path):
    target = _copy_dataset(tmp_path)
    path = target / "scenarios" / "scenario-a-complete.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["expected_facts"][0]["source"] = {"type": "PERSON", "key": "ghost-person"}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(DatasetError) as failure:
        load_dataset(target)
    assert any("ghost-person" in issue.message for issue in failure.value.issues)


def test_expected_fact_with_wrong_source_type_is_rejected(tmp_path):
    target = _copy_dataset(tmp_path)
    path = target / "scenarios" / "scenario-a-complete.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    # alice exists, but as PERSON — declaring her as EVENT must fail.
    data["expected_facts"][0]["source"] = {"type": "EVENT", "key": "alice"}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(DatasetError) as failure:
        load_dataset(target)
    assert any("EVENT:alice" in issue.message for issue in failure.value.issues)


def test_unknown_evaluation_dimension_is_rejected(tmp_path):
    target = _copy_dataset(tmp_path)
    path = target / "scenarios" / "scenario-b-sparse.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["evaluation_dimensions"].append("NOT_A_METRIC")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(DatasetError) as failure:
        load_dataset(target)
    assert any("NOT_A_METRIC" in issue.message for issue in failure.value.issues)


def test_missing_synthetic_flag_is_rejected(tmp_path):
    target = _copy_dataset(tmp_path)
    path = target / "fixtures" / "fixture-b-sparse.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["synthetic"]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(DatasetError) as failure:
        load_dataset(target)
    assert any("synthetic" in issue.path for issue in failure.value.issues)


def test_scenario_referencing_unknown_fixture_is_rejected(tmp_path):
    target = _copy_dataset(tmp_path)
    path = target / "scenarios" / "scenario-b-sparse.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["fixture"] = "fixture-ghost"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(DatasetError) as failure:
        load_dataset(target)
    assert any("fixture-ghost" in issue.message for issue in failure.value.issues)


def test_unknown_field_is_rejected_to_catch_typos(tmp_path):
    target = _copy_dataset(tmp_path)
    path = target / "scenarios" / "scenario-b-sparse.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["typo_field"] = 1
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(DatasetError) as failure:
        load_dataset(target)
    assert any("typo_field" in issue.path for issue in failure.value.issues)


# ------------------------------------------------------------------- privacy
def test_dataset_contains_no_real_contact_data_markers():
    for path in DATASET_DIR.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        for marker in ("@", "http://", "https://", "Bearer ", "sk-", "API_KEY"):
            assert marker not in text, f"{path} 含有疑似真实联系数据标记 {marker!r}"


def test_loading_dataset_never_touches_any_database(tmp_path, monkeypatch):
    # The loader is pure file parsing: even a failing storage layer must not be
    # reached. If someone later adds DB access to load_dataset, this trips.
    def explode(*args, **kwargs):
        raise AssertionError("load_dataset 不得访问数据库")
    monkeypatch.setattr("app.services.database.initialize_database", explode)
    monkeypatch.setattr("app.services.application.create_application_services", explode)
    load_dataset(DATASET_DIR)


def _copy_dataset(tmp_path) -> Path:
    target = tmp_path / "dataset"
    shutil.copytree(DATASET_DIR, target)
    return target
