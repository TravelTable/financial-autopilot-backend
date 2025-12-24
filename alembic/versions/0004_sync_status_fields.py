"""Add sync status fields to google_accounts

Revision ID: 0004_sync_status_fields
Revises: 0003_internal_date_ms_bigint
Create Date: 2025-12-24
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_sync_status_fields"
down_revision = "0003_internal_date_ms_bigint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("google_accounts", sa.Column("sync_state", sa.String(length=32), nullable=True))
    op.add_column("google_accounts", sa.Column("sync_started_at", sa.DateTime(), nullable=True))
    op.add_column("google_accounts", sa.Column("sync_completed_at", sa.DateTime(), nullable=True))
    op.add_column("google_accounts", sa.Column("sync_failed_at", sa.DateTime(), nullable=True))
    op.add_column("google_accounts", sa.Column("sync_error_message", sa.Text(), nullable=True))
    op.add_column(
        "google_accounts",
        sa.Column("sync_queued", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "google_accounts",
        sa.Column("sync_in_progress", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("google_accounts", "sync_in_progress")
    op.drop_column("google_accounts", "sync_queued")
    op.drop_column("google_accounts", "sync_error_message")
    op.drop_column("google_accounts", "sync_failed_at")
    op.drop_column("google_accounts", "sync_completed_at")
    op.drop_column("google_accounts", "sync_started_at")
    op.drop_column("google_accounts", "sync_state")
