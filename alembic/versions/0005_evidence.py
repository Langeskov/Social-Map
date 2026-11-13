"""Add captured evidence and its media bindings using frozen SQL types.

Revision ID: 0005_evidence
Revises: 0004_event
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_evidence"
down_revision = "0004_event"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("annotation", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(20), nullable=False),
        sa.Column("source_label", sa.String(300), nullable=True),
        sa.Column("source_reference", sa.Text(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence")),
        sa.ForeignKeyConstraint(["workspace_id", "event_id"], ["event.workspace_id", "event.id"],
                                name="fk_evidence_workspace_event"),
        # Required as the composite foreign key target of evidence_media.
        sa.UniqueConstraint("workspace_id", "id", name="uq_evidence_workspace_id_id"),
        sa.CheckConstraint("kind IN ('TEXT','IMAGE','FILE','LINK','MANUAL','OTHER')",
                           name=op.f("ck_evidence_kind_valid")),
        sa.CheckConstraint("source_type IN ('CHAT','EMAIL','DOCUMENT','IMAGE','FILE','WEB','MANUAL','OTHER')",
                           name=op.f("ck_evidence_source_type_valid")),
        sa.CheckConstraint("title = trim(title) AND length(title) BETWEEN 1 AND 200",
                           name=op.f("ck_evidence_title_trimmed_length")),
        sa.CheckConstraint("source_label IS NULL OR length(source_label) BETWEEN 1 AND 300",
                           name=op.f("ck_evidence_source_label_length")),
    )
    op.create_index("ix_evidence_workspace_event", "evidence", ["workspace_id", "event_id"])
    op.create_index("ix_evidence_workspace_kind", "evidence", ["workspace_id", "kind"])
    op.create_index("ix_evidence_workspace_deleted", "evidence", ["workspace_id", "deleted_at"])
    op.create_index("ix_evidence_workspace_created", "evidence", ["workspace_id", "created_at"])
    op.create_table(
        "evidence_media",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("media_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_media")),
        sa.ForeignKeyConstraint(["workspace_id", "evidence_id"], ["evidence.workspace_id", "evidence.id"],
                                name="fk_evidence_media_workspace_evidence"),
        sa.ForeignKeyConstraint(["workspace_id", "media_id"], ["media.workspace_id", "media.id"],
                                name="fk_evidence_media_workspace_media"),
        sa.UniqueConstraint("evidence_id", "media_id", "role", name="uq_evidence_media_evidence_media_role"),
    )
    op.create_index("ix_evidence_media_workspace_evidence", "evidence_media", ["workspace_id", "evidence_id"])
    op.create_index("ix_evidence_media_workspace_media", "evidence_media", ["workspace_id", "media_id"])
    # SQL treats NULLs as distinct, so UNIQUE(evidence_id, media_id, role) accepts repeated
    # NULL roles; this partial unique index closes that gap on both supported dialects.
    op.create_index("uq_evidence_media_evidence_media_norole", "evidence_media",
                    ["evidence_id", "media_id"], unique=True,
                    sqlite_where=sa.text("role IS NULL"), postgresql_where=sa.text("role IS NULL"))


def downgrade() -> None:
    op.drop_index("uq_evidence_media_evidence_media_norole", table_name="evidence_media",
                  sqlite_where=sa.text("role IS NULL"), postgresql_where=sa.text("role IS NULL"))
    op.drop_index("ix_evidence_media_workspace_media", table_name="evidence_media")
    op.drop_index("ix_evidence_media_workspace_evidence", table_name="evidence_media")
    op.drop_table("evidence_media")
    op.drop_index("ix_evidence_workspace_created", table_name="evidence")
    op.drop_index("ix_evidence_workspace_deleted", table_name="evidence")
    op.drop_index("ix_evidence_workspace_kind", table_name="evidence")
    op.drop_index("ix_evidence_workspace_event", table_name="evidence")
    op.drop_table("evidence")
