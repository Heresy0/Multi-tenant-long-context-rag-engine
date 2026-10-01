"""add tenant document quotas

Revision ID: d2f4a6c8e103
Revises: 6d8a9f2c1b40
Create Date: 2026-10-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d2f4a6c8e103"
down_revision: Union[str, Sequence[str], None] = (
    "6d8a9f2c1b40"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "max_document_count",
            sa.Integer(),
            server_default=sa.text("1000"),
            nullable=False,
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "max_storage_bytes",
            sa.BigInteger(),
            server_default=sa.text("10737418240"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_tenants_max_document_count",
        "tenants",
        "max_document_count >= 1",
    )
    op.create_check_constraint(
        "ck_tenants_max_storage_bytes",
        "tenants",
        "max_storage_bytes >= 1",
    )

    op.add_column(
        "documents",
        sa.Column(
            "file_size_bytes",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_documents_file_size_bytes",
        "documents",
        "file_size_bytes >= 0",
    )

    op.add_column(
        "document_indexing_jobs",
        sa.Column(
            "candidate_size_bytes",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "document_indexing_jobs",
        sa.Column(
            "reservation_released_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_indexing_jobs_candidate_size_bytes",
        "document_indexing_jobs",
        "candidate_size_bytes >= 0",
    )
    op.create_index(
        "ix_indexing_jobs_tenant_reservation",
        "document_indexing_jobs",
        ["tenant_id", "reservation_released_at"],
    )

    # 历史任务没有候选文件大小可回填。已经成功或已经清理
    # 候选文件的任务不应继续被视为未释放预留。
    op.execute(
        """
        UPDATE document_indexing_jobs
        SET reservation_released_at = COALESCE(
            finished_at,
            updated_at,
            created_at
        )
        WHERE status = 'succeeded'
           OR staged_candidate_deleted_at IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_indexing_jobs_tenant_reservation",
        table_name="document_indexing_jobs",
    )
    op.drop_constraint(
        "ck_indexing_jobs_candidate_size_bytes",
        "document_indexing_jobs",
        type_="check",
    )
    op.drop_column(
        "document_indexing_jobs",
        "reservation_released_at",
    )
    op.drop_column(
        "document_indexing_jobs",
        "candidate_size_bytes",
    )

    op.drop_constraint(
        "ck_documents_file_size_bytes",
        "documents",
        type_="check",
    )
    op.drop_column("documents", "file_size_bytes")

    op.drop_constraint(
        "ck_tenants_max_storage_bytes",
        "tenants",
        type_="check",
    )
    op.drop_constraint(
        "ck_tenants_max_document_count",
        "tenants",
        type_="check",
    )
    op.drop_column("tenants", "max_storage_bytes")
    op.drop_column("tenants", "max_document_count")
