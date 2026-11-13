"""Add events, participants and optional history attachments using frozen SQL types.

Revision ID: 0004_event
Revises: 0003_relationship
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_event"
down_revision = "0003_relationship"
branch_labels = None
depends_on = None


def _history_copy_from(*, include_event: bool) -> sa.Table:
    """Frozen source schema for SQLite --sql batch rebuilds (never used online)."""
    metadata = sa.MetaData()
    # These target stubs resolve composite FK columns during DDL compilation;
    # Alembic emits only the history table being rebuilt, never these stubs.
    sa.Table("relationship", metadata, sa.Column("workspace_id", sa.Uuid()), sa.Column("id", sa.Uuid()))
    if include_event:
        sa.Table("event", metadata, sa.Column("workspace_id", sa.Uuid()), sa.Column("id", sa.Uuid()))
    table = sa.Table(
        "relationship_history", metadata,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(50), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_relationship_history")),
        sa.ForeignKeyConstraint(["workspace_id", "relationship_id"],
                                ["relationship.workspace_id", "relationship.id"],
                                name="fk_relationship_history_workspace_relationship"),
        sa.CheckConstraint("state = trim(state) AND length(state) BETWEEN 1 AND 50",
                           name=op.f("ck_relationship_history_state_trimmed_length")),
    )
    sa.Index("ix_relationship_history_workspace_relationship_order", table.c.workspace_id,
             table.c.relationship_id, table.c.effective_at, table.c.created_at, table.c.id)
    if include_event:
        table.append_column(sa.Column("event_id", sa.Uuid(), nullable=True))
        table.append_constraint(sa.ForeignKeyConstraint(
            ["workspace_id", "event_id"], ["event.workspace_id", "event.id"],
            name="fk_relationship_history_workspace_event"))
        sa.Index("ix_relationship_history_workspace_event", table.c.workspace_id, table.c.event_id)
    return table


def upgrade() -> None:
    op.create_table(
        "event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("event_type", sa.String(200), nullable=False),
        sa.Column("record_kind", sa.String(20), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event")),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], name="fk_event_workspace_id_workspace"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_event_workspace_id_id"),
        sa.CheckConstraint("title = trim(title) AND length(title) BETWEEN 1 AND 200",
                           name=op.f("ck_event_title_trimmed_length")),
        sa.CheckConstraint("event_type = trim(event_type) AND length(event_type) BETWEEN 1 AND 200",
                           name=op.f("ck_event_type_trimmed_length")),
        sa.CheckConstraint("record_kind IN ('FACT', 'OBSERVATION')", name=op.f("ck_event_record_kind_valid")),
        sa.CheckConstraint(
            "(occurred_on IS NOT NULL AND started_at IS NULL AND ended_at IS NULL) OR "
            "(occurred_on IS NULL AND started_at IS NOT NULL AND (ended_at IS NULL OR ended_at >= started_at))",
            name=op.f("ck_event_time_valid")),
    )
    op.create_index("ix_event_workspace_started", "event", ["workspace_id", "started_at"])
    op.create_index("ix_event_workspace_occurred", "event", ["workspace_id", "occurred_on"])
    op.create_index("ix_event_workspace_deleted", "event", ["workspace_id", "deleted_at"])
    op.create_index("ix_event_workspace_type", "event", ["workspace_id", "event_type"])
    op.create_table(
        "event_participant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(200), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_participant")),
        sa.ForeignKeyConstraint(["workspace_id", "event_id"], ["event.workspace_id", "event.id"],
                                name="fk_event_participant_workspace_event"),
        sa.ForeignKeyConstraint(["workspace_id", "person_id"], ["person.workspace_id", "person.id"],
                                name="fk_event_participant_workspace_person"),
        sa.UniqueConstraint("event_id", "person_id", name="uq_event_participant_event_person"),
    )
    op.create_index("ix_event_participant_workspace_event", "event_participant", ["workspace_id", "event_id"])
    op.create_index("ix_event_participant_workspace_person", "event_participant", ["workspace_id", "person_id"])
    # History has no inbound FK: SQLite can rebuild it while keeping FK enforcement ON.
    # Reflected CHECK names are already convention-expanded; batch preserves them.
    source = _history_copy_from(include_event=False) if op.get_context().as_sql else None
    with op.batch_alter_table("relationship_history", recreate="auto", copy_from=source) as batch:
        batch.add_column(sa.Column("event_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key("fk_relationship_history_workspace_event", "event",
                                 ["workspace_id", "event_id"], ["workspace_id", "id"])
        batch.create_index("ix_relationship_history_workspace_event", ["workspace_id", "event_id"])


def downgrade() -> None:
    source = _history_copy_from(include_event=True) if op.get_context().as_sql else None
    with op.batch_alter_table("relationship_history", recreate="auto", copy_from=source) as batch:
        batch.drop_index("ix_relationship_history_workspace_event")
        batch.drop_constraint("fk_relationship_history_workspace_event", type_="foreignkey")
        batch.drop_column("event_id")
    op.drop_index("ix_event_participant_workspace_person", table_name="event_participant")
    op.drop_index("ix_event_participant_workspace_event", table_name="event_participant")
    op.drop_table("event_participant")
    op.drop_index("ix_event_workspace_type", table_name="event")
    op.drop_index("ix_event_workspace_deleted", table_name="event")
    op.drop_index("ix_event_workspace_occurred", table_name="event")
    op.drop_index("ix_event_workspace_started", table_name="event")
    op.drop_table("event")
