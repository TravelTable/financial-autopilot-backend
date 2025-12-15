"""init
Revision ID: 0001_init
Revises:
Create Date: 2025-12-15
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "google_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("google_user_id", sa.String(length=128), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token_enc", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False, server_default=""),
        sa.Column("token_expiry_utc", sa.DateTime(), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("last_history_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "google_user_id", name="uq_user_google"),
    )
    op.create_index("ix_google_accounts_user_id", "google_accounts", ["user_id"])
    op.create_index("ix_google_accounts_google_user_id", "google_accounts", ["google_user_id"])
    op.create_index("ix_google_accounts_email", "google_accounts", ["email"])

    op.create_table(
        "emails_index",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("google_account_id", sa.Integer(), sa.ForeignKey("google_accounts.id"), nullable=False),
        sa.Column("gmail_message_id", sa.String(length=128), nullable=False),
        sa.Column("gmail_thread_id", sa.String(length=128), nullable=True),
        sa.Column("internal_date_ms", sa.Integer(), nullable=False),
        sa.Column("from_email", sa.String(length=320), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("processed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("google_account_id", "gmail_message_id", name="uq_gmail_msg"),
    )
    op.create_index("ix_emails_index_google_account_id", "emails_index", ["google_account_id"])
    op.create_index("ix_emails_index_gmail_message_id", "emails_index", ["gmail_message_id"])

    op.create_table(
        "vendors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("canonical_name", sa.String(length=256), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("domains", sa.JSON(), nullable=True),
        sa.Column("support_email", sa.String(length=320), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_vendors_canonical_name", "vendors", ["canonical_name"], unique=True)

    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("google_account_id", sa.Integer(), sa.ForeignKey("google_accounts.id"), nullable=False),
        sa.Column("gmail_message_id", sa.String(length=128), nullable=False),
        sa.Column("vendor", sa.String(length=256), nullable=True),
        sa.Column("vendor_id", sa.Integer(), sa.ForeignKey("vendors.id"), nullable=True),
        sa.Column("amount", sa.Numeric(12,2), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("transaction_date", sa.Date(), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("is_subscription", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("trial_end_date", sa.Date(), nullable=True),
        sa.Column("renewal_date", sa.Date(), nullable=True),
        sa.Column("confidence", sa.JSON(), nullable=True),
        sa.Column("parser_version", sa.String(length=32), nullable=False, server_default="v1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("google_account_id", "gmail_message_id", name="uq_tx_gmail_msg"),
    )
    op.create_index("ix_transactions_user_id", "transactions", ["user_id"])
    op.create_index("ix_transactions_google_account_id", "transactions", ["google_account_id"])
    op.create_index("ix_transactions_gmail_message_id", "transactions", ["gmail_message_id"])

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("vendor_id", sa.Integer(), sa.ForeignKey("vendors.id"), nullable=True),
        sa.Column("vendor_name", sa.String(length=256), nullable=False),
        sa.Column("amount", sa.Numeric(12,2), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("billing_cycle_days", sa.Integer(), nullable=True),
        sa.Column("last_charge_date", sa.Date(), nullable=True),
        sa.Column("next_renewal_date", sa.Date(), nullable=True),
        sa.Column("trial_end_date", sa.Date(), nullable=True),
        sa.Column("status", sa.Enum("active","canceled","ignored", name="subscriptionstatus"), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    op.create_index("ix_subscriptions_vendor_name", "subscriptions", ["vendor_name"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("type", sa.Enum("trial","renewal","price_increase","anomaly", name="notificationtype"), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index("ix_notifications_scheduled_for", "notifications", ["scheduled_for"])

    op.create_table(
        "ai_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

def downgrade():
    op.drop_table("audit_log")
    op.drop_table("ai_runs")
    op.drop_table("notifications")
    op.drop_table("subscriptions")
    op.drop_table("transactions")
    op.drop_table("vendors")
    op.drop_table("emails_index")
    op.drop_table("google_accounts")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    op.execute("DROP TYPE IF EXISTS subscriptionstatus")
    op.execute("DROP TYPE IF EXISTS notificationtype")
