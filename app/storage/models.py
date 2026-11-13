"""Explicit Phase 3/4/5 persistence models; no Domain inheritance or UI access."""
from datetime import date, datetime
from uuid import UUID
from sqlalchemy import CheckConstraint, Date, ForeignKeyConstraint, Index, Integer, PrimaryKeyConstraint, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column
from app.storage.database import Base
from app.storage.types import UTCDateTime

class WorkspaceModel(Base):
    __tablename__ = "workspace"
    __table_args__ = (UniqueConstraint("name", name="uq_workspace_name"),
                      CheckConstraint("length(trim(name)) > 0", name="name_nonempty"))
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

class PersonModel(Base):
    __tablename__ = "person"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], name="fk_person_workspace_id_workspace"),
        UniqueConstraint("workspace_id", "id", name="uq_person_workspace_id_id"),
        CheckConstraint("length(trim(display_name)) > 0", name="display_name_nonempty"),
        Index("ix_person_workspace_display_name", "workspace_id", "display_name"),
        Index("ix_person_workspace_real_name", "workspace_id", "real_name"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    display_name: Mapped[str] = mapped_column(String(200))
    real_name: Mapped[str | None] = mapped_column(String(200))
    gender: Mapped[str | None] = mapped_column(String(100))
    birthday: Mapped[date | None] = mapped_column(Date)
    occupation: Mapped[str | None] = mapped_column(String(300))
    organization: Mapped[str | None] = mapped_column(String(300))
    location: Mapped[str | None] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

class PersonAliasModel(Base):
    __tablename__ = "person_alias"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "person_id"], ["person.workspace_id", "person.id"], name="fk_person_alias_workspace_person"),
        CheckConstraint("length(trim(alias)) > 0", name="alias_nonempty"),
        Index("ix_person_alias_workspace_alias", "workspace_id", "alias"),
        Index("ix_person_alias_person_id", "person_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    person_id: Mapped[UUID] = mapped_column(Uuid)
    alias: Mapped[str] = mapped_column(String(300))
    alias_type: Mapped[str] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())

class MediaModel(Base):
    __tablename__ = "media"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], name="fk_media_workspace_id_workspace"),
        UniqueConstraint("workspace_id", "sha256", name="uq_media_workspace_sha256"),
        UniqueConstraint("workspace_id", "id", name="uq_media_workspace_id_id"),
        CheckConstraint("length(sha256) = 64", name="sha256_length"),
        CheckConstraint("size_bytes >= 0", name="size_nonnegative"),
        CheckConstraint("(width IS NULL AND height IS NULL) OR (width IS NOT NULL AND height IS NOT NULL AND width > 0 AND height > 0)", name="dimensions_valid"),
        CheckConstraint("length(storage_path) > 0 AND storage_path NOT LIKE '/%' AND storage_path NOT LIKE '%:%'", name="path_relative"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    sha256: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(200))
    size_bytes: Mapped[int] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    storage_path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

class PersonMediaModel(Base):
    __tablename__ = "person_media"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "person_id"], ["person.workspace_id", "person.id"], name="fk_person_media_workspace_person"),
        ForeignKeyConstraint(["workspace_id", "media_id"], ["media.workspace_id", "media.id"], name="fk_person_media_workspace_media"),
        UniqueConstraint("person_id", "media_id", "role", name="uq_person_media_person_media_role"),
        CheckConstraint("role IN ('AVATAR', 'GALLERY')", name="role_valid"),
        CheckConstraint("sort_order >= 0", name="sort_order_nonnegative"),
        Index("ix_person_media_person_sort", "person_id", "sort_order"),
        Index("ix_person_media_media_id", "media_id"),
        Index("uq_person_media_avatar", "person_id", unique=True,
              sqlite_where=text("role = 'AVATAR'"), postgresql_where=text("role = 'AVATAR'")),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    person_id: Mapped[UUID] = mapped_column(Uuid)
    media_id: Mapped[UUID] = mapped_column(Uuid)
    role: Mapped[str] = mapped_column(String(20))
    sort_order: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


# Phase 4 additions: the five preceding mappings remain unchanged.
from sqlalchemy import PrimaryKeyConstraint


