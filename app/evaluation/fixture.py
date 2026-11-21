"""Materialize synthetic fixtures into an evaluation-owned temporary store.

Hard rules:

- every write goes through the real application Services (never ORM/SQL);
- the store is created under the caller-supplied directory only — the loader
  (`app.evaluation.dataset`) never touches any database, and the materializer
  never touches the production data directory;
- everything here is offline: no network, no LLM calls, no fact-layer semantics
  beyond what the Services already enforce.
"""
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from app.config.settings import AppConfig
from app.services.application import create_application_services
from app.services.database import initialize_database
from app.services.event import ParticipantInput
from app.evaluation.models import (CONTEXT_REF_TYPES, FixtureSpec, MediaSpec)


@dataclass
class MaterializedFixture:
    """One seeded synthetic workspace universe with stable key→UUID bindings."""

    fixture_id: str
    data_dir: Path
    workspace_ids: dict = field(default_factory=dict)   # workspace key -> UUID
    ids: dict = field(default_factory=dict)             # "TYPE:key" -> UUID
    services: object = None
    runtime: object = None

    def resolve(self, ref_type: str, key: str) -> UUID:
        if ref_type not in CONTEXT_REF_TYPES:
            raise KeyError(f"未知引用类型：{ref_type!r}")
        try:
            return self.ids[f"{ref_type}:{key}"]
        except KeyError:
            raise KeyError(f"{ref_type}:{key} 未在夹具 {self.fixture_id!r} 中物化。") from None

    def workspace_of(self, ref_type: str, key: str) -> UUID:
        return self.workspace_ids[self.workspace_key_of(ref_type, key)]

    def workspace_key_of(self, ref_type: str, key: str) -> str:
        return self._workspace_keys[f"{ref_type}:{key}"]

    def close(self) -> None:
        if self.runtime is not None:
            self.runtime.close()
            self.runtime = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    _workspace_keys: dict = field(default_factory=dict)


def materialize_fixture(fixture: FixtureSpec, data_dir: Path) -> MaterializedFixture:
    """Seed `fixture` into a fresh evaluation store under `data_dir`."""
    store_dir = Path(data_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    config = AppConfig(data_dir=store_dir)
    runtime = initialize_database(config)
    materialized = MaterializedFixture(fixture_id=fixture.fixture_id, data_dir=store_dir,
                                       runtime=runtime)
    try:
        services = create_application_services(config, runtime.sessions)
        materialized.services = services
        _seed(fixture, services, materialized)
    except BaseException:
        materialized.close()
        raise
    return materialized


def _bind(materialized, ref_type, key, identifier, workspace_key):
    token = f"{ref_type}:{key}"
    if token in materialized.ids:
        raise ValueError(f"夹具 key 重复：{token}")
    materialized.ids[token] = identifier
    materialized._workspace_keys[token] = workspace_key


def _seed(fixture: FixtureSpec, services, materialized: MaterializedFixture) -> None:
    for key, name in fixture.workspaces:
        workspace = services.workspaces.create(name)
        materialized.workspace_ids[key] = workspace.id

    for person in fixture.people:
        fields = {}
        for name in ("real_name", "gender", "occupation", "organization", "location", "notes"):
            value = getattr(person, name)
            if value:
                fields[name] = value
        if person.birthday is not None:
            fields["birthday"] = person.birthday
        created = services.people.create(materialized.workspace_ids[person.workspace],
                                         person.display_name, **fields)
        _bind(materialized, "PERSON", person.key, created.id, person.workspace)
        for alias in person.aliases:
            services.people.add_alias(materialized.workspace_ids[person.workspace], created.id,
                                      alias.alias, alias_type=alias.alias_type,
                                      note=alias.note or None)

    for relationship in fixture.relationships:
        created = services.relationships.create(
            materialized.workspace_ids[relationship.workspace],
            materialized.resolve("PERSON", relationship.person_a),
            materialized.resolve("PERSON", relationship.person_b),
            relationship.relationship_type, relationship.directionality,
            initial_state=relationship.initial_state,
            effective_at=relationship.effective_at, note=relationship.note or None)
        _bind(materialized, "RELATIONSHIP", relationship.key, created.id, relationship.workspace)
        for row in relationship.history:
            services.relationships.add_history(
                materialized.workspace_ids[relationship.workspace], created.id, row.state,
                effective_at=row.effective_at, note=row.note or None)

    for event in fixture.all_events():
        participants = tuple(ParticipantInput(materialized.resolve("PERSON", item.person),
                                              item.role or None, item.note or None)
                             for item in event.participants)
        created = services.events.create_event(
            materialized.workspace_ids[event.workspace], event.title, event.event_type,
            event.record_kind, occurred_on=event.occurred_on, started_at=event.started_at,
            ended_at=event.ended_at, location=event.location or None,
            description=event.description or None, participants=participants)
        _bind(materialized, "EVENT", event.key, created.id, event.workspace)

    for item in fixture.all_evidence():
        media_ids = []
        for media in item.media:
            media_ids.append(_import_media(services, materialized, item.workspace, media))
        created = services.evidence.create_evidence(
            materialized.workspace_ids[item.workspace],
            materialized.resolve("EVENT", item.event), item.kind, item.title,
            source_type=item.source_type, content_text=item.content_text or None,
            annotation=item.annotation or None, source_label=item.source_label or None,
            source_reference=item.source_reference or None, captured_at=item.captured_at,
            media=tuple(media_ids))
        _bind(materialized, "EVIDENCE", item.key, created.id, item.workspace)

    for memory in fixture.memories:
        created = services.memories.create(
            materialized.workspace_ids[memory.workspace], memory.content, kind=memory.kind,
            person_id=materialized.resolve("PERSON", memory.person) if memory.person else None,
            valid_from=memory.valid_from, valid_until=memory.valid_until)
        _bind(materialized, "MEMORY", memory.key, created.id, memory.workspace)


def _import_media(services, materialized, workspace_key, media: MediaSpec) -> UUID:
    staging = materialized.data_dir / "fixture-import"
    staging.mkdir(parents=True, exist_ok=True)
    # Prefix with the media key so filenames stay unique and deterministic.
    path = staging / f"{media.key}-{media.filename}"
    path.write_bytes(media.content.encode("utf-8"))
    created = services.media.import_file(materialized.workspace_ids[workspace_key], path)
    _bind(materialized, "MEDIA", media.key, created.id, workspace_key)
    return created.id
