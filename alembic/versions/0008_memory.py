"""Add the confirmed memory table.

Revision ID: 0008_memory
Revises: 0007_analysis

Phase 9 stores what the user has *confirmed*: a fact, a preference or background
context about one person or about a whole workspace. A memory is a business fact of
its own kind, never a model answer - the analysis layer may only propose text, and
the row exists here only after confirmation, which is why ``confirmed_at`` is NOT
NULL.

One table is created and nothing else is touched. The eleven fact tables of
0001-0005 keep their exact shape and rows, 0006's search projection (including the
FTS5 virtual table) is left alone, and 0007's analysis ledger keeps its four
provenance tables. ``person`` and ``analysis_run`` both already carry
UNIQUE(workspace_id, id), which the two composite foreign keys below require as
their target; neither table is rewritten here.

The table is declared with frozen ``sa`` types and explicitly named constraints
(``op.f`` is used only where the mapping's naming convention derives the name) so
the migrated schema matches the ORM metadata exactly. No data is written.
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_memory"
down_revision = "0007_analysis"
branch_labels = None
depends_on = None

# Frozen value sets, kept in step with the mapped model's CHECK constraints.
KINDS = ("FACT", "PREFERENCE", "CONTEXT")
SOURCE_TYPES = ("MANUAL", "ANALYSIS")

# Index name -> columns, in creation order; the downgrade walks this reversed.
MEMORY_INDEXES = (
    ("ix_memory_workspace_person", ("workspace_id", "person_id")),
    ("ix_memory_workspace_kind", ("workspace_id", "kind")),
    ("ix_memory_workspace_deleted", ("workspace_id", "deleted_at")),
    ("ix_memory_workspace_valid", ("workspace_id", "valid_from", "valid_until")),
    ("ix_memory_workspace_analysis", ("workspace_id", "source_analysis_id")),
)

SOURCE_REFERENCE_CHECK = (
    "(source_type = 'MANUAL' AND source_analysis_id IS NULL) "
    "OR (source_type = 'ANALYSIS' AND source_analysis_id IS NOT NULL)")
CONTENT_CHECK = "content = trim(content) AND length(content) BETWEEN 1 AND 4000"
VALIDITY_CHECK = "valid_from IS NULL OR valid_until IS NULL OR valid_until >= valid_from"


def upgrade() -> None:
    op.create_table(
        "memory",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        # NULL means a Workspace-wide memory rather than one about a person.
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(20), nullable=False),
        sa.Column("source_analysis_id", sa.Uuid(), nullable=True),
        # A memory exists only after confirmation, so this column is never NULL.
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory")),
        # Composite keys: a person or a run from another workspace is unreachable.
        # Both targets already carry UNIQUE(workspace_id, id).
        sa.ForeignKeyConstraint(["workspace_id", "person_id"],
                                ["person.workspace_id", "person.id"],
                                name="fk_memory_workspace_person"),
        sa.ForeignKeyConstraint(["workspace_id", "source_analysis_id"],
                                ["analysis_run.workspace_id", "analysis_run.id"],
                                name="fk_memory_workspace_analysis_run"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"],
                                name="fk_memory_workspace_id_workspace"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_memory_workspace_id_id"),
        sa.CheckConstraint("kind IN ('FACT','PREFERENCE','CONTEXT')",
                           name=op.f("ck_memory_kind_valid")),
        sa.CheckConstraint(CONTENT_CHECK, name=op.f("ck_memory_content_normalized")),
        sa.CheckConstraint("source_type IN ('MANUAL','ANALYSIS')",
                           name=op.f("ck_memory_source_type_valid")),
        sa.CheckConstraint(SOURCE_REFERENCE_CHECK,
                           name=op.f("ck_memory_source_reference_valid")),
        sa.CheckConstraint(VALIDITY_CHECK, name=op.f("ck_memory_validity_window_valid")),
    )
    for name, columns in MEMORY_INDEXES:
        op.create_index(name, "memory", list(columns))


def downgrade() -> None:
    # Reverse creation order: every index first, then the table itself.
    for name, _columns in reversed(MEMORY_INDEXES):
        op.drop_index(name, table_name="memory")
    op.drop_table("memory")
