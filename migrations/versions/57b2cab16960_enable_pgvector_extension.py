"""enable pgvector extension

Revision ID: 57b2cab16960
Revises: 301e7f0d190f
Create Date: 2026-09-27 17:24:25.001764

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '57b2cab16960'
down_revision: Union[str, Sequence[str], None] = '301e7f0d190f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP EXTENSION IF EXISTS vector")
