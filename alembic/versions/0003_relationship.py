"""Add relationship and append-only history schema independently of application models.

Revision ID: 0003_relationship
Revises: 0002_workspace_person_media
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_relationship"
down_revision = "0002_workspace_person_media"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "relationship",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("person_a_id", sa.Uuid(), nullable=False),
        sa.Column("person_b_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_type", sa.String(200), nullable=False),
        sa.Column("directionality", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_relationship")),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"],
                                name="fk_relationship_workspace_id_workspace"),
        sa.ForeignKeyConstraint(["workspace_id", "person_a_id"],
                                ["person.workspace_id", "person.id"],
                                name="fk_relationship_workspace_person_a"),
        sa.ForeignKeyConstraint(["workspace_id", "person_b_id"],
                                ["person.workspace_id", "person.id"],
                                name="fk_relationship_workspace_person_b"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_relationship_workspace_id_id"),
        sa.UniqueConstraint("workspace_id", "person_a_id", "person_b_id", "relationship_type", "directionality",
                            name="uq_relationship_workspace_pair_type_directionality"),
        sa.CheckConstraint("person_a_id <> person_b_id", name=op.f("ck_relationship_people_distinct")),
        sa.CheckConstraint("directionality IN ('UNDIRECTED', 'DIRECTED')",
                           name=op.f("ck_relationship_directionality_valid")),
        sa.CheckConstraint("directionality <> 'UNDIRECTED' OR person_a_id < person_b_id",
                           name=op.f("ck_relationship_undirected_sorted")),
        sa.CheckConstraint("relationship_type = trim(relationship_type) AND length(relationship_type) BETWEEN 1 AND 200",
                           name=op.f("ck_relationship_type_trimmed_length")),
    )
    op.create_index("ix_relationship_workspace_person_a", "relationship", ["workspace_id", "person_a_id"])
    op.create_index("ix_relationship_workspace_person_b", "relationship", ["workspace_id", "person_b_id"])
    op.create_index("ix_relationship_workspace_type", "relationship", ["workspace_id", "relationship_type"])
    op.create_index("ix_relationship_workspace_deleted", "relationship", ["workspace_id", "deleted_at"])
    op.create_table(
        "relationship_history",
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
    op.create_index("ix_relationship_history_workspace_relationship_order", "relationship_history",
                    ["workspace_id", "relationship_id", "effective_at", "created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_relationship_history_workspace_relationship_order", table_name="relationship_history")
    op.drop_table("relationship_history")
    op.drop_index("ix_relationship_workspace_deleted", table_name="relationship")
    op.drop_index("ix_relationship_workspace_type", table_name="relationship")
    op.drop_index("ix_relationship_workspace_person_b", table_name="relationship")
    op.drop_index("ix_relationship_workspace_person_a", table_name="relationship")
    op.drop_table("relationship")
