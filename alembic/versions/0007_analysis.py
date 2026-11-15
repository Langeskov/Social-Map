"""Add the analysis run ledger and its four source provenance tables.

Revision ID: 0007_analysis
Revises: 0006_search_projection

Phase 8 records *how* an answer was produced, it does not add any business fact.
The eleven fact tables of 0001-0005 keep their exact shape and data; 0006's search
projection (including the FTS5 virtual table) is untouched by this revision.

Five tables are created:

* ``analysis_run`` - one model invocation: the frozen request scope, the context
  snapshot that was actually sent, the raw answer and the lifecycle timestamps.
  It deliberately carries NO credential column: no API key, token or password is
  ever persisted beside a run.
* ``analysis_person_source`` / ``analysis_relationship_source`` /
  ``analysis_event_source`` / ``analysis_evidence_source`` - which facts a run
  consumed. Each row is provenance only. The status of a run never rewrites the
  facts it read.

Every table is declared with frozen ``sa`` types and explicitly named constraints
(``op.f`` is used only where the mapping's naming convention derives the name) so
the migrated schema matches the ORM metadata exactly. No data is written and no
FTS5 object is touched.
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_analysis"
down_revision = "0006_search_projection"
branch_labels = None
depends_on = None

TARGET_TYPES = ("PERSON", "RELATIONSHIP", "EVENT")
ANALYSIS_TYPES = ("PERSON_PROFILE", "RELATIONSHIP_ANALYSIS", "EVENT_ANALYSIS")
STATUSES = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED")

# One association table per fact kind a run may consume, in downgrade order
# (last created, first dropped). ``target`` is the fact table the composite
# foreign key of (workspace_id, target_id) points at; every one of them already
# carries UNIQUE(workspace_id, id), which a composite key requires.
SOURCE_TABLES = (
    ("analysis_person_source", "person"),
    ("analysis_relationship_source", "relationship"),
    ("analysis_event_source", "event"),
    ("analysis_evidence_source", "evidence"),
)


def upgrade() -> None:
    op.create_table(
        "analysis_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(20), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_type", sa.String(30), nullable=False),
        sa.Column("provider_name", sa.String(100), nullable=False),
        sa.Column("model_name", sa.String(200), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("context_snapshot", sa.Text(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_run")),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"],
                                name="fk_analysis_run_workspace_id_workspace"),
        # Required as the composite foreign key target of the four source tables.
        sa.UniqueConstraint("workspace_id", "id", name="uq_analysis_run_workspace_id_id"),
        sa.CheckConstraint("target_type IN ('PERSON','RELATIONSHIP','EVENT')",
                           name=op.f("ck_analysis_run_target_type_valid")),
        sa.CheckConstraint(
            "analysis_type IN ('PERSON_PROFILE','RELATIONSHIP_ANALYSIS','EVENT_ANALYSIS')",
            name=op.f("ck_analysis_run_analysis_type_valid")),
        sa.CheckConstraint("status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')",
                           name=op.f("ck_analysis_run_status_valid")),
        sa.CheckConstraint("duration_ms IS NULL OR duration_ms >= 0",
                           name=op.f("ck_analysis_run_duration_nonnegative")),
    )
    op.create_index("ix_analysis_run_workspace_created", "analysis_run",
                    ["workspace_id", "created_at"])
    op.create_index("ix_analysis_run_workspace_target", "analysis_run",
                    ["workspace_id", "target_type", "target_id"])
    op.create_index("ix_analysis_run_workspace_status", "analysis_run",
                    ["workspace_id", "status"])
    for name, target in SOURCE_TABLES:
        _create_source_table(name, target)


def _create_source_table(name: str, target: str) -> None:
    """Create one provenance table; all four differ only in their fact target."""
    op.create_table(
        name,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{name}")),
        sa.ForeignKeyConstraint(["workspace_id", "analysis_run_id"],
                                ["analysis_run.workspace_id", "analysis_run.id"],
                                name=f"fk_{name}_workspace_run"),
        sa.ForeignKeyConstraint(["workspace_id", "target_id"],
                                [f"{target}.workspace_id", f"{target}.id"],
                                name=f"fk_{name}_workspace_{target}"),
        sa.UniqueConstraint("analysis_run_id", "target_id", name=f"uq_{name}_run_target"),
    )
    op.create_index(f"ix_{name}_workspace_run", name, ["workspace_id", "analysis_run_id"])
    op.create_index(f"ix_{name}_workspace_target", name, ["workspace_id", "target_id"])


def downgrade() -> None:
    # Reverse creation order: the four source tables first, ``analysis_run`` last,
    # because their composite foreign key depends on it.
    for name, _target in reversed(SOURCE_TABLES):
        op.drop_index(f"ix_{name}_workspace_target", table_name=name)
        op.drop_index(f"ix_{name}_workspace_run", table_name=name)
        op.drop_table(name)
    op.drop_index("ix_analysis_run_workspace_status", table_name="analysis_run")
    op.drop_index("ix_analysis_run_workspace_target", table_name="analysis_run")
    op.drop_index("ix_analysis_run_workspace_created", table_name="analysis_run")
    op.drop_table("analysis_run")
