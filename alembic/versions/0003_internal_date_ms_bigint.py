"""Make emails_index.internal_date_ms BIGINT

Revision ID: 0003_internal_date_ms_bigint
Revises: 0002_email_raw
Create Date: 2025-12-23
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_internal_date_ms_bigint"
down_revision = "0002_email_raw"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "emails_index",
        "internal_date_ms",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        postgresql_using="internal_date_ms::bigint",
    )


def downgrade() -> None:
    op.alter_column(
        "emails_index",
        "internal_date_ms",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        postgresql_using="internal_date_ms::integer",
    )
