"""Five SQL adapters against the migrated SQLite database, never create_all."""
from dataclasses import fields, replace
from datetime import date, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.domain.entities import Media, Person, PersonAlias, PersonMedia, Workspace
from app.services.repositories import sql_repositories
from app.services.unit_of_work import transaction
from app.storage.database import DatabaseError
from app.storage.models import PersonAliasModel, PersonMediaModel, WorkspaceModel


@pytest.mark.parametrize("index,attribute,changes", [
    (0, "workspaces", {"name": "Renamed", "description": "updated"}),
    (1, "people", {"display_name": "Updated", "birthday": date(1990, 1, 2), "notes": None}),
    (2, "aliases", {"alias": "Updated Alias", "alias_type": "FORMER", "note": None}),
    (3, "media", {"storage_path": "media/updated.png", "size_bytes": 99, "width": None, "height": None}),
    (4, "person_media", {"role": "AVATAR", "sort_order": 5}),
])
def test_save_update_get_domain_snapshots(runtime, records, now, index, attribute, changes):
    workspace, person, _, _, _ = records
    entity = records[index]
    if index != 4:
        changes = {**changes, "updated_at": now + timedelta(days=1)}
    updated = replace(entity, **changes)
    with transaction(runtime.sessions) as session:
        repository = getattr(sql_repositories(session), attribute)
        saved = repository.save(updated)
        loaded = repository.get(updated.id) if index == 0 else repository.get(workspace.id, updated.id)
        assert loaded == updated == saved
        assert saved is not updated and saved is not loaded
        listed = (repository.list() if index == 0 else repository.list(workspace.id, person.id)
                  if index in (2, 4) else repository.list(workspace.id))
        assert listed == [updated]
    # Every scalar is still readable after the transaction's Session is closed.
    assert type(loaded) is type(entity)
    assert isinstance(loaded.id, UUID)
    assert loaded.created_at.tzinfo == timezone.utc
    assert {field.name: getattr(loaded, field.name) for field in fields(loaded)} == {
        field.name: getattr(updated, field.name) for field in fields(updated)}
    with transaction(runtime.sessions) as session:
        repository = getattr(sql_repositories(session), attribute)
        assert (repository.get(updated.id) if index == 0 else repository.get(workspace.id, updated.id)) == updated


@pytest.mark.parametrize("index,attribute", [(0, "workspaces"), (1, "people"), (3, "media")])
def test_soft_deleted_rows_are_opt_in(runtime, records, now, index, attribute):
    workspace = records[0]
    deleted = replace(records[index], deleted_at=now)
    with transaction(runtime.sessions) as session:
        repository = getattr(sql_repositories(session), attribute)
        repository.save(deleted)
        args = (deleted.id,) if index == 0 else (workspace.id, deleted.id)
        list_args = () if index == 0 else (workspace.id,)
        assert repository.get(*args) is None
        assert repository.get(*args, include_deleted=True) == deleted
        assert repository.list(*list_args) == []
        assert repository.list(*list_args, include_deleted=True) == [deleted]
        if index == 0:
            assert repository.get_by_name(deleted.name) is None
            assert repository.get_by_name(deleted.name, include_deleted=True) == deleted
        elif index == 3:
            assert repository.get_by_hash(workspace.id, deleted.sha256) is None
            assert repository.get_by_hash(workspace.id, deleted.sha256, include_deleted=True) == deleted


def test_deleted_workspace_children_are_available_only_for_audit(runtime, records, now):
    workspace, person, alias, media, link = records
    with transaction(runtime.sessions) as session:
        repositories = sql_repositories(session)
        repositories.workspaces.save(replace(workspace, deleted_at=now))
        assert repositories.people.get(workspace.id, person.id) is None
        assert repositories.people.list(workspace.id) == []
        assert repositories.people.get(workspace.id, person.id, include_deleted=True) == person
        assert repositories.people.list(workspace.id, include_deleted=True) == [person]
        assert repositories.media.get(workspace.id, media.id) is None
        assert repositories.media.get_by_hash(workspace.id, media.sha256) is None
        assert repositories.media.list(workspace.id) == []
        assert repositories.media.get(workspace.id, media.id, include_deleted=True) == media
        assert repositories.media.get_by_hash(workspace.id, media.sha256, include_deleted=True) == media
        assert repositories.media.list(workspace.id, include_deleted=True) == [media]
        assert repositories.aliases.get(workspace.id, alias.id) is None
        assert repositories.aliases.list(workspace.id, person.id) == []
        assert repositories.person_media.get(workspace.id, link.id) is None
        assert repositories.person_media.list(workspace.id, person.id) == []
        assert not repositories.person_media.has_references(workspace.id, media.id)
        assert repositories.person_media.has_references(workspace.id, media.id, active_only=False)


