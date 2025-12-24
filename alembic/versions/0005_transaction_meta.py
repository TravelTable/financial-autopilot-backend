"""Add meta column to transactions

Revision ID: 0005_transaction_meta
Revises: 0004_sync_status_fields
Create Date: 2025-12-24
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_transaction_meta"
down_revision = "0004_sync_status_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("transactions", sa.Column("meta", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("transactions", "meta")