class RelationshipModel(Base):
    __tablename__ = "relationship"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_relationship"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"],
                             name="fk_relationship_workspace_id_workspace"),
        ForeignKeyConstraint(["workspace_id", "person_a_id"],
                             ["person.workspace_id", "person.id"],
                             name="fk_relationship_workspace_person_a"),
        ForeignKeyConstraint(["workspace_id", "person_b_id"],
                             ["person.workspace_id", "person.id"],
                             name="fk_relationship_workspace_person_b"),
        UniqueConstraint("workspace_id", "id", name="uq_relationship_workspace_id_id"),
        UniqueConstraint("workspace_id", "person_a_id", "person_b_id", "relationship_type", "directionality",
                         name="uq_relationship_workspace_pair_type_directionality"),
        CheckConstraint("person_a_id <> person_b_id", name="people_distinct"),
        CheckConstraint("directionality IN ('UNDIRECTED', 'DIRECTED')", name="directionality_valid"),
        CheckConstraint("directionality <> 'UNDIRECTED' OR person_a_id < person_b_id",
                        name="undirected_sorted"),
        CheckConstraint("relationship_type = trim(relationship_type) AND length(relationship_type) BETWEEN 1 AND 200",
                        name="type_trimmed_length"),
        Index("ix_relationship_workspace_person_a", "workspace_id", "person_a_id"),
        Index("ix_relationship_workspace_person_b", "workspace_id", "person_b_id"),
        Index("ix_relationship_workspace_type", "workspace_id", "relationship_type"),
        Index("ix_relationship_workspace_deleted", "workspace_id", "deleted_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    person_a_id: Mapped[UUID] = mapped_column(Uuid)
    person_b_id: Mapped[UUID] = mapped_column(Uuid)
    relationship_type: Mapped[str] = mapped_column(String(200))
    directionality: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class EventModel(Base):
    __tablename__ = "event"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"], name="fk_event_workspace_id_workspace"),
        UniqueConstraint("workspace_id", "id", name="uq_event_workspace_id_id"),
        CheckConstraint("title = trim(title) AND length(title) BETWEEN 1 AND 200", name="title_trimmed_length"),
        CheckConstraint("event_type = trim(event_type) AND length(event_type) BETWEEN 1 AND 200", name="type_trimmed_length"),
        CheckConstraint("record_kind IN ('FACT', 'OBSERVATION')", name="record_kind_valid"),
        CheckConstraint(
            "(occurred_on IS NOT NULL AND started_at IS NULL AND ended_at IS NULL) OR "
            "(occurred_on IS NULL AND started_at IS NOT NULL AND (ended_at IS NULL OR ended_at >= started_at))",
            name="time_valid"),
        Index("ix_event_workspace_started", "workspace_id", "started_at"),
        Index("ix_event_workspace_occurred", "workspace_id", "occurred_on"),
        Index("ix_event_workspace_deleted", "workspace_id", "deleted_at"),
        Index("ix_event_workspace_type", "workspace_id", "event_type"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    title: Mapped[str] = mapped_column(String(200))
    event_type: Mapped[str] = mapped_column(String(200))
    record_kind: Mapped[str] = mapped_column(String(20))
    occurred_on: Mapped[date | None] = mapped_column(Date)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    location: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class EventParticipantModel(Base):
    __tablename__ = "event_participant"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "event_id"], ["event.workspace_id", "event.id"],
                             name="fk_event_participant_workspace_event"),
        ForeignKeyConstraint(["workspace_id", "person_id"], ["person.workspace_id", "person.id"],
                             name="fk_event_participant_workspace_person"),
        UniqueConstraint("event_id", "person_id", name="uq_event_participant_event_person"),
        Index("ix_event_participant_workspace_event", "workspace_id", "event_id"),
        Index("ix_event_participant_workspace_person", "workspace_id", "person_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    event_id: Mapped[UUID] = mapped_column(Uuid)
    person_id: Mapped[UUID] = mapped_column(Uuid)
    role: Mapped[str | None] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class RelationshipHistoryModel(Base):
    __tablename__ = "relationship_history"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_relationship_history"),
        ForeignKeyConstraint(["workspace_id", "relationship_id"],
                             ["relationship.workspace_id", "relationship.id"],
                             name="fk_relationship_history_workspace_relationship"),
        ForeignKeyConstraint(["workspace_id", "event_id"], ["event.workspace_id", "event.id"],
                             name="fk_relationship_history_workspace_event"),
        CheckConstraint("state = trim(state) AND length(state) BETWEEN 1 AND 50",
                        name="state_trimmed_length"),
        Index("ix_relationship_history_workspace_relationship_order",
              "workspace_id", "relationship_id", "effective_at", "created_at", "id"),
        Index("ix_relationship_history_workspace_event", "workspace_id", "event_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    relationship_id: Mapped[UUID] = mapped_column(Uuid)
    event_id: Mapped[UUID | None] = mapped_column(Uuid)
    state: Mapped[str] = mapped_column(String(50))
    effective_at: Mapped[datetime] = mapped_column(UTCDateTime())
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


# ORM guards deliberately do not claim to protect administrator-issued raw SQL.
from sqlalchemy import event, inspect as sqlalchemy_inspect
from app.domain.errors import DomainError


@event.listens_for(RelationshipModel, "before_update")
def _guard_relationship_identity(_mapper, _connection, target):
    identity_fields = ("id", "workspace_id", "person_a_id", "person_b_id",
                       "relationship_type", "directionality", "created_at")
    attributes = sqlalchemy_inspect(target).attrs
    if any(attributes[field].history.has_changes() for field in identity_fields):
        raise DomainError("关系身份不可修改；请软删除旧关系后创建新关系。")


@event.listens_for(RelationshipHistoryModel, "before_delete")
def _guard_relationship_history_delete(_mapper, _connection, _target):
    raise DomainError("关系历史只允许追加，不允许修改或删除。")


@event.listens_for(RelationshipHistoryModel, "before_update")
def _guard_relationship_history_append_only(_mapper, connection, target):
    from sqlalchemy import select
    from sqlalchemy.orm import object_session

    attributes = sqlalchemy_inspect(target).attrs
    changed = {attribute.key for attribute in attributes if attribute.history.has_changes()}
    session = object_session(target)
    if changed == {"event_id"} and session is not None:
        history = attributes.event_id.history
        # Expired/deferred attributes may not expose the old value in ORM history.
        # Read through the same transaction, never guess that an absent value is NULL.
        old_event_id = (history.deleted[0] if history.deleted else connection.scalar(
            select(RelationshipHistoryModel.__table__.c.event_id).where(
                RelationshipHistoryModel.__table__.c.id == target.id)))
        authorization = session.info.get("ris_history_event_attachment")
        if authorization == (target.id, old_event_id, target.event_id):
            return
    raise DomainError("关系历史只允许追加，不允许修改或删除。")


# Phase 6 additions: captured evidence and its media bindings; earlier mappings remain unchanged.
class EvidenceModel(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "event_id"], ["event.workspace_id", "event.id"],
                             name="fk_evidence_workspace_event"),
        UniqueConstraint("workspace_id", "id", name="uq_evidence_workspace_id_id"),
        CheckConstraint("kind IN ('TEXT','IMAGE','FILE','LINK','MANUAL','OTHER')", name="kind_valid"),
        CheckConstraint("source_type IN ('CHAT','EMAIL','DOCUMENT','IMAGE','FILE','WEB','MANUAL','OTHER')",
                        name="source_type_valid"),
        CheckConstraint("title = trim(title) AND length(title) BETWEEN 1 AND 200",
                        name="title_trimmed_length"),
        CheckConstraint("source_label IS NULL OR length(source_label) BETWEEN 1 AND 300",
                        name="source_label_length"),
        Index("ix_evidence_workspace_event", "workspace_id", "event_id"),
        Index("ix_evidence_workspace_kind", "workspace_id", "kind"),
        Index("ix_evidence_workspace_deleted", "workspace_id", "deleted_at"),
        Index("ix_evidence_workspace_created", "workspace_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    event_id: Mapped[UUID] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(200))
    content_text: Mapped[str | None] = mapped_column(Text)
    annotation: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(20))
    source_label: Mapped[str | None] = mapped_column(String(300))
    source_reference: Mapped[str | None] = mapped_column(Text)
    captured_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class EvidenceMediaModel(Base):
    __tablename__ = "evidence_media"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "evidence_id"], ["evidence.workspace_id", "evidence.id"],
                             name="fk_evidence_media_workspace_evidence"),
        ForeignKeyConstraint(["workspace_id", "media_id"], ["media.workspace_id", "media.id"],
                             name="fk_evidence_media_workspace_media"),
        UniqueConstraint("evidence_id", "media_id", "role", name="uq_evidence_media_evidence_media_role"),
        Index("ix_evidence_media_workspace_evidence", "workspace_id", "evidence_id"),
        Index("ix_evidence_media_workspace_media", "workspace_id", "media_id"),
        # NULL role values never compare equal, so the UNIQUE above cannot reject them.
        Index("uq_evidence_media_evidence_media_norole", "evidence_id", "media_id", unique=True,
              sqlite_where=text("role IS NULL"), postgresql_where=text("role IS NULL")),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    evidence_id: Mapped[UUID] = mapped_column(Uuid)
    media_id: Mapped[UUID] = mapped_column(Uuid)
    role: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


