"""Phase 4 public Service contracts, real migrations and atomic failure injection."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
import logging
from threading import Barrier, get_ident
from uuid import UUID, uuid4

import pytest
from PIL import Image
from sqlalchemy import event, func, select

from app.config.settings import AppConfig
from app.domain.entities import Relationship, RelationshipHistory
from app.domain.errors import ConflictError, DomainError, NotFoundError, WorkspaceMismatchError
from app.services.database import initialize_database
from app.services.media import MediaService
from app.services.person import PersonService
from app.services.relationship import RelationshipService, RelationshipView
from app.services.relationship_repositories import sql_relationship_repositories
from app.services.unit_of_work import transaction
from app.services.workspace import WorkspaceService
from app.storage.media_store import MediaStore
from app.storage.models import RelationshipModel, RelationshipHistoryModel, PersonMediaModel
from app.storage.session import ThreadBoundSession


@dataclass
class Clock:
    instant: datetime = datetime(2025, 4, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.instant


@dataclass
class Environment:
    config: object
    runtime: object
    workspaces: object
    people: object
    service: object
    clock: Clock
    workspace: object
    a: object
    b: object

    @property
    def ws(self):
        return self.workspace.id

    def create(self, kind="Friend", **kwargs):
        return self.service.create(self.ws, self.a.id, self.b.id, kind, **kwargs)


@pytest.fixture
def env(tmp_path):
    config = AppConfig(data_dir=tmp_path / "relationship-data")
    runtime = initialize_database(config)
    workspaces, people, clock = WorkspaceService(runtime.sessions), PersonService(runtime.sessions), Clock()
    workspace = workspaces.create("Relationship fixture")
    a, b = people.create(workspace.id, "Fixture A"), people.create(workspace.id, "Fixture B")
    service = RelationshipService(runtime.sessions, clock=clock)
    try:
        yield Environment(config, runtime, workspaces, people, service, clock, workspace, a, b)
    finally:
        runtime.close()


def counts(env):
    with transaction(env.runtime.sessions) as session:
        return tuple(session.scalar(select(func.count()).select_from(model))
                     for model in (RelationshipModel, RelationshipHistoryModel))


def persisted(env, id):
    with transaction(env.runtime.sessions) as session:
        repositories = sql_relationship_repositories(session)
        return (repositories.relationships.get(env.ws, id, include_deleted=True),
                tuple(repositories.histories.list_by_relationship(env.ws, id)))


@contextmanager
def fail_next_write_commit(env):
    observed = {"failures": 0, "read_commits": 0}

    def before_commit(session):
        if session.get_bind() is not env.runtime.engine:
            return
        if not session.info.get("ris_write"):
            observed["read_commits"] += 1
        elif observed["failures"] == 0:
            observed["failures"] += 1
            raise RuntimeError("synthetic before_commit failure")

    event.listen(ThreadBoundSession, "before_commit", before_commit)
    try:
        yield observed
    finally:
        event.remove(ThreadBoundSession, "before_commit", before_commit)


def failing_history_factory(observed):
    """Inject via the supported factory, after real history flush (no fake DB)."""
    def factory(session):
        repositories = sql_relationship_repositories(session)

        class FailingHistory:
            def __getattr__(self, name):
                return getattr(repositories.histories, name)

            def add(self, entity):
                observed.append((entity, session.info.get("ris_write")))
                repositories.histories.add(entity)
                raise RuntimeError("synthetic history persistence failure")

        return replace(repositories, histories=FailingHistory())
    return factory


def test_create_trim_normalization_directions_types_and_public_no_update(env):
    hi, lo = sorted((env.a.id, env.b.id), key=lambda id: id.int, reverse=True)
    first = env.service.create(env.ws, hi, lo, "  Friend  ", initial_state=" active ")
    assert type(first) is Relationship
    assert (first.person_a_id, first.person_b_id) == (lo, hi)
    assert first.relationship_type == "Friend"
    assert first.directionality == "UNDIRECTED"
    assert first.created_at == first.updated_at == env.clock.instant
    assert not hasattr(env.service, "update")
    lower = env.create("friend")
    colleague = env.create("Colleague")
    directed = env.service.create(env.ws, hi, lo, "Friend", "DIRECTED")
    reverse = env.service.create(env.ws, lo, hi, "Friend", "DIRECTED")
    assert (directed.person_a_id, directed.person_b_id) == (hi, lo)
    assert (reverse.person_a_id, reverse.person_b_id) == (lo, hi)
    assert len({first.id, lower.id, colleague.id, directed.id, reverse.id}) == 5
    assert counts(env) == (5, 5)
    assert {view.relationship.id for view in env.service.list_by_person(env.ws, env.a.id)} == {first.id, lower.id, colleague.id, directed.id, reverse.id}
    assert len(env.service.list_by_person(env.ws, env.b.id)) == 5
    view = env.service.current(env.ws, first.id)
    assert view.current_state == "ACTIVE" and view.current_history == env.service.history(env.ws, first.id)[0]
    assert view.other(env.a.id).id == env.b.id and view.other(env.b.id).id == env.a.id
    with pytest.raises(DomainError):
        view.other(uuid4())


@pytest.mark.parametrize("endpoint", ["a", "b"])
def test_create_rejects_foreign_and_soft_deleted_endpoints(env, endpoint):
    other = env.workspaces.create("Foreign fixture")
    foreign = env.people.create(other.id, "Foreign endpoint")
    a, b = (foreign.id, env.b.id) if endpoint == "a" else (env.a.id, foreign.id)
    with pytest.raises(WorkspaceMismatchError):
        env.service.create(env.ws, a, b, "Friend")
    local = env.a if endpoint == "a" else env.b
    env.people.soft_delete(env.ws, local.id)
    with pytest.raises(WorkspaceMismatchError):
        env.create()
    assert counts(env) == (0, 0)


def test_self_relation_and_missing_workspace_are_rejected(env):
    with pytest.raises(DomainError):
        env.service.create(env.ws, env.a.id, env.a.id, "Friend")
    with pytest.raises(DomainError):
        env.service.create(uuid4(), env.a.id, env.b.id, "Friend")
    assert counts(env) == (0, 0)


@pytest.mark.parametrize("kind", ["", "   ", "x" * 201, None, 123])
def test_invalid_type_rejected_without_writes(env, kind):
    with pytest.raises(DomainError):
        env.create(kind)
    assert counts(env) == (0, 0)


@pytest.mark.parametrize("direction", ["directed", " DIRECTED ", "OTHER", None])
def test_invalid_direction_is_not_silently_normalized(env, direction):
    with pytest.raises(DomainError):
        env.create(directionality=direction)
    assert counts(env) == (0, 0)


@pytest.mark.parametrize("state", ["ACTIVE", "PAUSED", "ENDED"])
def test_supported_initial_and_appended_states_normalize(env, state):
    relation = env.create(initial_state=" " + state.lower() + " ")
    assert env.service.current(env.ws, relation.id).current_state == state
    env.clock.instant += timedelta(seconds=1)
    entry = env.service.add_history(env.ws, relation.id, " " + state.lower() + " ")
    assert entry.state == state
    assert len(env.service.history(env.ws, relation.id)) == 2


@pytest.mark.parametrize("state", ["FUTURE_STATE", "", "  ", None, 123])
def test_service_rejects_future_states_but_domain_can_represent_them(env, state):
    with pytest.raises(DomainError):
        env.create(initial_state=state)
    relation = env.create()
    before = persisted(env, relation.id)
    with pytest.raises(DomainError):
        env.service.add_history(env.ws, relation.id, state)
    assert persisted(env, relation.id) == before
    domain_history = RelationshipHistory(workspace_id=env.ws, relationship_id=relation.id,
                                         state="FUTURE_STATE", effective_at=env.clock.instant)
    assert domain_history.state == "FUTURE_STATE"


def test_duplicate_active_ended_and_deleted_requires_original_identity_recovery(env):
    relation = env.create()
    with pytest.raises(ConflictError):
        env.service.create(env.ws, env.b.id, env.a.id, " Friend ")
    env.clock.instant += timedelta(seconds=1)
    env.service.add_history(env.ws, relation.id, "ENDED")
    with pytest.raises(ConflictError):
        env.create()
    history = env.service.history(env.ws, relation.id)
    env.clock.instant += timedelta(seconds=1)
    deleted = env.service.soft_delete(env.ws, relation.id)
    assert deleted.deleted_at == env.clock.instant
    with pytest.raises(ConflictError, match="恢复"):
        env.create()
    assert env.service.history(env.ws, relation.id, include_deleted=True) == history
    restored = env.service.restore(env.ws, relation.id)
    assert restored.id == relation.id and restored.deleted_at is None
    assert env.service.current(env.ws, relation.id).current_state == "ENDED"
    assert env.service.history(env.ws, relation.id) == history
    env.clock.instant += timedelta(seconds=1)
    active = env.service.add_history(env.ws, relation.id, "ACTIVE")
    assert active.relationship_id == relation.id
    assert env.service.current(env.ws, relation.id).current_state == "ACTIVE"
    assert counts(env) == (1, 3)


def test_soft_delete_restore_are_idempotent_and_do_not_invent_history(env):
    relation = env.create(initial_state="PAUSED")
    history = env.service.history(env.ws, relation.id)
    env.clock.instant += timedelta(days=1)
    deleted = env.service.soft_delete(env.ws, relation.id)
    env.clock.instant += timedelta(days=1)
    assert env.service.soft_delete(env.ws, relation.id) == deleted
    for operation in (env.service.get, env.service.current, env.service.history):
        with pytest.raises(NotFoundError):
            operation(env.ws, relation.id)
    assert env.service.list_by_workspace(env.ws) == []
    assert env.service.list_by_person(env.ws, env.a.id) == []
    assert env.service.get(env.ws, relation.id, include_deleted=True) == deleted
    audit = env.service.list_by_workspace(env.ws, include_deleted=True)
    assert len(audit) == 1 and audit[0].current_state == "PAUSED"
    assert env.service.list_by_person(env.ws, env.b.id, include_deleted=True) == audit
    with pytest.raises(NotFoundError):
        env.service.add_history(env.ws, relation.id, "ACTIVE")
    restored = env.service.restore(env.ws, relation.id)
    env.clock.instant += timedelta(days=1)
    assert env.service.restore(env.ws, relation.id) == restored
    assert env.service.history(env.ws, relation.id) == history
    assert env.service.current(env.ws, relation.id).current_state == "PAUSED"


@pytest.mark.parametrize("endpoint", ["a", "b"])
def test_restore_and_history_writes_fail_if_endpoint_archived(env, endpoint):
    relation = env.create()
    env.service.soft_delete(env.ws, relation.id)
    env.people.soft_delete(env.ws, getattr(env, endpoint).id)
    before = persisted(env, relation.id)
    with pytest.raises(WorkspaceMismatchError):
        env.service.restore(env.ws, relation.id)
    with pytest.raises(NotFoundError):
        env.service.add_history(env.ws, relation.id, "ACTIVE")
    assert persisted(env, relation.id) == before
    view = env.service.current(env.ws, relation.id, include_deleted=True)
    assert not view.endpoints_active
    assert view.current_state == "ACTIVE"
    assert view.relationship.deleted_at is not None


def test_all_reads_and_writes_respect_scope_including_deleted_audit(env):
    relation = env.create()
    foreign = env.workspaces.create("Other scope")
    env.service.soft_delete(env.ws, relation.id)
    before = persisted(env, relation.id)
    for operation in (env.service.get, env.service.current, env.service.history):
        for include_deleted in (False, True):
            with pytest.raises(NotFoundError):
                operation(foreign.id, relation.id, include_deleted=include_deleted)
    for operation in (env.service.soft_delete, env.service.restore):
        with pytest.raises(NotFoundError):
            operation(foreign.id, relation.id)
    with pytest.raises(NotFoundError):
        env.service.add_history(foreign.id, relation.id, "ENDED")
    assert env.service.list_by_workspace(foreign.id, include_deleted=True) == []
    with pytest.raises(NotFoundError):
        env.service.list_by_person(foreign.id, env.a.id, include_deleted=True)
    assert persisted(env, relation.id) == before


def test_future_initial_and_future_history_only_apply_at_effective_instant(env):
    now = env.clock.instant
    future = now + timedelta(days=2)
    relation = env.create(effective_at=future)
    assert env.service.current(env.ws, relation.id).current_state is None
    assert env.service.current(env.ws, relation.id).current_history is None
    assert env.service.current(env.ws, relation.id, as_of=future).current_state == "ACTIVE"
    assert env.service.list_by_person(env.ws, env.a.id)[0].current_state is None
    assert env.service.list_by_workspace(env.ws, as_of=future)[0].current_state == "ACTIVE"
    paused = env.service.add_history(env.ws, relation.id, "PAUSED", effective_at=future + timedelta(days=1))
    assert env.service.current(env.ws, relation.id, as_of=future).current_state == "ACTIVE"
    assert env.service.current(env.ws, relation.id, as_of=paused.effective_at).current_state == "PAUSED"
    assert len(env.service.history(env.ws, relation.id)) == 2
    env.clock.instant = paused.effective_at
    assert env.service.current(env.ws, relation.id).current_history == paused


def test_future_append_preserves_existing_current_until_boundary(env):
    relation = env.create()
    original = env.service.current(env.ws, relation.id).current_history
    future = env.clock.instant + timedelta(days=1)
    entry = env.service.add_history(env.ws, relation.id, "ENDED", effective_at=future)
    assert env.service.current(env.ws, relation.id).current_history == original
    assert env.service.list_by_person(env.ws, env.a.id)[0].current_state == "ACTIVE"
    assert env.service.list_by_workspace(env.ws)[0].current_state == "ACTIVE"
    assert env.service.current(env.ws, relation.id, as_of=future - timedelta(microseconds=1)).current_state == "ACTIVE"
    assert env.service.current(env.ws, relation.id, as_of=future).current_history == entry
    env.clock.instant = future
    assert env.service.current(env.ws, relation.id).current_state == "ENDED"


def test_backdated_new_history_does_not_replace_later_current(env):
    start = env.clock.instant
    relation = env.create()
    env.clock.instant += timedelta(days=3)
    ended = env.service.add_history(env.ws, relation.id, "ENDED")
    env.clock.instant += timedelta(days=1)
    backdated = env.service.add_history(env.ws, relation.id, "PAUSED", effective_at=start + timedelta(days=1))
    assert env.service.current(env.ws, relation.id).current_history == ended
    assert env.service.current(env.ws, relation.id, as_of=start + timedelta(days=2)).current_history == backdated
    assert [entry.state for entry in env.service.history(env.ws, relation.id)] == ["ACTIVE", "PAUSED", "ENDED"]
    assert env.service.get(env.ws, relation.id).updated_at == env.clock.instant


def test_service_same_effective_ties_use_created_then_uuid_deterministically(env):
    relation = env.create(effective_at=env.clock.instant - timedelta(days=1))
    original = env.service.history(env.ws, relation.id)[0]
    effective = env.clock.instant
    entries = [replace(original, id=UUID(int=50), state="ENDED", effective_at=effective, created_at=effective),
               replace(original, id=UUID(int=10), state="PAUSED", effective_at=effective, created_at=effective),
               replace(original, id=UUID(int=1), state="ACTIVE", effective_at=effective, created_at=effective + timedelta(seconds=1))]
    with transaction(env.runtime.sessions) as session:
        repo = sql_relationship_repositories(session).histories
        for entry in entries:
            repo.add(entry)
    assert env.service.history(env.ws, relation.id) == (original, entries[1], entries[0], entries[2])
    assert env.service.current(env.ws, relation.id).current_history == entries[2]
    assert env.service.current(env.ws, relation.id, as_of=effective - timedelta(microseconds=1)).current_history == original


def test_factory_history_failure_rolls_back_relationship_and_history(env):
    observed = []
    broken = RelationshipService(env.runtime.sessions, failing_history_factory(observed), clock=env.clock)
    with pytest.raises(RuntimeError, match="history persistence"):
        broken.create(env.ws, env.a.id, env.b.id, "Failure")
    assert len(observed) == 1 and observed[0][1] is True
    assert counts(env) == (0, 0)
    assert env.service.list_by_workspace(env.ws, include_deleted=True) == []
    # Failed write did not reserve the natural identity.
    assert env.create("Failure").id


def test_factory_history_append_failure_keeps_existing_updated_at_and_history(env):
    relation = env.create()
    before = persisted(env, relation.id)
    env.clock.instant += timedelta(days=1)
    observed = []
    broken = RelationshipService(env.runtime.sessions, failing_history_factory(observed), clock=env.clock)
    with pytest.raises(RuntimeError, match="history persistence"):
        broken.add_history(env.ws, relation.id, "PAUSED")
    assert len(observed) == 1
    assert persisted(env, relation.id) == before
    assert env.service.get(env.ws, relation.id).updated_at == relation.updated_at


@pytest.mark.parametrize("operation", ["create", "add_history", "soft_delete", "restore"])
def test_before_commit_write_failure_rolls_back_entire_operation(env, operation):
    relation = None
    if operation != "create":
        relation = env.create()
        if operation == "restore":
            env.service.soft_delete(env.ws, relation.id)
    before_counts = counts(env)
    before = None if relation is None else persisted(env, relation.id)
    env.clock.instant += timedelta(days=1)
    with fail_next_write_commit(env) as observed:
        # A read commit must not consume the injected write failure.
        env.service.list_by_workspace(env.ws, include_deleted=True)
        with pytest.raises(RuntimeError, match="before_commit"):
            if operation == "create":
                env.create()
            elif operation == "add_history":
                env.service.add_history(env.ws, relation.id, "ENDED")
            else:
                getattr(env.service, operation)(env.ws, relation.id)
    assert observed["failures"] == 1 and observed["read_commits"] >= 1
    assert counts(env) == before_counts
    if relation is not None:
        assert persisted(env, relation.id) == before


def test_two_concurrent_writers_same_natural_key_one_conflict_no_duplicate(env):
    barrier = Barrier(2)
    sessions = []

    def factory(session):
        sessions.append((session, get_ident(), session.info.get("ris_write")))
        return sql_relationship_repositories(session)

    def writer(reverse):
        # Synchronize before BEGIN IMMEDIATE; never wait holding a writer lock.
        barrier.wait(timeout=10)
        service = RelationshipService(env.runtime.sessions, factory, clock=env.clock)
        a, b = (env.b.id, env.a.id) if reverse else (env.a.id, env.b.id)
        try:
            return service.create(env.ws, a, b, "Concurrent")
        except ConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(writer, reverse) for reverse in (False, True)]
        results = [future.result(timeout=20) for future in futures]
    assert sum(isinstance(result, Relationship) for result in results) == 1
    assert sum(isinstance(result, ConflictError) for result in results) == 1
    assert len(sessions) == 2
    assert len({id(item[0]) for item in sessions}) == 2
    assert len({item[1] for item in sessions}) == 2
    assert all(item[2] for item in sessions)
    assert counts(env) == (1, 1)


def test_restart_utc_and_closed_session_dtos_remain_readable(env):
    local = datetime(2025, 4, 1, 20, 0, tzinfo=timezone(timedelta(hours=8)))
    env.clock.instant = local
    relation = env.create(note="retained fixture", effective_at=local)
    env.clock.instant += timedelta(days=1)
    entry = env.service.add_history(env.ws, relation.id, "PAUSED", effective_at=local + timedelta(hours=1))
    snapshot = env.service.current(env.ws, relation.id)
    listed = env.service.list_by_workspace(env.ws)
    history = env.service.history(env.ws, relation.id)
    env.runtime.close()
    reopened = initialize_database(env.config)
    try:
        service = RelationshipService(reopened.sessions, clock=env.clock)
        assert service.current(env.ws, relation.id) == snapshot
        assert service.list_by_person(env.ws, env.b.id) == listed
        assert service.history(env.ws, relation.id) == history
    finally:
        reopened.close()
    assert type(snapshot) is RelationshipView
    assert snapshot.current_state == "PAUSED" and snapshot.current_history == entry
    assert snapshot.other(env.a.id).display_name == env.b.display_name
    assert snapshot.endpoints_active
    for entity in (snapshot.relationship, snapshot.current_history, *history):
        for field in fields(entity):
            value = getattr(entity, field.name)
            if isinstance(value, datetime):
                assert value.tzinfo == timezone.utc
    assert relation.created_at == local.astimezone(timezone.utc)


def test_person_archive_unlinks_media_but_keeps_file_relationship_and_history(env, tmp_path):
    source = tmp_path / "fixture.png"
    Image.new("RGB", (12, 10), (20, 40, 60)).save(source)
    original_bytes = source.read_bytes()
    media_service = MediaService(env.runtime.sessions, MediaStore(env.config.storage_paths.data_dir))
    media = media_service.import_file(env.ws, source, person_id=env.a.id, role="AVATAR")
    env.people.add_gallery(env.ws, env.a.id, media.id)
    survivor_link = env.people.add_gallery(env.ws, env.b.id, media.id)
    path = media_service.original_path(env.ws, media.id)
    relation = env.create()
    history = env.service.history(env.ws, relation.id)
    env.people.soft_delete(env.ws, env.a.id)
    with transaction(env.runtime.sessions) as session:
        links = list(session.scalars(select(PersonMediaModel)))
    assert [link.id for link in links] == [survivor_link.id]
    assert path.exists() and path.read_bytes() == original_bytes
    assert source.read_bytes() == original_bytes
    assert media_service.get(env.ws, media.id).deleted_at is None
    saved, saved_history = persisted(env, relation.id)
    assert saved == relation and saved.deleted_at is None
    assert saved_history == history and len(saved_history) == 1
    assert env.service.list_by_workspace(env.ws) == []
    assert env.service.list_by_person(env.ws, env.b.id) == []
    with pytest.raises(NotFoundError):
        env.service.get(env.ws, relation.id)
    with pytest.raises(NotFoundError):
        env.service.list_by_person(env.ws, env.a.id, include_deleted=True)
    audited = env.service.current(env.ws, relation.id, include_deleted=True)
    assert not audited.endpoints_active and audited.current_state == "ACTIVE"
    assert env.service.history(env.ws, relation.id, include_deleted=True) == history
    assert env.service.list_by_workspace(env.ws, include_deleted=True) == [audited]
    assert env.service.list_by_person(env.ws, env.b.id, include_deleted=True) == [audited]
    with pytest.raises(NotFoundError):
        env.service.add_history(env.ws, relation.id, "ENDED")
    # Audit deletion remains available, but recovery cannot bypass the endpoint archive.
    env.service.soft_delete(env.ws, relation.id)
    with pytest.raises(WorkspaceMismatchError):
        env.service.restore(env.ws, relation.id)
    assert persisted(env, relation.id)[1] == history


def test_failure_and_conflict_logs_never_include_private_type_or_note(env, caplog):
    secret_type, secret_note = "PRIVATE_TYPE_8d92", "PRIVATE_NOTE_7c63"
    caplog.set_level(logging.DEBUG, logger="app")
    relation = env.create(secret_type, note=secret_note)
    with pytest.raises(ConflictError):
        env.create(secret_type, note=secret_note)
    observed = []
    broken = RelationshipService(env.runtime.sessions, failing_history_factory(observed), clock=env.clock)
    with pytest.raises(RuntimeError):
        broken.add_history(env.ws, relation.id, "PAUSED", note=secret_note)
    with fail_next_write_commit(env):
        with pytest.raises(RuntimeError):
            env.service.add_history(env.ws, relation.id, "ENDED", note=secret_note)
    assert caplog.records
    assert secret_type not in caplog.text and secret_note not in caplog.text
    assert all(secret_type not in record.getMessage() and secret_note not in record.getMessage()
               for record in caplog.records)