@pytest.mark.parametrize("inactive", ["person", "media"])
def test_active_associations_and_all_references(runtime, records, now, inactive):
    workspace, person, alias, media, link = records
    with transaction(runtime.sessions) as session:
        repositories = sql_repositories(session)
        assert repositories.person_media.has_references(workspace.id, media.id)
        if inactive == "person":
            repositories.people.save(replace(person, deleted_at=now))
            assert repositories.aliases.get(workspace.id, alias.id) is None
            assert repositories.aliases.list(workspace.id, person.id) == []
            assert repositories.people.list(workspace.id, "Alias") == []
            assert repositories.people.list(workspace.id, "Alias", include_deleted=True) == [replace(person, deleted_at=now)]
        else:
            repositories.media.save(replace(media, deleted_at=now))
            assert repositories.aliases.get(workspace.id, alias.id) == alias
        assert repositories.person_media.get(workspace.id, link.id) is None
        assert repositories.person_media.list(workspace.id, person.id) == []
        assert not repositories.person_media.has_references(workspace.id, media.id, active_only=True)
        assert repositories.person_media.has_references(workspace.id, media.id, active_only=False)
        assert not repositories.person_media.has_references(workspace.id, uuid4(), active_only=False)


def test_scope_isolation_for_all_reads_and_deletions(runtime, records, now):
    workspace, person, alias, media, link = records
    other = Workspace(id=uuid4(), name="Other", created_at=now, updated_at=now)
    with transaction(runtime.sessions) as session:
        repositories = sql_repositories(session)
        repositories.workspaces.save(other)
        for repository, entity in ((repositories.people, person), (repositories.aliases, alias),
                                   (repositories.media, media), (repositories.person_media, link)):
            assert repository.get(other.id, entity.id) is None
        assert repositories.people.get(other.id, person.id, include_deleted=True) is None
        assert repositories.media.get(other.id, media.id, include_deleted=True) is None
        assert repositories.people.list(other.id, "Same") == []
        assert repositories.people.list(other.id, include_deleted=True) == []
        assert repositories.media.list(other.id, include_deleted=True) == []
        assert repositories.media.get_by_hash(other.id, media.sha256, include_deleted=True) is None
        assert repositories.aliases.list(other.id, person.id) == []
        assert repositories.person_media.list(other.id, person.id) == []
        assert not repositories.person_media.has_references(other.id, media.id)
        assert not repositories.person_media.has_references(other.id, media.id, active_only=False)
        repositories.aliases.remove(other.id, alias.id)
        repositories.person_media.remove(other.id, link.id)
        repositories.person_media.remove_for_person(other.id, person.id)
        assert repositories.aliases.get(workspace.id, alias.id) == alias
        assert repositories.person_media.get(workspace.id, link.id) == link


def test_same_name_people_and_alias_search_exists_not_join(runtime, records, now):
    workspace, person, _, _, _ = records
    other = replace(person, id=uuid4(), real_name="Different Real")
    with transaction(runtime.sessions) as session:
        repositories = sql_repositories(session)
        repositories.people.save(other)
        for text in ("Shared Alias", "Shared Alias two"):
            for target in (person, other):
                repositories.aliases.save(PersonAlias(id=uuid4(), workspace_id=workspace.id,
                    person_id=target.id, alias=text, created_at=now, updated_at=now))
        for search in ("Same Name", "Shared Alias"):
            results = repositories.people.list(workspace.id, search)
            assert len(results) == 2
            assert {entry.id for entry in results} == {person.id, other.id}
        assert repositories.people.list(workspace.id, "Real Person") == [person]
        assert repositories.people.list(workspace.id, "First Alias") == [person]
        assert repositories.people.list(workspace.id, "not present") == []


@pytest.mark.parametrize("field", ["display_name", "real_name", "alias"])
@pytest.mark.parametrize("literal", ["%", "_", "\\", "a%_\\b"])
def test_search_escapes_like_metacharacters(runtime, records, now, field, literal):
    workspace, person, _, _, _ = records
    target = replace(person, id=uuid4(), display_name="Literal Target", real_name=None)
    if field != "alias":
        target = replace(target, **{field: "before " + literal + " after"})
    with transaction(runtime.sessions) as session:
        repositories = sql_repositories(session)
        repositories.people.save(target)
        if field == "alias":
            repositories.aliases.save(PersonAlias(id=uuid4(), workspace_id=workspace.id,
                person_id=target.id, alias="before " + literal + " after", created_at=now, updated_at=now))
        assert repositories.people.list(workspace.id, literal) == [target]


