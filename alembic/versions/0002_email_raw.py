"""add emails_raw table

Revision ID: 0002_email_raw
Revises: 0001_init
Create Date: 2025-12-16
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_email_raw"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "emails_raw",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("google_account_id", sa.Integer(), sa.ForeignKey("google_accounts.id"), nullable=False),
        sa.Column("gmail_message_id", sa.String(length=128), nullable=False),
        sa.Column("gmail_thread_id", sa.String(length=128), nullable=True),
        sa.Column("internal_date_ms", sa.BigInteger(), nullable=False),
        sa.Column("headers_json", sa.JSON(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("text_plain", sa.Text(), nullable=True),
        sa.Column("text_html", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("google_account_id", "gmail_message_id", name="uq_raw_gmail_msg"),
    )
    op.create_index("ix_emails_raw_google_account_id", "emails_raw", ["google_account_id"])
    op.create_index("ix_emails_raw_gmail_message_id", "emails_raw", ["gmail_message_id"])


def downgrade():
    op.drop_index("ix_emails_raw_gmail_message_id", table_name="emails_raw")
    op.drop_index("ix_emails_raw_google_account_id", table_name="emails_raw")
    op.drop_table("emails_raw")
