"""support manual journal entries

Revision ID: ee1ad1a4a544
Revises: e1456817fc8b
Create Date: 2026-09-14 14:08:08.650875

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ee1ad1a4a544'
down_revision: Union[str, Sequence[str], None] = 'e1456817fc8b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('ledger_transactions', 'transfer_id', existing_type=sa.String(length=36), nullable=True)
    op.add_column('ledger_transactions', sa.Column('memo', sa.String(length=1024), nullable=True))
    op.add_column('ledger_transactions', sa.Column('entered_by', sa.String(length=36), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('ledger_transactions', 'entered_by')
    op.drop_column('ledger_transactions', 'memo')
    op.alter_column('ledger_transactions', 'transfer_id', existing_type=sa.String(length=36), nullable=False)
