"""track staged candidate cleanup

Revision ID: e87c9a14d2f1
Revises: 1012f991b595
Create Date: 2026-09-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e87c9a14d2f1"
down_revision: Union[str, Sequence[str], None] = (
    "1012f991b595"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "document_indexing_jobs",
        sa.Column(
            "staged_candidate_deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_indexing_jobs_failed_cleanup",
        "document_indexing_jobs",
        [
            "status",
            "staged_candidate_deleted_at",
            "finished_at",
        ],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_indexing_jobs_failed_cleanup",
        table_name="document_indexing_jobs",
        if_exists=True,
    )
    op.drop_column(
        "document_indexing_jobs",
        "staged_candidate_deleted_at",
    )
