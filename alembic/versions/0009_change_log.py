"""Add the append-only ChangeLog table.

Revision ID: 0009_change_log
Revises: 0008_memory

Phase 14 records *what the user actually did* to business entities: one row per
real write, grouped by operation_id, with whitelisted canonical JSON snapshots.
A revision is a read-time view over rows of one entity ordered by revision_no, so
no second revision table is created.

This migration only creates one new table and its indexes. The eleven business
fact tables (0001-0005), the search projection (0006), the analysis ledger (0007)
and the memory table (0008) keep their exact shape and rows. ``workspace_id`` is
a real foreign key; the polymorphic ``entity_type/entity_id`` reference cannot be
a database foreign key in SQLite and is validated by the writer (Phase 14.2) and
the integrity audit (Phase 14.4). No rows are written by the migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_change_log"
down_revision = "0008_memory"
branch_labels = None
depends_on = None

# Frozen value sets, kept in step with the mapped model's CHECK constraints.
ENTITY_TYPES = ("WORKSPACE", "PERSON", "RELATIONSHIP", "RELATIONSHIP_HISTORY",
                "EVENT", "EVIDENCE", "MEDIA", "MEMORY")
ACTIONS = ("CREATE", "UPDATE", "ARCHIVE", "RESTORE", "IMPORT", "APPEND_HISTORY",
           "ATTACH_EVENT", "DETACH_EVENT", "REVERT")

# Index name -> columns, in creation order; the downgrade walks this reversed.
CHANGE_LOG_INDEXES = (
    ("ix_change_log_workspace_entity",
     ("workspace_id", "entity_type", "entity_id", "revision_no")),
    ("ix_change_log_workspace_operation", ("workspace_id", "operation_id")),
    ("ix_change_log_workspace_recorded", ("workspace_id", "recorded_at", "id")),
)


def _values(values) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "change_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(30), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        # Aggregate parent for history views; NULL when the entity is its own root.
        sa.Column("parent_entity_type", sa.String(30), nullable=True),
        sa.Column("parent_entity_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        # Canonical JSON array of whitelisted business field names.
        sa.Column("changed_fields", sa.Text(), nullable=False),
        # Canonical JSON objects; before is NULL only for CREATE/IMPORT.
        sa.Column("before_snapshot", sa.Text(), nullable=True),
        sa.Column("after_snapshot", sa.Text(), nullable=True),
        sa.Column("payload_version", sa.String(10), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_change_log")),
        # Real foreign key: a history row can never belong to a missing workspace.
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"],
                                name="fk_change_log_workspace_id_workspace"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_change_log_workspace_id_id"),
        sa.UniqueConstraint("workspace_id", "entity_type", "entity_id", "revision_no",
                            name="uq_change_log_workspace_entity_revision"),
        sa.CheckConstraint(f"entity_type IN ({_values(ENTITY_TYPES)})",
                           name=op.f("ck_change_log_entity_type_valid")),
        sa.CheckConstraint(f"action IN ({_values(ACTIONS)})",
                           name=op.f("ck_change_log_action_valid")),
        sa.CheckConstraint("revision_no >= 1",
                           name=op.f("ck_change_log_revision_positive")),
        sa.CheckConstraint(f"parent_entity_type IN ({_values(ENTITY_TYPES)}) "
                           "OR parent_entity_type IS NULL",
                           name=op.f("ck_change_log_parent_type_valid")),
        sa.CheckConstraint("(parent_entity_type IS NULL) = (parent_entity_id IS NULL)",
                           name=op.f("ck_change_log_parent_pair_valid")),
        sa.CheckConstraint("length(payload_version) > 0",
                           name=op.f("ck_change_log_payload_version_nonempty")),
        sa.CheckConstraint("length(trim(changed_fields)) > 0",
                           name=op.f("ck_change_log_changed_fields_present")),
    )
    for name, columns in CHANGE_LOG_INDEXES:
        op.create_index(name, "change_log", list(columns))


def downgrade() -> None:
    # Reverse creation order: every index first, then the table itself.
    for name, _columns in reversed(CHANGE_LOG_INDEXES):
        op.drop_index(name, table_name="change_log")
    op.drop_table("change_log")
