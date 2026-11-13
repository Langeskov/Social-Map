"""Add the rebuildable search projection (search_document + FTS5 index).

Revision ID: 0006_search_projection
Revises: 0005_evidence

This migration adds PROJECTION / SEARCH INFRASTRUCTURE only. It creates no
business fact table and does not touch any fact table (0001-0005 are unchanged).
The FTS5 index can be dropped and rebuilt at any time from person, person_alias,
event and evidence, so it is never a source of truth.
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_search_projection"
down_revision = "0005_evidence"
branch_labels = None
depends_on = None

FTS_COLUMNS = ("title_tokens", "body_tokens")


def _require_fts5(connection) -> None:
    """Fail with a clear message instead of an opaque SQL error."""
    options = [row[0] for row in connection.exec_driver_sql("PRAGMA compile_options")]
    if not any("FTS5" in str(option).upper() for option in options):
        raise RuntimeError(
            "此 SQLite 构建未启用 FTS5，Phase 7 搜索投影无法创建。"
            "请使用启用 FTS5 的 SQLite（本项目验证环境为 3.53.2）。")


def upgrade() -> None:
    context = op.get_context()
    if context.as_sql:
        # Offline rendering cannot run PRAGMA; emit the same DDL.
        _create_tables()
        return
    _require_fts5(context.bind)
    _create_tables()


def _create_tables() -> None:
    op.create_table(
        "search_document",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(20), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("title_tokens", sa.Text(), nullable=False),
        sa.Column("body_tokens", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_document")),
        sa.UniqueConstraint("workspace_id", "entity_type", "entity_id",
                            name="uq_search_document_workspace_entity"),
        sa.CheckConstraint("entity_type IN ('PERSON','ALIAS','EVENT','EVIDENCE')",
                           name=op.f("ck_search_document_entity_type_valid")),
    )
    op.create_index("ix_search_document_workspace_type", "search_document",
                    ["workspace_id", "entity_type"])
    op.create_index("ix_search_document_workspace_deleted", "search_document",
                    ["workspace_id", "deleted_at"])
    columns = ", ".join(FTS_COLUMNS)
    # FTS5 DDL goes through op.execute() so the offline (--sql) path renders it too;
    # op.get_bind().exec_driver_sql() has no offline counterpart (MockConnection).
    # Note: DROPping this virtual table needs a writable SQLite temporary directory,
    # otherwise SQLite answers SQLITE_CANTOPEN (see conftest.py).
    op.execute(f"CREATE VIRTUAL TABLE search_document_fts USING fts5({columns})")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS search_document_fts")
    op.drop_index("ix_search_document_workspace_deleted", table_name="search_document")
    op.drop_index("ix_search_document_workspace_type", table_name="search_document")
    op.drop_table("search_document")
