"""Phase 10 DataIntegrityService: read-only audit over real SQLite and files.

Every test here runs against a database created exclusively by Alembic
migrations, a real temporary media directory and the public services. Damaged
state is injected the only way SQLite allows it - through a dedicated raw
connection with ``PRAGMA foreign_keys=OFF`` - never through the ORM, which would
refuse exactly the rows the audit has to detect.

The suite proves four separate properties of ``check()``:

* it finds the ten classes of damage the specification names;
* it classifies them as ERROR/WARNING and derives ``ok`` from errors alone;
* it never repairs, deletes or rewrites anything - no row value changes and no
  media file is created, removed or modified by an audit;
* ``verify_media_files=False`` really touches no filesystem at all.
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from PIL import Image
from sqlalchemy import func, select

from app.config.settings import AppConfig
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.services.integrity import (ERROR, WARNING, DataIntegrityService, IntegrityIssue,
                                    IntegrityReport)
from app.services.unit_of_work import transaction
from app.storage.models import (AnalysisEventSourceModel, AnalysisEvidenceSourceModel,
                                AnalysisPersonSourceModel, AnalysisRelationshipSourceModel,
                                AnalysisRunModel, EventModel, EventParticipantModel,
                                EvidenceMediaModel, EvidenceModel, MediaModel, MemoryModel,
                                PersonAliasModel, PersonMediaModel, PersonModel,
                                RelationshipHistoryModel, RelationshipModel,
                                SearchDocumentModel, WorkspaceModel)

NOW = datetime.now(timezone.utc).replace(microsecond=0)
ALL_MODELS = (WorkspaceModel, PersonModel, PersonAliasModel, MediaModel, PersonMediaModel,
              RelationshipModel, RelationshipHistoryModel, EventModel, EventParticipantModel,
              EvidenceModel, EvidenceMediaModel, SearchDocumentModel, AnalysisRunModel,
              AnalysisPersonSourceModel, AnalysisRelationshipSourceModel,
              AnalysisEventSourceModel, AnalysisEvidenceSourceModel, MemoryModel)
# Every workspace-scoped child table and the parent it must agree with.
WORKSPACE_MISMATCH_CODES = ("PERSON_WORKSPACE_MISMATCH", "RELATIONSHIP_WORKSPACE_MISMATCH",
                            "RELATIONSHIP_HISTORY_WORKSPACE_MISMATCH",
                            "EVENT_PARTICIPANT_WORKSPACE_MISMATCH",
                            "EVIDENCE_WORKSPACE_MISMATCH",
                            "EVIDENCE_MEDIA_WORKSPACE_MISMATCH",
                            "PERSON_MEDIA_WORKSPACE_MISMATCH", "MEDIA_WORKSPACE_MISMATCH")


# --------------------------------------------------------------------- fixtures
def make_image(path: Path, color=(30, 60, 90), size=(23, 17)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")
    return path


@pytest.fixture
def env(tmp_path):
    """A migrated database, a real media tree and five seeded fact rows."""
    config = AppConfig(data_dir=tmp_path / "integrity-data")
    runtime = initialize_database(config)
    services = create_application_services(config, runtime.sessions)
    store = services.media.store
    originals = tmp_path / "originals"
    workspace = services.initial_workspace
    person = services.people.create(workspace.id, "审计对象甲")
    services.people.add_alias(workspace.id, person.id, "甲甲", "NICKNAME")
    album = services.media.import_file(workspace.id, make_image(originals / "album.png"))
    services.people.add_gallery(workspace.id, person.id, album.id)
    other = services.people.create(workspace.id, "审计对象乙")
    relationship = services.relationships.create(workspace.id, person.id, other.id, "Friend")
    event = services.events.create_event(workspace.id, "初次见面", "Meeting", "FACT",
                                         occurred_on=NOW.date(),
                                         participants=(person.id, other.id))
    capture = services.media.import_file(workspace.id,
                                         make_image(originals / "capture.png", (10, 120, 40)))
    evidence = services.evidence.create_evidence(workspace.id, event.id, "IMAGE", "合影",
                                                 source_type="IMAGE", media=(capture.id,))
    try:
        yield SimpleNamespace(config=config, runtime=runtime, services=services, store=store,
                              tmp_path=tmp_path, originals=originals, ws=workspace.id,
                              person=person, other=other, relationship=relationship,
                              event=event, evidence=evidence, album=album, capture=capture,
                              integrity=DataIntegrityService(runtime.sessions, media_store=store))
    finally:
        runtime.close()


@pytest.fixture
def raw_connection(env):
    """A raw DBAPI connection able to create state the constraints forbid.

    SQLite ignores ``PRAGMA foreign_keys`` inside a transaction, so damage is
    injected outside one, on the DBAPI connection, and enforcement is restored
    before the connection is handed back.
    """
    connection = env.runtime.engine.raw_connection()
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        yield connection
        connection.execute("PRAGMA foreign_keys=ON")
    finally:
        connection.close()


# ---------------------------------------------------------------------- helpers
def statement(connection, model, **values):
    """Insert one row from the model's own columns, for either connection kind.

    The column list and the bind order are read back from the mapped table instead
    of being written out by hand, so a model that gains a column makes these tests
    insert its default rather than silently forgetting it. Positional parameters
    need ``exec_driver_sql`` on a Connection; the fixture's raw DBAPI connection
    takes them directly.
    """
    table = model.__table__
    defaults = {"id": uuid4(), "created_at": NOW, "updated_at": NOW}
    unknown = set(values) - set(table.c.keys())
    assert not unknown, f"{table.name} 没有这些列：{sorted(unknown)}"
    # Only the columns the caller named (plus the three lifecycle defaults) are
    # written, so an omitted NOT NULL column still fails loudly instead of being
    # silently filled with NULL here.
    bound = {column.name: text_value(values[column.name] if column.name in values
                                     else defaults[column.name])
             for column in table.columns if column.name in values or column.name in defaults}
    columns = ", ".join(bound)
    placeholders = ", ".join("?" for _ in bound)
    sql = f"INSERT INTO {table.name} ({columns}) VALUES ({placeholders})"
    parameters = tuple(bound.values())
    if hasattr(connection, "exec_driver_sql"):
        connection.exec_driver_sql(sql, parameters)
    else:
        connection.execute(sql, parameters)


def text_value(value):
    """The driver-level form of a UUID/datetime, as SQLite stores it."""
    if isinstance(value, UUID):
        return value.hex
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(" ")
    return value


def table_snapshot(env):
    """Every row of every table as stable text, the FTS5 projection included."""
    snapshot = {}
    with env.runtime.engine.connect() as connection:
        for name in sorted(env.runtime.engine.dialect.get_table_names(connection)):
            rows = connection.exec_driver_sql(f'SELECT * FROM "{name}"').all()
            snapshot[name] = sorted(tuple(str(value) for value in row) for row in rows)
    return snapshot


def media_files(env):
    """Every file in the media tree with its digest."""
    return {path.relative_to(env.store.media_dir).as_posix():
            hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(env.store.media_dir.rglob("*")) if path.is_file()}


def file_of(env, media):
    return env.store.path_for(media.storage_path)


def codes(report, severity=None):
    return report.codes(severity)


def assert_single(report, code, severity=None):
    issues = [issue for issue in report.issues if issue.code == code]
    assert len(issues) == 1, [issue.code for issue in report.issues]
    if severity is not None:
        assert issues[0].severity == severity
    return issues[0]


def add_run(env, workspace_id, **overrides):
    values = dict(id=uuid4(), workspace_id=workspace_id, target_type="PERSON", target_id=uuid4(),
                  analysis_type="PERSON_PROFILE", provider_name="fake", model_name="fake-model",
                  scope="{}", status="SUCCEEDED", context_snapshot=None, result=None,
                  error_message=None, created_at=NOW, started_at=NOW, completed_at=NOW,
                  duration_ms=1)
    values.update(overrides)
    with env.runtime.engine.begin() as connection:
        statement(connection, AnalysisRunModel,
                  **{key: text_value(value) for key, value in values.items()})
    return values["id"]


def add_memory(env, workspace_id, *, unchecked=False, orphan_reference=False, **overrides):
    """Insert one memory row.

    ``unchecked=True`` writes the row the way only a database whose constraints
    were bypassed could hold it: SQLite's ``ignore_check_constraints`` switch is
    turned on for the insert and restored immediately afterwards. ``orphan_reference``
    additionally drops foreign key enforcement so ``source_analysis_id`` may name a
    run that does not exist. Both are how a Memory that no service would ever
    create - a MANUAL row naming a run, an un-trimmed body, a backwards validity
    window, a dangling run reference - is placed in the file so the audit has
    something real to find. Neither is ever used to write valid data.
    """
    values = dict(id=uuid4(), workspace_id=workspace_id, person_id=None, kind="FACT",
                  content="保留记忆正文", source_type="MANUAL", source_analysis_id=None,
                  confirmed_at=NOW, valid_from=None, valid_until=None, created_at=NOW,
                  updated_at=NOW, deleted_at=None)
    values.update(overrides)
    bound = {key: text_value(value) for key, value in values.items()}
    if not unchecked and not orphan_reference:
        with env.runtime.engine.begin() as connection:
            statement(connection, MemoryModel, **bound)
        return values["id"]
    raw = env.runtime.engine.raw_connection()
    try:
        if unchecked:
            raw.execute("PRAGMA ignore_check_constraints=ON")
        if orphan_reference:
            raw.execute("PRAGMA foreign_keys=OFF")
        statement(raw, MemoryModel, **bound)
    finally:
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute("PRAGMA ignore_check_constraints=OFF")
        raw.close()
    return values["id"]


def add_media_row(env, workspace_id, **overrides):
    values = dict(id=uuid4(), workspace_id=workspace_id, sha256="b" * 64,
                  mime_type="image/png", size_bytes=8, width=2, height=2,
                  storage_path=f"media/{workspace_id}/{'bb'}/{'b' * 64}",
                  created_at=NOW, updated_at=NOW, deleted_at=None)
    values.update(overrides)
    with env.runtime.engine.begin() as connection:
        statement(connection, MediaModel,
                  **{key: text_value(value) for key, value in values.items()})
    return values["id"]


# ------------------------------------------------------------- 1. a clean audit
def test_clean_database_reports_ok_and_real_counts(env):
    report = env.integrity.check()
    assert isinstance(report, IntegrityReport)
    assert report.ok is True
    assert report.errors == () and report.warnings == ()
    assert report.checked_at.tzinfo is not None
    assert report.checked_at.utcoffset() == timedelta(0)
    # The eleven fact tables are counted from the real rows, plus the projection,
    # the analysis ledger and memory, plus the file-level statistics. The Phase 7
    # projection holds one row per searchable fact, so this fixture seeds five:
    # two person documents, one alias document, one event and one evidence.
    for name, expected in (("workspace", 1), ("person", 2), ("person_alias", 1), ("media", 2),
                           ("person_media", 1), ("relationship", 1), ("relationship_history", 1),
                           ("event", 1), ("event_participant", 2), ("evidence", 1),
                           ("evidence_media", 1), ("search_document", 5), ("analysis_run", 0),
                           ("memory", 0)):
        assert report.counts[f"table_{name}"] == expected, name
    assert report.counts["fact_tables"] == 11
    assert report.counts["workspace_filtered"] is False
    assert report.counts["media_rows"] == 2
    assert report.counts["media_files_checked"] == 2
    assert report.counts["media_hashes_checked"] == 2
    assert report.counts["orphan_files"] == 0
    assert report.counts["media_unreferenced"] == 0
    assert report.counts["error_count"] == 0 and report.counts["warning_count"] == 0
    with transaction(env.runtime.sessions) as session:
        assert session.scalar(select(func.count()).select_from(PersonModel)) == 2
    # Summary and mapping are the frozen UI/manifest surface.
    assert report.summary().startswith("OK：") and "0 error" in report.summary()
    assert "11" in report.summary()
    mapping = report.to_mapping()
    assert json.loads(json.dumps(mapping, ensure_ascii=False)) == mapping
    assert mapping["ok"] is True and mapping["errors"] == [] and mapping["warnings"] == []
    assert mapping["checked_at"] == report.checked_at.isoformat()


def test_workspace_filter_audits_only_that_workspace(env):
    other = env.services.workspaces.create("另一个 Workspace")
    outsider = env.services.people.create(other.id, "外部人物")
    # A media row of that workspace with a real file behind it, so the scoped audit
    # has something file-level to reconcile and still reports nothing broken.
    spare = env.services.media.import_file(other.id, make_image(env.originals / "scoped.png",
                                                               (9, 9, 200)))
    report = env.integrity.check(other.id)
    assert report.errors == (), [i.detail for i in report.errors]
    assert report.ok is True and report.counts["workspace_filtered"] is True
    assert report.counts["table_person"] == 1
    assert report.counts["table_media"] == 1
    assert report.counts["media_files_checked"] == 1
    assert report.counts["table_relationship"] == 0
    assert report.counts["table_search_document"] == 1
    assert [item.entity_id for item in report.warnings
            if item.code == "MEDIA_UNREFERENCED"] == [str(spare.id)]
    # Neither audit sees the other workspace's rows.
    assert env.integrity.check(env.ws).counts["table_person"] == 2
    assert env.integrity.check(env.ws).counts["table_media"] == 2
    assert env.integrity.check().counts["table_person"] == 3
    assert env.integrity.check().counts["table_media"] == 3
    assert env.integrity.check(other.id).counts["table_evidence"] == 0
    with transaction(env.runtime.sessions) as session:
        assert session.scalar(select(func.count()).select_from(PersonModel)
                              .where(PersonModel.id == outsider.id)) == 1
        assert session.scalar(select(func.count()).select_from(MediaModel)) == 3
    with pytest.raises(Exception):
        env.integrity.check("not-a-uuid")


# ---------------------------------------------------------- 2. foreign key damage
def test_foreign_key_violation_is_an_error(env, raw_connection):
    orphan = uuid4()
    statement(raw_connection, PersonModel, id=orphan.hex, workspace_id=uuid4().hex,
              display_name="孤儿人物", real_name=None, notes=None,
              created_at=text_value(NOW), updated_at=text_value(NOW), deleted_at=None)
    report = env.integrity.check()
    assert report.ok is False
    issue = assert_single(report, "FOREIGN_KEY_VIOLATION", ERROR)
    assert issue.entity_type == "person" and "workspace" in issue.message
    assert issue.detail is not None and "rowid" in issue.detail
    # The same row also cannot be reconciled with a parent workspace, and the audit
    # names both facts instead of hiding one behind the other.
    mismatch = assert_single(report, "PERSON_WORKSPACE_MISMATCH", ERROR)
    assert mismatch.entity_id == str(orphan)
    assert mismatch.detail.endswith("workspace.workspace_id=-")
    assert report.counts["error_count"] == 2
    # The damaged row is reported, never repaired.
    assert report.counts["table_person"] == 3
    with transaction(env.runtime.sessions) as session:
        assert session.scalar(select(func.count()).select_from(PersonModel)) == 3


# ------------------------------------------------------- 3./5. media file damage
def test_missing_media_file_is_an_error_and_is_not_recreated(env):
    path = file_of(env, env.album)
    path.unlink()
    report = env.integrity.check()
    assert report.ok is False
    issue = assert_single(report, "MEDIA_FILE_MISSING", ERROR)
    assert issue.entity_id == str(env.album.id)
    assert issue.detail == env.album.storage_path
    assert not Path(issue.detail).is_absolute()       # never an absolute host path
    assert path.exists() is False                     # an audit never restores bytes
    assert report.counts["media_files_checked"] == 1  # only the surviving file
    assert "MEDIA_FILE_HASH_MISMATCH" not in codes(report)


def test_modified_media_file_reports_hash_and_size_mismatch(env):
    path = file_of(env, env.capture)
    tampered = path.read_bytes() + b"tampered"
    path.write_bytes(tampered)
    report = env.integrity.check()
    assert report.ok is False
    assert "MEDIA_FILE_SIZE_MISMATCH" in codes(report, ERROR)
    assert "MEDIA_FILE_HASH_MISMATCH" in codes(report, ERROR)
    assert path.read_bytes() == tampered              # never rewritten back
    assert file_of(env, env.album).exists() is True


def test_same_size_but_different_bytes_is_a_hash_mismatch(env):
    path = file_of(env, env.album)
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 0xFF]) + original[1:])
    assert len(path.read_bytes()) == len(original)
    report = env.integrity.check()
    assert "MEDIA_FILE_HASH_MISMATCH" in codes(report, ERROR)
    # The length is unchanged, so size alone must not raise the size code.
    assert "MEDIA_FILE_SIZE_MISMATCH" not in codes(report)


# ------------------------------------------------------- 4. orphan media files
def test_orphan_media_file_is_a_warning_and_survives_the_audit(env):
    relative = f"media/{env.ws}/ff/{'f' * 64}"
    orphan = env.store.media_dir.joinpath(*relative.split("/"))
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"no row claims this file")
    before = media_files(env)

    report = env.integrity.check()
    assert report.ok is True                          # warnings never fail the audit
    assert report.errors == ()
    issue = assert_single(report, "ORPHAN_MEDIA_FILE", WARNING)
    assert issue.entity_id is None and issue.detail == relative
    assert report.counts["orphan_files"] == 1
    # The file is still there, byte-identical: the audit only reports.
    assert orphan.read_bytes() == b"no row claims this file"
    assert media_files(env) == before


def test_staging_and_lock_files_are_never_orphans(env):
    (env.store.staging_dir / str(uuid4())).write_bytes(b"staged upload")
    (env.store.locks_dir / "leftover.lock").write_bytes(b"lock")
    report = env.integrity.check()
    assert report.ok is True
    assert "ORPHAN_MEDIA_FILE" not in codes(report)
    assert report.counts["orphan_files"] == 0


def test_unreferenced_media_is_a_warning_and_deleted_evidence_still_counts(env):
    spare = env.services.media.import_file(env.ws, make_image(env.originals / "spare.png",
                                                             (200, 10, 10)))
    report = env.integrity.check()
    assert report.ok is True
    issue = assert_single(report, "MEDIA_UNREFERENCED", WARNING)
    assert issue.entity_id == str(spare.id)
    # The detail carries the relative storage path, which is the file the operator
    # has to judge - never an absolute host path.
    assert spare.storage_path in issue.detail
    assert issue.detail.startswith(f"workspace_id={env.ws} media/{env.ws}/")
    # Phase 7 rule: soft deleting the Evidence keeps its EvidenceMedia row, so the
    # attached media stays referenced and is NOT reported as unreferenced.
    env.services.evidence.soft_delete_evidence(env.ws, env.evidence.id)
    after = env.integrity.check()
    assert after.counts["table_evidence_media"] == 1
    unreferenced = {item.entity_id for item in after.issues if item.code == "MEDIA_UNREFERENCED"}
    assert unreferenced == {str(spare.id)}
    assert str(env.capture.id) not in unreferenced and str(env.album.id) not in unreferenced


def test_soft_deleted_person_and_media_keep_existing_semantics(env):
    archived = env.services.people.create(env.ws, "将被归档")
    gallery = env.services.media.import_file(env.ws, make_image(env.originals / "gone.png",
                                                                (5, 5, 200)))
    env.services.people.add_gallery(env.ws, archived.id, gallery.id)
    env.services.people.soft_delete(env.ws, archived.id)   # removes its PersonMedia rows
    report = env.integrity.check()
    assert report.ok is True
    unreferenced = {item.entity_id for item in report.issues if item.code == "MEDIA_UNREFERENCED"}
    # Archiving releases the binding, so the media becomes unreferenced - and the
    # archived person itself is never an error.
    assert unreferenced == {str(gallery.id)}
    assert not [code for code in codes(report) if code.endswith("WORKSPACE_MISMATCH")]


# ------------------------------------------------- 6. workspace mismatches (bare)
def test_composite_fk_workspace_mismatches_are_detected(env, raw_connection):
    """The six checks whose parent carries its own workspace_id really can fire.

    Every parent row below EXISTS - it simply lives in the other workspace - so the
    child row is a genuine workspace mismatch and not a dangling reference. Each
    child is written on a dedicated connection with ``foreign_keys=OFF``, which is
    the only way SQLite admits such a row.
    """
    other = env.services.workspaces.create("另一个 Workspace")
    with env.runtime.engine.begin() as connection:
        outside_person_a, outside_person_b = sorted((uuid4(), uuid4()))
        for identifier, name in ((outside_person_a, "外部甲"), (outside_person_b, "外部乙")):
            statement(connection, PersonModel, id=identifier.hex, workspace_id=other.id.hex,
                      display_name=name, real_name=None, notes=None,
                      created_at=text_value(NOW), updated_at=text_value(NOW), deleted_at=None)
        outside_relationship = uuid4()
        statement(connection, RelationshipModel, id=outside_relationship.hex,
                  workspace_id=other.id.hex, person_a_id=outside_person_a.hex,
                  person_b_id=outside_person_b.hex, relationship_type="外部关系",
                  directionality="UNDIRECTED", created_at=text_value(NOW),
                  updated_at=text_value(NOW), deleted_at=None)
        outside_event = uuid4()
        statement(connection, EventModel, id=outside_event.hex, workspace_id=other.id.hex,
                  title="外部事件", event_type="Meeting", record_kind="FACT",
                  occurred_on=NOW.date().isoformat(), started_at=None, ended_at=None,
                  created_at=text_value(NOW), updated_at=text_value(NOW), deleted_at=None)
        outside_evidence = uuid4()
        statement(connection, EvidenceModel, id=outside_evidence.hex, workspace_id=other.id.hex,
                  event_id=outside_event.hex, kind="MANUAL", title="外部证据", content_text=None,
                  annotation=None, source_type="MANUAL", source_label=None,
                  source_reference=None, captured_at=None, created_at=text_value(NOW),
                  updated_at=text_value(NOW), deleted_at=None)
    # Both media rows exist, so every child below is a genuine workspace mismatch
    # rather than a dangling binding.
    shared_media = add_media_row(env, other.id)
    local_media = add_media_row(env, env.ws)
    bad_history, bad_participant, bad_evidence, bad_binding, bad_person_media = (uuid4(), uuid4(),
                                                                                uuid4(), uuid4(),
                                                                                uuid4())

    statement(raw_connection, RelationshipHistoryModel, id=bad_history.hex,
              workspace_id=env.ws.hex, relationship_id=outside_relationship.hex, event_id=None,
              state="ACTIVE", effective_at=text_value(NOW), note=None,
              created_at=text_value(NOW))
    statement(raw_connection, EventParticipantModel, id=bad_participant.hex,
              workspace_id=env.ws.hex, event_id=outside_event.hex, person_id=outside_person_a.hex,
              role=None, note=None, created_at=text_value(NOW))
    statement(raw_connection, EvidenceModel, id=bad_evidence.hex, workspace_id=env.ws.hex,
              event_id=outside_event.hex, kind="MANUAL", title="错位证据", content_text=None,
              annotation=None, source_type="MANUAL", source_label=None, source_reference=None,
              captured_at=None, created_at=text_value(NOW), updated_at=text_value(NOW),
              deleted_at=None)
    statement(raw_connection, EvidenceMediaModel, id=bad_binding.hex, workspace_id=env.ws.hex,
              evidence_id=outside_evidence.hex, media_id=shared_media.hex, role="ATTACHMENT",
              created_at=text_value(NOW))
    statement(raw_connection, PersonMediaModel, id=bad_person_media.hex, workspace_id=env.ws.hex,
              person_id=outside_person_a.hex, media_id=local_media.hex, role="GALLERY",
              sort_order=9, created_at=text_value(NOW))

    report = env.integrity.check()
    assert report.ok is False
    found = set(codes(report, ERROR))
    assert found >= {"RELATIONSHIP_HISTORY_WORKSPACE_MISMATCH",
                     "EVENT_PARTICIPANT_WORKSPACE_MISMATCH", "EVIDENCE_WORKSPACE_MISMATCH",
                     "EVIDENCE_MEDIA_WORKSPACE_MISMATCH",
                     "PERSON_MEDIA_WORKSPACE_MISMATCH"}, sorted(found)
    # These rows are mismatches only: their parents exist, so the only foreign key
    # violations in the report are the ones the schema cannot avoid.
    assert "PERSON_MEDIA_DANGLING" not in found and "EVIDENCE_MEDIA_DANGLING" not in found
    # The finding names the child, both workspaces and the parent table.
    issue = {item.entity_id: item for item in report.errors
             if item.code == "RELATIONSHIP_HISTORY_WORKSPACE_MISMATCH"}[str(bad_history)]
    assert issue.entity_type == "relationship_history"
    assert str(env.ws) in issue.detail and str(other.id) in issue.detail
    assert "relationship.workspace_id" in issue.detail
    # Every damaged row survives the audit untouched.
    with transaction(env.runtime.sessions) as session:
        assert session.scalar(select(func.count()).select_from(RelationshipHistoryModel)) == 2
        assert session.scalar(select(func.count()).select_from(PersonMediaModel)) == 2
    # The very same rows are visible in a single-workspace audit of the parent side.
    scoped = env.integrity.check(other.id)
    assert scoped.counts["table_evidence"] == 1
    assert scoped.counts["table_media"] == 1


def test_person_and_media_workspace_mismatch_fire_only_without_a_parent_row(env, raw_connection):
    """The two checks whose parent IS the workspace row cannot disagree otherwise.

    ``person`` and ``media`` copy ``workspace.id`` itself, so a row that names an
    existing workspace is by construction in that workspace; there is no second
    column to disagree about. The checks are still real queries over real rows and
    they do report the one reachable shape - a child whose workspace row does not
    exist at all - which the ``PRAGMA foreign_key_check`` pass reports as well.
    """
    ghost_workspace = uuid4()
    stranded_person, stranded_media = uuid4(), uuid4()
    with env.runtime.engine.begin() as connection:
        assert connection.scalar(select(func.count()).select_from(WorkspaceModel).where(
            WorkspaceModel.id == ghost_workspace)) == 0
    statement(raw_connection, PersonModel, id=stranded_person.hex, workspace_id=ghost_workspace.hex,
              display_name="无主人物", real_name=None, notes=None,
              created_at=text_value(NOW), updated_at=text_value(NOW), deleted_at=None)
    statement(raw_connection, MediaModel, id=stranded_media.hex, workspace_id=ghost_workspace.hex,
              sha256="c" * 64, mime_type="image/png", size_bytes=4, width=2, height=2,
              storage_path=f"media/{ghost_workspace}/cc/{'c' * 64}", created_at=text_value(NOW),
              updated_at=text_value(NOW), deleted_at=None)

    report = env.integrity.check()
    assert report.ok is False
    for code, entity_id, entity_type in (
            ("PERSON_WORKSPACE_MISMATCH", stranded_person, "person"),
            ("MEDIA_WORKSPACE_MISMATCH", stranded_media, "media")):
        issue = assert_single(report, code, ERROR)
        assert issue.entity_id == str(entity_id) and issue.entity_type == entity_type
        assert issue.detail.endswith("workspace.workspace_id=-")   # no parent row exists
    # Each stranded row is also a foreign key violation: the two facts are reported
    # side by side, one PRAGMA row each, because they answer different questions.
    assert [item.code for item in report.errors].count("FOREIGN_KEY_VIOLATION") == 2
    # The queries really run and really return nothing when every child can be
    # reconciled with its workspace: a second, healthy workspace reports neither.
    healthy = env.services.workspaces.create("健康的 Workspace")
    env.services.people.create(healthy.id, "正常人物")
    clean = env.integrity.check(healthy.id)
    assert clean.counts["table_person"] == 1
    assert not [code for code in codes(clean) if code.endswith("WORKSPACE_MISMATCH")]
    # The PRAGMA pass is the one deliberate exception to the workspace filter: a
    # broken foreign key belongs to the database, not to one Workspace, so it is
    # reported even here instead of being hidden by the filter.
    assert clean.counts["foreign_key_check_scope"] == "database"
    assert "FOREIGN_KEY_VIOLATION" in codes(clean, ERROR)
    assert all(item.entity_type in ("person", "media") for item in clean.errors)


def test_a_clean_database_never_reports_a_workspace_mismatch(env):
    # On a database written only through the composite foreign keys the eight
    # self-joins really are empty; the damaged cases above prove they can see one.
    report = env.integrity.check()
    assert not [code for code in codes(report) if code in WORKSPACE_MISMATCH_CODES]
    assert report.counts["table_person_alias"] == 1   # the query ran over real rows
    assert report.errors == ()


# ------------------------------------------------------------ 3b. dangling links
def test_dangling_media_bindings_are_errors(env, raw_connection):
    ghost = uuid4()
    statement(raw_connection, PersonMediaModel, id=uuid4().hex, workspace_id=env.ws.hex,
              person_id=env.person.id.hex, media_id=ghost.hex, role="GALLERY", sort_order=7,
              created_at=text_value(NOW))
    statement(raw_connection, EvidenceMediaModel, id=uuid4().hex, workspace_id=env.ws.hex,
              evidence_id=env.evidence.id.hex, media_id=ghost.hex, role="ATTACHMENT",
              created_at=text_value(NOW))
    report = env.integrity.check()
    assert report.ok is False
    assert "PERSON_MEDIA_DANGLING" in codes(report, ERROR)
    assert "EVIDENCE_MEDIA_DANGLING" in codes(report, ERROR)
    assert str(ghost) in assert_single(report, "PERSON_MEDIA_DANGLING").detail
    assert str(ghost) in assert_single(report, "EVIDENCE_MEDIA_DANGLING").detail
    # A dangling binding is not "unreferenced media": no Media row exists at all.
    assert "MEDIA_UNREFERENCED" not in codes(report)


# ------------------------------------------------------- 7. analysis provenance
def test_dangling_analysis_source_is_an_error(env, raw_connection):
    run = add_run(env, env.ws)
    ghost = uuid4()
    statement(raw_connection, AnalysisPersonSourceModel, id=uuid4().hex,
              analysis_run_id=run.hex, workspace_id=env.ws.hex, target_id=ghost.hex,
              created_at=text_value(NOW))
    report = env.integrity.check()
    assert report.ok is False
    issue = assert_single(report, "ANALYSIS_SOURCE_DANGLING", ERROR)
    assert issue.entity_type == "analysis_person_source"
    assert f"analysis_run_id={run}" in issue.detail and str(ghost) in issue.detail
    # A healthy source row of the same run adds no further finding.
    with env.runtime.engine.begin() as connection:
        statement(connection, AnalysisEvidenceSourceModel, id=uuid4().hex,
                  analysis_run_id=run.hex, workspace_id=env.ws.hex,
                  target_id=env.evidence.id.hex, created_at=text_value(NOW))
    healthy = env.integrity.check()
    assert len([item for item in healthy.errors
                if item.code == "ANALYSIS_SOURCE_DANGLING"]) == 1
    assert healthy.counts["table_analysis_evidence_source"] == 1


# ------------------------------------------------------- 8./11. memory integrity
def test_manual_memory_with_analysis_reference_is_invalid(env):
    # A MANUAL row carrying a run is refused by CHECK(source_reference_valid), so it
    # is written with the constraint bypassed - the shape a corrupted or externally
    # edited file has, and the only shape this check can ever see.
    run = add_run(env, env.ws)
    bad = add_memory(env, env.ws, source_type="MANUAL", source_analysis_id=run,
                     content="错位来源的记忆", unchecked=True)
    report = env.integrity.check()
    assert report.ok is False
    issue = assert_single(report, "MEMORY_SOURCE_INVALID", ERROR)
    assert issue.entity_id == str(bad) and "MANUAL" in issue.message
    assert "source_analysis_id" in issue.detail
    assert "错位来源的记忆" not in issue.message and "错位来源的记忆" not in issue.detail


def test_analysis_memory_without_run_is_invalid(env):
    bad = add_memory(env, env.ws, source_type="ANALYSIS", source_analysis_id=None,
                     content="缺少来源的记忆", unchecked=True)
    report = env.integrity.check()
    assert report.ok is False
    issue = assert_single(report, "MEMORY_SOURCE_INVALID", ERROR)
    assert issue.entity_id == str(bad) and "ANALYSIS" in issue.message
    assert "缺少来源的记忆" not in json.dumps(report.to_mapping(), ensure_ascii=False)


def test_memory_pointing_at_a_missing_run_is_invalid(env):
    bad = add_memory(env, env.ws, source_type="ANALYSIS", source_analysis_id=uuid4(),
                     content="指向不存在分析的记忆", orphan_reference=True)
    report = env.integrity.check()
    assert report.ok is False
    issues = [item for item in report.errors if item.code == "MEMORY_SOURCE_INVALID"]
    assert {item.entity_id for item in issues} == {str(bad)}
    assert "不存在的 AnalysisRun" in issues[0].message
    assert "指向不存在分析的记忆" not in json.dumps(report.to_mapping(), ensure_ascii=False)


def test_memory_referencing_a_run_of_another_workspace_is_invalid(env):
    # The run exists, but in the other workspace: the audit looks the run up by id
    # alone, so this is reported rather than silently accepted as "same workspace".
    other = env.services.workspaces.create("另一个 Workspace")
    foreign_run = add_run(env, other.id)
    bad = add_memory(env, env.ws, source_type="ANALYSIS", source_analysis_id=foreign_run,
                     content="跨 Workspace 的来源记忆", orphan_reference=True)
    report = env.integrity.check()
    assert report.ok is False
    issues = [item for item in report.errors
              if item.code == "MEMORY_SOURCE_INVALID" and item.entity_id == str(bad)]
    assert len(issues) == 1
    assert str(foreign_run) in issues[0].detail


def test_valid_analysis_memory_is_clean(env):
    run = add_run(env, env.ws)
    memory = add_memory(env, env.ws, source_type="ANALYSIS", source_analysis_id=run,
                        content="来自分析的记忆", person_id=env.person.id, kind="PREFERENCE")
    report = env.integrity.check()
    assert report.ok is True and report.errors == ()
    assert report.counts["table_memory"] == 1
    assert str(memory) not in codes(report)


def test_memory_state_violations_are_errors(env):
    # Both shapes are refused by CHECK constraints, so they are written with those
    # constraints bypassed - the state a corrupted database is actually in.
    #
    # ``confirmed_at IS NULL`` is deliberately NOT tested: migration 0008 declares
    # that column NOT NULL, so no row can reach the audit with it empty. The check
    # stays in the service because a NULL there would break the "a Memory exists
    # only after confirmation" rule and must be reported if a future schema or a
    # third-party writer ever allows it.
    backwards = add_memory(env, env.ws, valid_from=NOW, valid_until=NOW - timedelta(days=1),
                           content="顺序错误的记忆", unchecked=True)
    untrimmed = add_memory(env, env.ws, content=" 未去空白的记忆 ", unchecked=True)
    report = env.integrity.check()
    assert report.ok is False
    issues = [item for item in report.errors if item.code == "MEMORY_STATE_INVALID"]
    assert {item.entity_id for item in issues} == {str(backwards), str(untrimmed)}
    reasons = " ".join(item.message for item in issues)
    assert "valid_until 早于 valid_from" in reasons
    assert "content 为空、超长或与 trim 后不同" in reasons
    assert all(item.severity == ERROR for item in issues)
    # Only a length is reported, never the body itself.
    assert all((item.detail or "").startswith("content_length=") for item in issues)
    # The rows are untouched: the audit reports the state instead of fixing it.
    with transaction(env.runtime.sessions) as session:
        assert session.scalar(select(func.count()).select_from(MemoryModel)) == 2
        assert session.scalar(select(MemoryModel.content).where(
            MemoryModel.id == untrimmed)) == " 未去空白的记忆 "
    assert env.integrity.check().counts["table_memory"] == 2


# ------------------------------------------------------------------ 9. expiry
def test_expired_memory_is_a_warning_without_leaking_its_content(env):
    secret = "极其私密的过期记忆正文"
    expired = add_memory(env, env.ws, content=secret, kind="CONTEXT",
                         valid_from=NOW - timedelta(days=30),
                         valid_until=NOW - timedelta(days=1))
    current = add_memory(env, env.ws, content="仍然有效的记忆",
                         valid_until=NOW + timedelta(days=30))
    future = add_memory(env, env.ws, content="尚未生效的记忆",
                        valid_from=NOW + timedelta(days=1))
    deleted = add_memory(env, env.ws, content="已删除的过期记忆",
                         valid_until=NOW - timedelta(days=5), deleted_at=NOW)
    report = env.integrity.check()
    assert report.ok is True
    issue = assert_single(report, "MEMORY_EXPIRED", WARNING)
    assert issue.entity_id == str(expired) and issue.entity_type == "memory"
    # Deleted, future and still-valid memories are never reported as expired.
    reported = {item.entity_id for item in report.warnings if item.code == "MEMORY_EXPIRED"}
    assert reported == {str(expired)}
    assert {str(current), str(future), str(deleted)}.isdisjoint(reported)
    # No memory body ever reaches a message, a detail or the serialized report.
    serialized = json.dumps(report.to_mapping(), ensure_ascii=False)
    for body in (secret, "仍然有效的记忆", "尚未生效的记忆", "已删除的过期记忆"):
        assert body not in serialized
        assert all(body not in item.message and body not in (item.detail or "")
                   for item in report.issues)


def test_analysis_run_state_warning_never_modifies_the_run(env):
    stuck = add_run(env, env.ws, status="SUCCEEDED", completed_at=None, duration_ms=None)
    report = env.integrity.check()
    assert report.ok is True
    issue = assert_single(report, "ANALYSIS_RUN_STATE_INVALID", WARNING)
    assert issue.entity_id == str(stuck)
    with transaction(env.runtime.sessions) as session:
        row = session.execute(select(AnalysisRunModel.status, AnalysisRunModel.completed_at)
                              .where(AnalysisRunModel.id == stuck)).one()
        assert row.status == "SUCCEEDED" and row.completed_at is None


# ------------------------------------------------------------- 10. read-only proof
def test_audit_changes_no_row_and_no_file(env):
    add_memory(env, env.ws, content="审计前存在的记忆", person_id=env.person.id, kind="FACT")
    add_run(env, env.ws)
    orphan = env.store.media_dir / "media" / str(env.ws) / "aa" / ("a" * 64)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan bytes")
    rows_before = table_snapshot(env)
    files_before = media_files(env)

    report = env.integrity.check()
    assert "ORPHAN_MEDIA_FILE" in codes(report)
    assert table_snapshot(env) == rows_before
    assert media_files(env) == files_before
    assert orphan.read_bytes() == b"orphan bytes"
    # A second audit of an unchanged state produces exactly the same findings.
    assert codes(env.integrity.check()) == codes(report)
    assert table_snapshot(env) == rows_before


def test_audit_of_a_damaged_database_still_writes_nothing(env, raw_connection):
    statement(raw_connection, PersonModel, id=uuid4().hex, workspace_id=uuid4().hex,
              display_name="孤儿人物", real_name=None, notes=None,
              created_at=text_value(NOW), updated_at=text_value(NOW), deleted_at=None)
    file_of(env, env.album).unlink()
    rows_before = table_snapshot(env)
    files_before = media_files(env)
    report = env.integrity.check()
    assert report.ok is False
    assert table_snapshot(env) == rows_before       # no repair, no cleanup, no INSERT
    assert media_files(env) == files_before         # the missing file stays missing
    assert "FOREIGN_KEY_VIOLATION" in codes(report)  # the FK break is still there
    assert "MEDIA_FILE_MISSING" in codes(report)


# ------------------------------------------------------ verify_media_files=False
def test_database_only_audit_never_touches_the_filesystem(env, monkeypatch):
    class ExplodingStore:
        """Any use of the store is a test failure, not a skipped check."""

        def path_for(self, *args, **kwargs):
            raise AssertionError("verify_media_files=False 时不得访问媒体存储。")

        @property
        def media_dir(self):
            raise AssertionError("verify_media_files=False 时不得读取媒体根目录。")

    monkeypatch.setattr(env, "store", ExplodingStore())
    service = DataIntegrityService(env.runtime.sessions, media_store=env.store)
    report = service.check(verify_media_files=False)
    assert report.ok is True and report.errors == ()
    assert report.counts["media_files_checked"] == 0
    assert report.counts["media_hashes_checked"] == 0
    assert report.counts["media_files_skipped"] == 2
    assert report.counts["orphan_files"] == 0
    assert report.counts["table_media"] == 2          # database checks still ran
    assert "ORPHAN_MEDIA_FILE" not in codes(report)
    assert "MEDIA_STORE_UNAVAILABLE" not in codes(report)


def test_missing_media_store_is_a_warning_not_a_crash(env):
    service = DataIntegrityService(env.runtime.sessions)
    report = service.check()
    assert report.ok is True and report.errors == ()
    issue = assert_single(report, "MEDIA_STORE_UNAVAILABLE", WARNING)
    assert issue.detail is not None
    assert report.counts["media_files_checked"] == 0
    assert report.counts["orphan_files"] == 0
    # Database checks are unaffected by the missing store.
    assert report.counts["table_person"] == 2
    assert "MEDIA_FILE_MISSING" not in codes(report)
    assert json.loads(json.dumps(report.to_mapping(), ensure_ascii=False))["ok"] is True


# ------------------------------------------------------------------ report shape
def test_report_dataclasses_are_frozen_and_serializable(env):
    report = env.integrity.check()
    assert isinstance(report.errors, tuple) and isinstance(report.warnings, tuple)
    with pytest.raises(Exception):
        report.ok = False
    issue = IntegrityIssue(code="X", severity=ERROR, message="m")
    assert issue.entity_id is None and issue.detail is None
    assert issue.to_mapping() == {"code": "X", "severity": ERROR, "message": "m",
                                  "entity_type": None, "entity_id": None, "detail": None}
    assert set(report.to_mapping()) >= {"ok", "checked_at", "errors", "warnings", "counts",
                                        "summary"}
    assert all(item.severity in (ERROR, WARNING) for item in report.issues)