def test_workspace_names_are_case_sensitive_and_remain_unique_when_deleted(runtime, records, now):
    workspace = records[0]
    lower = replace(workspace, id=uuid4(), name=workspace.name.lower())
    with transaction(runtime.sessions) as session:
        repository = sql_repositories(session).workspaces
        repository.save(lower)
        assert repository.get_by_name(workspace.name) == workspace
        assert repository.get_by_name(lower.name) == lower
        assert repository.get_by_name(workspace.name.upper()) is None
        repository.save(replace(workspace, deleted_at=now))
    with pytest.raises(DatabaseError) as error:
        with transaction(runtime.sessions) as session:
            sql_repositories(session).workspaces.save(replace(workspace, id=uuid4()))
    assert isinstance(error.value.__cause__, IntegrityError)


def test_media_hash_dedup_is_workspace_scoped_even_when_deleted(runtime, records, now):
    workspace, _, _, media, _ = records
    other = Workspace(id=uuid4(), name="Other", created_at=now, updated_at=now)
    with transaction(runtime.sessions) as session:
        repositories = sql_repositories(session)
        repositories.workspaces.save(other)
        copy = replace(media, id=uuid4(), workspace_id=other.id)
        assert repositories.media.save(copy) == copy
        assert repositories.media.get_by_hash(other.id, copy.sha256) == copy
        repositories.media.save(replace(media, deleted_at=now))
    with pytest.raises(DatabaseError) as error:
        with transaction(runtime.sessions) as session:
            sql_repositories(session).media.save(replace(media, id=uuid4()))
    assert isinstance(error.value.__cause__, IntegrityError)


@pytest.mark.parametrize("violation", ["duplicate_role", "second_avatar", "cross_scope_person", "cross_scope_media"])
def test_association_constraints_are_enforced_by_database(runtime, records, now, violation):
    workspace, person, _, media, link = records
    with transaction(runtime.sessions) as session:
        repositories = sql_repositories(session)
        if violation == "second_avatar":
            repositories.person_media.save(replace(link, role="AVATAR"))
            second_media = replace(media, id=uuid4(), sha256="b" * 64)
            repositories.media.save(second_media)
            invalid = replace(link, id=uuid4(), media_id=second_media.id, role="AVATAR")
        elif violation.startswith("cross_scope"):
            other = Workspace(id=uuid4(), name="Other", created_at=now, updated_at=now)
            repositories.workspaces.save(other)
            if violation == "cross_scope_person":
                other_person = replace(person, id=uuid4(), workspace_id=other.id)
                repositories.people.save(other_person)
                invalid = replace(link, id=uuid4(), person_id=other_person.id)
            else:
                other_media = replace(media, id=uuid4(), workspace_id=other.id)
                repositories.media.save(other_media)
                invalid = replace(link, id=uuid4(), media_id=other_media.id)
        else:
            invalid = replace(link, id=uuid4())
    with pytest.raises(DatabaseError) as error:
        with transaction(runtime.sessions) as session:
            sql_repositories(session).person_media.save(invalid)
    assert isinstance(error.value.__cause__, IntegrityError)


def test_gallery_partial_unique_allows_many_gallery_rows_and_avatar_same_media(runtime, records):
    workspace, person, _, media, link = records
    with transaction(runtime.sessions) as session:
        repositories = sql_repositories(session)
        second_media = replace(media, id=uuid4(), sha256="b" * 64)
        repositories.media.save(second_media)
        gallery = replace(link, id=uuid4(), media_id=second_media.id, sort_order=0)
        avatar = replace(link, id=uuid4(), role="AVATAR", sort_order=1)
        repositories.person_media.save(gallery)
        repositories.person_media.save(avatar)
        assert repositories.person_media.list(workspace.id, person.id) == [gallery, avatar, link]
        repositories.person_media.remove_for_person(workspace.id, person.id, "AVATAR")
        assert repositories.person_media.list(workspace.id, person.id) == [gallery, link]
        repositories.person_media.remove(workspace.id, link.id)
        assert repositories.person_media.list(workspace.id, person.id) == [gallery]
        repositories.person_media.remove_for_person(workspace.id, person.id)
        assert repositories.person_media.list(workspace.id, person.id) == []
        assert not repositories.person_media.has_references(workspace.id, media.id, active_only=False)