@event.listens_for(EvidenceModel, "before_update")
def _guard_evidence_original_content(_mapper, _connection, target):
    original_fields = ("id", "workspace_id", "event_id", "kind", "source_type",
                       "source_reference", "content_text", "captured_at", "created_at")
    attributes = sqlalchemy_inspect(target).attrs
    if any(attributes[field].history.has_changes() for field in original_fields):
        raise DomainError("原始证据内容不可修改，请软删除后新建。")


@event.listens_for(EvidenceModel, "before_delete")
def _guard_evidence_delete(_mapper, _connection, _target):
    raise DomainError("原始证据内容不可修改，请软删除后新建。")


# Phase 7 additions: the search index is a rebuildable PROJECTION, not a fact table.
# It carries no business meaning of its own and may be dropped and rebuilt at any
# time from person/person_alias/event/evidence. The FTS5 virtual table is created by
# migration 0006 (and by the projection adapter for a bare test database); it is
# deliberately not declared as a mapped model because it is storage infrastructure.
SEARCH_ENTITY_TYPES = ("PERSON", "ALIAS", "EVENT", "EVIDENCE")


class SearchDocumentModel(Base):
    """One searchable fact projection row; content is derived, never authoritative."""

    __tablename__ = "search_document"
    __table_args__ = (
        UniqueConstraint("workspace_id", "entity_type", "entity_id",
                         name="uq_search_document_workspace_entity"),
        CheckConstraint("entity_type IN ('PERSON','ALIAS','EVENT','EVIDENCE')",
                        name="entity_type_valid"),
        Index("ix_search_document_workspace_type", "workspace_id", "entity_type"),
        Index("ix_search_document_workspace_deleted", "workspace_id", "deleted_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    entity_type: Mapped[str] = mapped_column(String(20))
    entity_id: Mapped[UUID] = mapped_column(Uuid)
    parent_id: Mapped[UUID | None] = mapped_column(Uuid)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    title_tokens: Mapped[str] = mapped_column(Text)
    body_tokens: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


# Phase 8 additions: the twelve preceding mappings remain unchanged.
#
# An ANALYSIS RUN is a reproducible record of one model invocation, not a business
# fact about a person, relationship or event: the sources it consumed live in
# separate association tables, so nothing here overwrites a fact. The exact request
# (scope), the exact context handed to the model (context_snapshot) and the raw
# answer (result) are kept apart, so a later review can separate "what was asked",
# "what the model saw" and "what it answered".
#
# This module deliberately declares NO credential column: no API key, token,
# password or secret is ever persisted next to an analysis run. A credential is
# either supplied for a single call or looked up by the infrastructure layer, so a
# database copy can never leak one.
ANALYSIS_TARGET_TYPES = ("PERSON", "RELATIONSHIP", "EVENT")
ANALYSIS_TYPES = ("PERSON_PROFILE", "RELATIONSHIP_ANALYSIS", "EVENT_ANALYSIS")
ANALYSIS_STATUSES = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED")


class AnalysisRunModel(Base):
    """One analysis invocation: scope, sent context, status and raw answer."""

    __tablename__ = "analysis_run"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"],
                             name="fk_analysis_run_workspace_id_workspace"),
        # Required as the composite foreign key target of the four source tables.
        UniqueConstraint("workspace_id", "id", name="uq_analysis_run_workspace_id_id"),
        CheckConstraint("target_type IN ('PERSON','RELATIONSHIP','EVENT')",
                        name="target_type_valid"),
        CheckConstraint("analysis_type IN ('PERSON_PROFILE','RELATIONSHIP_ANALYSIS','EVENT_ANALYSIS')",
                        name="analysis_type_valid"),
        CheckConstraint("status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')",
                        name="status_valid"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="duration_nonnegative"),
        Index("ix_analysis_run_workspace_created", "workspace_id", "created_at"),
        Index("ix_analysis_run_workspace_target", "workspace_id", "target_type", "target_id"),
        Index("ix_analysis_run_workspace_status", "workspace_id", "status"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    target_type: Mapped[str] = mapped_column(String(20))
    target_id: Mapped[UUID] = mapped_column(Uuid)
    analysis_type: Mapped[str] = mapped_column(String(30))
    provider_name: Mapped[str] = mapped_column(String(100))
    model_name: Mapped[str] = mapped_column(String(200))
    # JSON document: requested time range / as_of instant / include switches /
    # explicitly selected ids. Text keeps it readable at rest on both dialects.
    scope: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    # Exactly what was handed to the model, frozen before the call returns; it is
    # NULL until the context has been built and never a substitute for the facts.
    context_snapshot: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class AnalysisPersonSourceModel(Base):
    """A person fact a run consumed; the row is provenance, never a fact owner."""

    __tablename__ = "analysis_person_source"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "analysis_run_id"],
                             ["analysis_run.workspace_id", "analysis_run.id"],
                             name="fk_analysis_person_source_workspace_run"),
        ForeignKeyConstraint(["workspace_id", "target_id"],
                             ["person.workspace_id", "person.id"],
                             name="fk_analysis_person_source_workspace_person"),
        UniqueConstraint("analysis_run_id", "target_id",
                         name="uq_analysis_person_source_run_target"),
        Index("ix_analysis_person_source_workspace_run", "workspace_id", "analysis_run_id"),
        Index("ix_analysis_person_source_workspace_target", "workspace_id", "target_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    analysis_run_id: Mapped[UUID] = mapped_column(Uuid)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    target_id: Mapped[UUID] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class AnalysisRelationshipSourceModel(Base):
    """A relationship fact a run consumed; the row is provenance, never a fact owner."""

    __tablename__ = "analysis_relationship_source"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "analysis_run_id"],
                             ["analysis_run.workspace_id", "analysis_run.id"],
                             name="fk_analysis_relationship_source_workspace_run"),
        ForeignKeyConstraint(["workspace_id", "target_id"],
                             ["relationship.workspace_id", "relationship.id"],
                             name="fk_analysis_relationship_source_workspace_relationship"),
        UniqueConstraint("analysis_run_id", "target_id",
                         name="uq_analysis_relationship_source_run_target"),
        Index("ix_analysis_relationship_source_workspace_run", "workspace_id", "analysis_run_id"),
        Index("ix_analysis_relationship_source_workspace_target", "workspace_id", "target_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    analysis_run_id: Mapped[UUID] = mapped_column(Uuid)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    target_id: Mapped[UUID] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class AnalysisEventSourceModel(Base):
    """An event fact a run consumed; the row is provenance, never a fact owner."""

    __tablename__ = "analysis_event_source"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "analysis_run_id"],
                             ["analysis_run.workspace_id", "analysis_run.id"],
                             name="fk_analysis_event_source_workspace_run"),
        ForeignKeyConstraint(["workspace_id", "target_id"],
                             ["event.workspace_id", "event.id"],
                             name="fk_analysis_event_source_workspace_event"),
        UniqueConstraint("analysis_run_id", "target_id",
                         name="uq_analysis_event_source_run_target"),
        Index("ix_analysis_event_source_workspace_run", "workspace_id", "analysis_run_id"),
        Index("ix_analysis_event_source_workspace_target", "workspace_id", "target_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    analysis_run_id: Mapped[UUID] = mapped_column(Uuid)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    target_id: Mapped[UUID] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class AnalysisEvidenceSourceModel(Base):
    """An evidence fact a run consumed; the row is provenance, never a fact owner."""

    __tablename__ = "analysis_evidence_source"
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "analysis_run_id"],
                             ["analysis_run.workspace_id", "analysis_run.id"],
                             name="fk_analysis_evidence_source_workspace_run"),
        ForeignKeyConstraint(["workspace_id", "target_id"],
                             ["evidence.workspace_id", "evidence.id"],
                             name="fk_analysis_evidence_source_workspace_evidence"),
        UniqueConstraint("analysis_run_id", "target_id",
                         name="uq_analysis_evidence_source_run_target"),
        Index("ix_analysis_evidence_source_workspace_run", "workspace_id", "analysis_run_id"),
        Index("ix_analysis_evidence_source_workspace_target", "workspace_id", "target_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    analysis_run_id: Mapped[UUID] = mapped_column(Uuid)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    target_id: Mapped[UUID] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


# Phase 9 additions: the seventeen preceding mappings remain unchanged.
#
# A MEMORY is what the user has *confirmed* to be true about a person or about a
# whole workspace - a fact, a preference or background context. It is a business
# fact in its own right, never a model output: the analysis layer may only ever
# propose text, and a row exists here exclusively after confirmation, which is why
# ``confirmed_at`` carries no NULL.
#
# Two deliberate omissions:
#
# * ``person_id`` is nullable: NULL means the memory belongs to the workspace as a
#   whole instead of to one person.
# * there is no unique constraint on ``person_id`` or on ``content``. One person
#   legitimately carries many similar memories, and an edit may rewrite content, so
#   uniqueness would be a false business rule rather than a guarantee.
MEMORY_KINDS = ("FACT", "PREFERENCE", "CONTEXT")
MEMORY_SOURCE_TYPES = ("MANUAL", "ANALYSIS")


class MemoryModel(Base):
    """One confirmed memory: person-scoped when person_id is set, else workspace-wide.

    ``source_type``/``source_analysis_id`` record where the text came from without
    ever storing a model answer as a fact: MANUAL keeps the id NULL, ANALYSIS
    requires the id of the run that proposed it. The run is provenance only, so
    deleting it later is blocked instead of cascading into the confirmed memory.
    """

    __tablename__ = "memory"
    __table_args__ = (
        # Composite keys: a person or a run from another workspace is unreachable,
        # so a memory can never point across a workspace boundary.
        ForeignKeyConstraint(["workspace_id", "person_id"],
                             ["person.workspace_id", "person.id"],
                             name="fk_memory_workspace_person"),
        ForeignKeyConstraint(["workspace_id", "source_analysis_id"],
                             ["analysis_run.workspace_id", "analysis_run.id"],
                             name="fk_memory_workspace_analysis_run"),
        ForeignKeyConstraint(["workspace_id"], ["workspace.id"],
                             name="fk_memory_workspace_id_workspace"),
        # The composite foreign key targets above require it on this side too.
        UniqueConstraint("workspace_id", "id", name="uq_memory_workspace_id_id"),
        CheckConstraint("kind IN ('FACT','PREFERENCE','CONTEXT')", name="kind_valid"),
        # Content is stored exactly as confirmed: no surrounding blank, never empty
        # and never longer than 4000 characters.
        CheckConstraint("content = trim(content) AND length(content) BETWEEN 1 AND 4000",
                        name="content_normalized"),
        CheckConstraint("source_type IN ('MANUAL','ANALYSIS')", name="source_type_valid"),
        # MANUAL text has no run behind it; ANALYSIS text must name the run it came
        # from. SQLite evaluates this per row, NULL included.
        CheckConstraint("(source_type = 'MANUAL' AND source_analysis_id IS NULL) OR "
                        "(source_type = 'ANALYSIS' AND source_analysis_id IS NOT NULL)",
                        name="source_reference_valid"),
        CheckConstraint("valid_from IS NULL OR valid_until IS NULL "
                        "OR valid_until >= valid_from", name="validity_window_valid"),
        Index("ix_memory_workspace_person", "workspace_id", "person_id"),
        Index("ix_memory_workspace_kind", "workspace_id", "kind"),
        Index("ix_memory_workspace_deleted", "workspace_id", "deleted_at"),
        Index("ix_memory_workspace_valid", "workspace_id", "valid_from", "valid_until"),
        Index("ix_memory_workspace_analysis", "workspace_id", "source_analysis_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    # NULL = a Workspace-wide memory, not a memory without an owner.
    person_id: Mapped[UUID | None] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(20))
    source_analysis_id: Mapped[UUID | None] = mapped_column(Uuid)
    # A memory exists only after the user confirmed it, so this is never NULL.
    confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    # Optional validity window in real-world time; both ends are open.
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime())
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

