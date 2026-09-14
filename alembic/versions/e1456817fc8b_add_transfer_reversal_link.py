"""add transfer reversal link

Revision ID: e1456817fc8b
Revises: b910a66caa4f
Create Date: 2026-09-14 14:04:46.706108

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1456817fc8b'
down_revision: Union[str, Sequence[str], None] = 'b910a66caa4f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('transfers', sa.Column('reversal_of_transfer_id', sa.String(length=36), nullable=True))
    op.create_foreign_key(
        'fk_transfers_reversal_of_transfer_id', 'transfers', 'transfers', ['reversal_of_transfer_id'], ['id']
    )
    op.create_unique_constraint('uq_transfers_reversal_of_transfer_id', 'transfers', ['reversal_of_transfer_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('uq_transfers_reversal_of_transfer_id', 'transfers', type_='unique')
    op.drop_constraint('fk_transfers_reversal_of_transfer_id', 'transfers', type_='foreignkey')
    op.drop_column('transfers', 'reversal_of_transfer_id')