@pytest.mark.parametrize("parent", ["person", "workspace", "media"])
def test_removals_do_not_require_active_parents(runtime, records, now, parent):
    workspace, person, alias, media, link = records
    with transaction(runtime.sessions) as session:
        repositories = sql_repositories(session)
        repository, entity = {"person": (repositories.people, person),
                              "workspace": (repositories.workspaces, workspace),
                              "media": (repositories.media, media)}[parent]
        repository.save(replace(entity, deleted_at=now))
        repositories.aliases.remove(workspace.id, alias.id)
        repositories.aliases.remove(workspace.id, alias.id)  # Idempotent.
        repositories.person_media.remove_for_person(workspace.id, person.id, "GALLERY")
        assert session.scalar(select(func.count()).select_from(PersonAliasModel)) == 0
        assert session.scalar(select(func.count()).select_from(PersonMediaModel)) == 0


def test_remove_link_by_id_with_inactive_workspace(runtime, records, now):
    workspace, _, _, media, link = records
    with transaction(runtime.sessions) as session:
        repositories = sql_repositories(session)
        repositories.workspaces.save(replace(workspace, deleted_at=now))
        repositories.person_media.remove(workspace.id, link.id)
        repositories.person_media.remove(workspace.id, link.id)
        assert not repositories.person_media.has_references(workspace.id, media.id, active_only=False)


def test_repository_flushes_do_not_commit_and_transaction_rolls_back(runtime, now):
    workspace = Workspace(id=uuid4(), name="Atomic", created_at=now, updated_at=now)
    person = Person(id=uuid4(), workspace_id=workspace.id, display_name="Atomic Person",
                    created_at=now, updated_at=now)
    with pytest.raises(RuntimeError, match="abort operation"):
        with transaction(runtime.sessions) as session:
            repositories = sql_repositories(session)
            repositories.workspaces.save(workspace)
            repositories.people.save(person)
            assert session.scalar(select(func.count()).select_from(WorkspaceModel)) == 1
            raise RuntimeError("abort operation")
    with transaction(runtime.sessions) as session:
        repositories = sql_repositories(session)
        assert repositories.workspaces.get(workspace.id, include_deleted=True) is None
        assert repositories.people.get(workspace.id, person.id, include_deleted=True) is None


@pytest.mark.parametrize("invalid_parent", ["cross_scope", "missing"])
def test_alias_foreign_keys_are_enforced_by_database(runtime, records, now, invalid_parent):
    workspace, person, alias, _, _ = records
    if invalid_parent == "cross_scope":
        with transaction(runtime.sessions) as session:
            repositories = sql_repositories(session)
            other = Workspace(id=uuid4(), name="Other", created_at=now, updated_at=now)
            repositories.workspaces.save(other)
            other_person = replace(person, id=uuid4(), workspace_id=other.id)
            repositories.people.save(other_person)
        invalid = replace(alias, id=uuid4(), person_id=other_person.id)
    else:
        invalid = replace(alias, id=uuid4(), person_id=uuid4())
    with pytest.raises(DatabaseError) as error:
        with transaction(runtime.sessions) as session:
            sql_repositories(session).aliases.save(invalid)
    assert isinstance(error.value.__cause__, IntegrityError)


def test_save_is_persistence_only_even_with_inactive_parents(runtime, records, now):
    workspace, person, alias, media, link = records
    with transaction(runtime.sessions) as session:
        repositories = sql_repositories(session)
        repositories.workspaces.save(replace(workspace, deleted_at=now))
        repositories.people.save(replace(person, deleted_at=now))
        repositories.media.save(replace(media, deleted_at=now))
        # Business rejection/restore rules belong to services, not these adapters.
        saved_alias = replace(alias, id=uuid4(), alias="Archived Alias")
        saved_link = replace(link, id=uuid4(), role="AVATAR")
        assert repositories.aliases.save(saved_alias) == saved_alias
        assert repositories.person_media.save(saved_link) == saved_link
        assert repositories.aliases.get(workspace.id, saved_alias.id) is None
        assert repositories.person_media.get(workspace.id, saved_link.id) is None
        assert repositories.person_media.has_references(workspace.id, media.id, active_only=False)


def test_missing_ids_return_none_and_missing_removals_are_noops(runtime):
    with transaction(runtime.sessions) as session:
        repositories = sql_repositories(session)
        workspace_id, missing_id = uuid4(), uuid4()
        assert repositories.workspaces.get(missing_id) is None
        assert repositories.workspaces.get_by_name("missing") is None
        for repository in (repositories.people, repositories.aliases, repositories.media, repositories.person_media):
            assert repository.get(workspace_id, missing_id) is None
        repositories.aliases.remove(workspace_id, missing_id)
        repositories.person_media.remove(workspace_id, missing_id)
        repositories.person_media.remove_for_person(workspace_id, missing_id)
