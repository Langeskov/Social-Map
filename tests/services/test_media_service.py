"""Phase 3 media integration contracts using migrated temporary databases only.

Fault tests explicitly simulate business failure, pre-commit rollback, unavailable
fresh proof, and an ambiguous *successful commit followed by an exception*.
No real application database, media, or private profile is read by this module.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime, timezone
import hashlib
import logging
import multiprocessing
from pathlib import Path
from threading import Barrier, Lock, get_ident

import pytest
from PIL import Image
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError

from app.config.settings import AppConfig
from app.domain.errors import ConflictError, DomainError, MediaValidationError
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.services.media import MediaService
from app.services.repositories import sql_repositories
from app.services.unit_of_work import transaction
from app.storage.models import MediaModel, PersonMediaModel, PersonModel
from app.storage.repositories import SQLMediaRepository
from app.storage.session import ThreadBoundSession
from worker_helpers import import_media_worker


@dataclass
class MediaEnvironment:
    config: AppConfig
    runtime: object
    services: object
    workspace: object


@pytest.fixture
def media_env(tmp_path):
    config = AppConfig(data_dir=tmp_path / "isolated-data")
    runtime = initialize_database(config)
    services = create_application_services(config, runtime.sessions)
    yield MediaEnvironment(config, runtime, services, services.initial_workspace)
    runtime.close()


@pytest.fixture
def images(tmp_path):
    """Fresh Pillow originals outside app-owned storage, with distinct hashes."""
    originals = tmp_path / "originals"
    originals.mkdir()
    result = []
    for index, color in enumerate(((12, 34, 56), (100, 80, 60), (90, 20, 10))):
        path = originals / f"image-{index}.png"
        Image.new("RGB", (19 + index, 13 + index), color).save(path, "PNG")
        result.append(path)
    return tuple(result)


def rows(env):
    """Fresh read proves persisted row counts, including inactive entities."""
    with transaction(env.runtime.sessions) as session:
        return tuple(session.scalar(select(func.count()).select_from(model))
                     for model in (PersonModel, MediaModel, PersonMediaModel))


def all_media(env, workspace_id=None):
    with transaction(env.runtime.sessions) as session:
        return sql_repositories(session).media.list(
            workspace_id or env.workspace.id, include_deleted=True)


def effective_files(env):
    store = env.services.media.store
    return sorted(path for path in store.media_dir.rglob("*")
                  if path.is_file()
                  and store.staging_dir not in path.parents
                  and store.locks_dir not in path.parents)


def assert_no_staging(env):
    assert list(env.services.media.store.staging_dir.iterdir()) == []


def assert_utc(*entities):
    for entity in entities:
        for field in fields(entity):
            value = getattr(entity, field.name)
            if isinstance(value, datetime):
                assert value.tzinfo == timezone.utc, field.name
                assert value.utcoffset().total_seconds() == 0


def inject_link_failure(monkeypatch):
    """Failure happens after Media/new Person flush, before transaction commit."""
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic business link failure")
    monkeypatch.setattr("app.services.media.attach_media", fail)


@contextmanager
def fail_next_write_commit(env):
    """Exactly one real SQLAlchemy before_commit failure, never read commits."""
    observed = {"failures": 0, "read_commits": 0, "write_sessions": []}

    def before_commit(session):
        if session.get_bind() is not env.runtime.engine:
            return
        if not session.info.get("ris_write"):
            observed["read_commits"] += 1
            return
        observed["write_sessions"].append(session)
        if observed["failures"] == 0:
            observed["failures"] += 1
            raise RuntimeError("synthetic before_commit failure")

    event.listen(ThreadBoundSession, "before_commit", before_commit)
    try:
        yield observed
    finally:
        event.remove(ThreadBoundSession, "before_commit", before_commit)


def test_import_hash_metadata_dimensions_and_original_unchanged(media_env, images):
    env = media_env
    source = images[0]
    original = source.read_bytes()
    before = source.stat()
    media = env.services.media.import_file(env.workspace.id, source)
    assert media.sha256 == hashlib.sha256(original).hexdigest()
    assert media.mime_type == "image/png"
    assert media.size_bytes == len(original)
    assert (media.width, media.height) == (19, 13)
    final = env.services.media.original_path(env.workspace.id, media.id)
    assert final != source
    assert final.read_bytes() == original
    assert source.read_bytes() == original
    assert source.stat().st_mtime_ns == before.st_mtime_ns
    assert rows(env) == (0, 1, 0)
    assert_utc(media, env.services.media.get(env.workspace.id, media.id))
    assert_no_staging(env)


def test_dedup_same_scope_and_separate_files_across_scopes(media_env, images, tmp_path):
    env = media_env
    copied = tmp_path / "same-content-renamed.png"
    copied.write_bytes(images[0].read_bytes())
    first = env.services.media.import_file(env.workspace.id, images[0])
    duplicate = env.services.media.import_file(env.workspace.id, copied)
    second_workspace = env.services.workspaces.create("Isolated scope")
    other = env.services.media.import_file(second_workspace.id, images[0])
    assert duplicate.id == first.id
    assert duplicate.storage_path == first.storage_path
    assert other.sha256 == first.sha256
    assert other.id != first.id
    assert other.storage_path != first.storage_path
    first_path = env.services.media.original_path(env.workspace.id, first.id)
    other_path = env.services.media.original_path(second_workspace.id, other.id)
    assert first_path != other_path
    assert first_path.read_bytes() == other_path.read_bytes() == images[0].read_bytes()
    assert rows(env) == (0, 2, 0)
    assert len(effective_files(env)) == 2
    assert_no_staging(env)


def test_shared_media_two_people_and_avatar_replacement_gallery_many(media_env, images):
    env = media_env
    people, service, ws = env.services.people, env.services.media, env.workspace.id
    first = people.create(ws, "Fixture person A")
    second = people.create(ws, "Fixture person B")
    shared = service.import_file(ws, images[0], person_id=first.id, role="AVATAR")
    duplicate = service.import_file(ws, images[0], person_id=second.id, role="AVATAR")
    assert duplicate.id == shared.id
    replacement = service.import_file(ws, images[1])
    third = service.import_file(ws, images[2])
    old_link = people.detail(ws, first.id).images[0].link
    new_avatar = people.set_avatar(ws, first.id, replacement.id)
    assert people.set_avatar(ws, first.id, replacement.id).id == new_avatar.id
    gallery_a = people.add_gallery(ws, first.id, shared.id)
    gallery_b = people.add_gallery(ws, first.id, third.id)
    assert people.add_gallery(ws, first.id, shared.id).id == gallery_a.id
    detail = people.detail(ws, first.id)
    avatars = [image for image in detail.images if image.link.role == "AVATAR"]
    gallery = [image for image in detail.images if image.link.role == "GALLERY"]
    assert len(avatars) == 1 and avatars[0].media.id == replacement.id
    assert old_link.id not in {image.link.id for image in detail.images}
    assert {image.media.id for image in gallery} == {shared.id, third.id}
    assert (gallery_a.sort_order, gallery_b.sort_order) == (0, 1)
    assert people.detail(ws, second.id).images[0].media.id == shared.id
    assert rows(env) == (2, 3, 4)
    assert len(effective_files(env)) == 3
    assert_utc(first, second, *(image.link for image in detail.images))


def test_service_rejects_cross_workspace_media_person_and_link(media_env, images):
    env = media_env
    people, service, ws = env.services.people, env.services.media, env.workspace.id
    other = env.services.workspaces.create("Other scope")
    local_person = people.create(ws, "Local fixture")
    foreign_person = people.create(other.id, "Foreign fixture")
    media = service.import_file(ws, images[0], person_id=local_person.id)
    link = people.detail(ws, local_person.id).images[0].link
    before = rows(env)
    with pytest.raises(DomainError):
        people.set_avatar(other.id, foreign_person.id, media.id)
    with pytest.raises(DomainError):
        people.add_gallery(other.id, foreign_person.id, media.id)
    with pytest.raises(DomainError):
        service.import_file(ws, images[1], person_id=foreign_person.id)
    with pytest.raises(DomainError):
        service.get(other.id, media.id)
    with pytest.raises(DomainError):
        service.original_path(other.id, media.id)
    with pytest.raises(DomainError):
        service.soft_delete(other.id, media.id)
    with pytest.raises(DomainError):
        people.remove_media_reference(other.id, foreign_person.id, link.id)
    assert rows(env) == before
    assert len(effective_files(env)) == 1
    assert_no_staging(env)


def test_unbind_and_person_soft_delete_remove_refs_not_files(media_env, images):
    env = media_env
    people, service, ws = env.services.people, env.services.media, env.workspace.id
    first = people.create(ws, "First fixture")
    second = people.create(ws, "Second fixture")
    media = service.import_file(ws, images[0], person_id=first.id)
    second_link = people.set_avatar(ws, second.id, media.id)
    path = service.original_path(ws, media.id)
    first_link = people.detail(ws, first.id).images[0].link
    with pytest.raises(DomainError):
        people.remove_media_reference(ws, second.id, first_link.id)
    people.remove_media_reference(ws, first.id, first_link.id)
    assert not people.detail(ws, first.id).images
    assert people.detail(ws, second.id).images[0].link.id == second_link.id
    assert path.read_bytes() == images[0].read_bytes()
    with pytest.raises(ConflictError):
        service.soft_delete(ws, media.id)
    people.soft_delete(ws, second.id)
    with transaction(env.runtime.sessions) as session:
        repositories = sql_repositories(session)
        assert not repositories.person_media.has_references(ws, media.id, active_only=False)
        deleted = repositories.people.get(ws, second.id, include_deleted=True)
    assert deleted.deleted_at is not None
    assert deleted.updated_at == deleted.deleted_at
    assert_utc(deleted)
    assert rows(env) == (2, 1, 0)
    assert path.exists()
    assert service.get(ws, media.id).deleted_at is None


def test_media_soft_delete_requires_no_refs_retains_file_reimport_same_id(media_env, images):
    env = media_env
    ws = env.workspace.id
    person = env.services.people.create(ws, "Fixture")
    media = env.services.media.import_file(ws, images[0], person_id=person.id)
    path = env.services.media.original_path(ws, media.id)
    with pytest.raises(ConflictError):
        env.services.media.soft_delete(ws, media.id)
    assert env.services.media.get(ws, media.id).deleted_at is None
    link = env.services.people.detail(ws, person.id).images[0].link
    env.services.people.remove_media_reference(ws, person.id, link.id)
    env.services.media.soft_delete(ws, media.id)
    with pytest.raises(DomainError):
        env.services.media.get(ws, media.id)
    archived = all_media(env)[0]
    assert archived.id == media.id
    assert archived.deleted_at is not None
    assert archived.updated_at == archived.deleted_at
    assert_utc(archived)
    assert path.read_bytes() == images[0].read_bytes()
    active = env.services.media.import_file(ws, images[0])
    assert active.id == archived.id
    assert active.created_at == archived.created_at
    assert active.deleted_at is None
    assert active.updated_at >= archived.updated_at
    assert active.storage_path == archived.storage_path
    assert rows(env) == (1, 1, 0)
    assert_utc(active)
    assert_no_staging(env)


def test_person_with_avatar_creation_is_one_atomic_success(media_env, images):
    env = media_env
    person = env.services.people.create(env.workspace.id, "Avatar fixture", avatar_path=images[0])
    detail = env.services.people.detail(env.workspace.id, person.id)
    assert detail.person == person
    assert len(detail.images) == 1
    assert detail.images[0].link.role == "AVATAR"
    assert detail.images[0].link.person_id == person.id
    assert rows(env) == (1, 1, 1)
    assert env.services.media.original_path(env.workspace.id, detail.images[0].media.id).exists()
    assert_no_staging(env)


@pytest.mark.parametrize("payload", [b"", b"not a valid image", b"\x89PNG\r\n\x1a\ncorrupt"])
def test_bad_image_leaves_no_records_or_temporary_files(media_env, tmp_path, payload):
    env = media_env
    bad = tmp_path / "broken.png"
    bad.write_bytes(payload)
    with pytest.raises(MediaValidationError):
        env.services.media.import_file(env.workspace.id, bad)
    with pytest.raises(MediaValidationError):
        env.services.people.create(env.workspace.id, "Invalid avatar fixture", avatar_path=bad)
    assert rows(env) == (0, 0, 0)
    assert effective_files(env) == []
    assert_no_staging(env)
    assert bad.read_bytes() == payload


def test_business_link_failure_rolls_back_new_media_and_cleans_file(media_env, images, monkeypatch):
    env = media_env
    person = env.services.people.create(env.workspace.id, "Fixture")
    inject_link_failure(monkeypatch)
    with pytest.raises(RuntimeError, match="synthetic business link failure"):
        env.services.media.import_file(env.workspace.id, images[0], person_id=person.id)
    assert rows(env) == (1, 0, 0)
    assert effective_files(env) == []
    assert_no_staging(env)


def test_create_person_avatar_link_failure_rolls_back_all_rows(media_env, images, monkeypatch):
    env = media_env
    inject_link_failure(monkeypatch)
    with pytest.raises(RuntimeError, match="synthetic business link failure"):
        env.services.people.create(env.workspace.id, "Atomic fixture", avatar_path=images[0])
    assert rows(env) == (0, 0, 0)
    assert env.services.people.list(env.workspace.id) == []
    assert effective_files(env) == []
    assert_no_staging(env)


@pytest.mark.parametrize("new_person", [False, True])
def test_before_commit_write_failure_rolls_back_and_compensates(media_env, images, new_person):
    env = media_env
    with fail_next_write_commit(env) as observed:
        # These actual read transaction commits must not consume the injection.
        assert env.services.workspaces.get(env.workspace.id) == env.workspace
        assert env.services.people.list(env.workspace.id) == []
        with pytest.raises(RuntimeError, match="synthetic before_commit failure"):
            if new_person:
                env.services.people.create(env.workspace.id, "Atomic fixture", avatar_path=images[0])
            else:
                env.services.media.import_file(env.workspace.id, images[0])
        assert observed["failures"] == 1
        assert observed["read_commits"] >= 3
        assert rows(env) == (0, 0, 0)
        assert effective_files(env) == []
        # Failure is one-shot: next real write succeeds while hook is installed.
        imported = env.services.media.import_file(env.workspace.id, images[0])
        assert imported.id == env.services.media.get(env.workspace.id, imported.id).id
        assert observed["failures"] == 1
        assert len(observed["write_sessions"]) == 2
    assert rows(env) == (0, 1, 0)
    assert_no_staging(env)


@pytest.mark.parametrize("failure", ["business", "commit"])
def test_failed_import_never_removes_preexisting_file(media_env, images, monkeypatch, failure):
    env = media_env
    person = env.services.people.create(env.workspace.id, "Fixture")
    media = env.services.media.import_file(env.workspace.id, images[0])
    path = env.services.media.original_path(env.workspace.id, media.id)
    if failure == "business":
        inject_link_failure(monkeypatch)
        with pytest.raises(RuntimeError):
            env.services.media.import_file(env.workspace.id, images[0], person_id=person.id)
    else:
        with fail_next_write_commit(env) as observed:
            with pytest.raises(RuntimeError, match="synthetic before_commit failure"):
                env.services.media.import_file(env.workspace.id, images[0], person_id=person.id)
            assert observed["failures"] == 1
    assert path.read_bytes() == images[0].read_bytes()
    assert env.services.media.get(env.workspace.id, media.id) == media
    assert rows(env) == (1, 1, 0)
    assert_no_staging(env)


def test_existing_published_file_without_db_row_is_not_compensated(media_env, images, monkeypatch):
    """created_new=False is protective even when a prior operation left an orphan."""
    env = media_env
    store = env.services.media.store
    staged = store.stage(images[0])
    try:
        with store.content_lock(env.workspace.id, staged.sha256):
            receipt = store.publish(env.workspace.id, staged)
    finally:
        store.discard(staged)
    path = store.path_for(receipt.storage_path)
    person = env.services.people.create(env.workspace.id, "Fixture")
    inject_link_failure(monkeypatch)
    with pytest.raises(RuntimeError):
        env.services.media.import_file(env.workspace.id, images[0], person_id=person.id)
    assert rows(env) == (1, 0, 0)
    assert path.read_bytes() == images[0].read_bytes()
    assert_no_staging(env)


def test_fresh_proof_read_failure_conservatively_retains_new_file(media_env, images, monkeypatch, caplog):
    """Explicit transaction-wrapper simulation: only compensation read fails."""
    import app.services.media as media_module
    env = media_env
    person = env.services.people.create(env.workspace.id, "Fixture")
    actual_transaction = media_module.transaction
    sessions = []
    state = {"business_failed": False, "proof_failed": False}

    def fail_link(*args, **kwargs):
        state["business_failed"] = True
        raise RuntimeError("synthetic business failure")

    @contextmanager
    def unavailable_proof(factory, **kwargs):
        with actual_transaction(factory, **kwargs) as session:
            sessions.append(session)
            if state["business_failed"]:
                state["proof_failed"] = True
                raise RuntimeError("synthetic fresh proof unavailable")
            yield session

    monkeypatch.setattr(media_module, "attach_media", fail_link)
    monkeypatch.setattr(media_module, "transaction", unavailable_proof)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="synthetic business failure"):
            env.services.media.import_file(env.workspace.id, images[0], person_id=person.id)
    assert state["proof_failed"]
    assert len(sessions) == 2 and sessions[0] is not sessions[1]
    assert rows(env) == (1, 0, 0)
    files = effective_files(env)
    assert len(files) == 1 and files[0].read_bytes() == images[0].read_bytes()
    records = [record for record in caplog.records if record.name == "app.services.media"]
    assert any("file retained: type=RuntimeError" in record.getMessage() for record in records)
    assert all(record.exc_info is None for record in records)
    assert all("notes" not in record.getMessage().lower() for record in caplog.records)
    assert all("synthetic fresh proof unavailable" not in record.getMessage() for record in caplog.records)
    assert_no_staging(env)


def test_simulated_commit_then_raise_preserves_durable_person_media_file(media_env, images, monkeypatch):
    """Ambiguous/partial outward success, not an actual SQLite partial transaction.

    Wrapper delegates a real complete commit and then raises to simulate an
    acknowledgement failure. Compensation must use fresh durable proof instead
    of assuming the outward exception means rollback.
    """
    import app.services.media as media_module
    env = media_env
    real_write_operation = media_module.write_operation
    writes, proofs = [], []
    original_factory = env.services.media._repositories

    def recording_repositories(session):
        if session.info.get("ris_write"):
            writes.append(session)
        else:
            proofs.append(session)
        return original_factory(session)

    @contextmanager
    def committed_then_raise(factory):
        with real_write_operation(factory) as session:
            yield session
        raise RuntimeError("synthetic post-commit acknowledgement failure")

    monkeypatch.setattr(env.services.media, "_repositories", recording_repositories)
    monkeypatch.setattr(media_module, "write_operation", committed_then_raise)
    with pytest.raises(RuntimeError, match="synthetic post-commit acknowledgement failure"):
        env.services.people.create(env.workspace.id, "Committed fixture", avatar_path=images[0])
    assert rows(env) == (1, 1, 1)
    assert len(writes) == 1
    assert proofs[-1] is not writes[0]
    person = env.services.people.list(env.workspace.id)[0]
    image = env.services.people.detail(env.workspace.id, person.id).images[0]
    assert image.link.role == "AVATAR"
    assert env.services.media.original_path(env.workspace.id, image.media.id).read_bytes() == images[0].read_bytes()
    assert_no_staging(env)


def test_unique_collision_uses_real_constraint_and_fresh_session_retry(media_env, images, monkeypatch, caplog):
    env = media_env
    existing = env.services.media.import_file(env.workspace.id, images[0])
    actual_get = SQLMediaRepository.get_by_hash
    actual_save = SQLMediaRepository.save
    reads, collisions = [], []
    state = {"hidden": False}

    def hide_first_existing(self, workspace_id, sha256, **kwargs):
        reads.append(self.session)
        found = actual_get(self, workspace_id, sha256, **kwargs)
        if not state["hidden"] and found is not None:
            state["hidden"] = True
            return None
        return found

    def observe_real_save(self, entity):
        try:
            return actual_save(self, entity)
        except IntegrityError:
            collisions.append(self.session)
            raise

    monkeypatch.setattr(SQLMediaRepository, "get_by_hash", hide_first_existing)
    monkeypatch.setattr(SQLMediaRepository, "save", observe_real_save)
    with caplog.at_level(logging.ERROR):
        returned = env.services.media.import_file(env.workspace.id, images[0])
    assert state["hidden"]
    assert len(collisions) == 1
    assert len(reads) == 3
    assert len({id(session) for session in reads}) == 3
    assert reads[0] is collisions[0]
    assert returned == existing
    assert rows(env) == (0, 1, 0)
    assert len(effective_files(env)) == 1
    database_records = [record for record in caplog.records if record.name == "app.storage.database"]
    assert any("type=IntegrityError" in record.getMessage() for record in database_records)
    assert all("SQL/parameters omitted" in record.getMessage() for record in database_records)
    assert all("notes" not in record.getMessage().lower() and record.exc_info is None
               for record in database_records)
    assert_no_staging(env)


def test_two_threads_import_same_content_one_media_and_owned_sessions(media_env, images, monkeypatch):
    env = media_env
    barrier = Barrier(2)
    actual_stage = env.services.media.store.stage
    captured, capture_lock = [], Lock()
    real_factory = env.runtime.sessions

    def tracked_factory():
        session = real_factory()
        with capture_lock:
            captured.append((get_ident(), session))
        return session

    # Build one application composition shared by worker threads, never a Session.
    services = create_application_services(env.config, tracked_factory)
    captured.clear()

    def simultaneous_stage(source):
        staged = actual_stage(source)
        barrier.wait(timeout=20)
        return staged

    monkeypatch.setattr(services.media.store, "stage", simultaneous_stage)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(services.media.import_file, env.workspace.id, images[0])
                   for _ in range(2)]
        imported = [future.result(timeout=30) for future in futures]
    assert imported[0].id == imported[1].id
    assert imported[0].storage_path == imported[1].storage_path
    assert len({thread for thread, _ in captured}) == 2
    assert len({id(session) for _, session in captured}) == len(captured)
    assert len(captured) >= 4
    assert all(session._owner_thread == thread for thread, session in captured)
    assert rows(env) == (0, 1, 0)
    assert len(effective_files(env)) == 1
    assert_no_staging(env)


def test_spawn_two_processes_import_same_hash_one_media(media_env, images):
    env = media_env
    # Parent fixture has already run the migration; no Session crosses spawn.
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(2), context.Queue()
    workers = [context.Process(target=import_media_worker,
                               args=(str(env.config.storage_paths.data_dir), str(images[0]),
                                     str(env.workspace.id), barrier, results))
               for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=45)
        assert all(not worker.is_alive() for worker in workers), "spawn import did not complete"
        assert [worker.exitcode for worker in workers] == [0, 0]
        outcomes = [results.get(timeout=5) for _ in workers]
        assert all(outcome[0] == "ok" for outcome in outcomes), outcomes
        assert outcomes[0][1:] == outcomes[1][1:]
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
            if worker.pid is not None and not worker.is_alive():
                worker.close()
        results.close()
        results.join_thread()
    assert rows(env) == (0, 1, 0)
    assert len(effective_files(env)) == 1
    assert_no_staging(env)


def test_soft_deleted_entities_and_all_times_reload_as_utc(media_env, images):
    env = media_env
    ws = env.workspace.id
    person = env.services.people.create(ws, "Reload fixture", avatar_path=images[0])
    alias = env.services.people.add_alias(ws, person.id, "Fixture alias")
    active = env.services.people.detail(ws, person.id)
    media = active.images[0].media
    assert_utc(env.workspace, person, alias, media, active.images[0].link)
    env.services.people.soft_delete(ws, person.id)
    env.services.media.soft_delete(ws, media.id)
    env.services.workspaces.soft_delete(ws)
    # Dispose/reopen against the same migrated temporary database.
    env.runtime.close()
    restarted = initialize_database(env.config)
    try:
        with transaction(restarted.sessions) as session:
            repositories = sql_repositories(session)
            archived_workspace = repositories.workspaces.get(ws, include_deleted=True)
            archived_person = repositories.people.get(ws, person.id, include_deleted=True)
            archived_media = repositories.media.get(ws, media.id, include_deleted=True)
            assert not repositories.person_media.has_references(ws, media.id, active_only=False)
        for entity in (archived_workspace, archived_person, archived_media):
            assert entity.deleted_at is not None
            assert entity.updated_at == entity.deleted_at
        assert_utc(archived_workspace, archived_person, archived_media)
        assert archived_person.created_at == person.created_at
        assert archived_media.created_at == media.created_at
        assert env.services.media.store.path_for(archived_media.storage_path).read_bytes() == images[0].read_bytes()
    finally:
        restarted.close()
