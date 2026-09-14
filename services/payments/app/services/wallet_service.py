from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain import ActorType, AuditAggregateType
from ..infrastructure.models import WalletModel
from .audit_service import write_audit_event


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WalletServiceError(Exception):
    pass


class WalletMissingError(WalletServiceError):
    pass


class InvalidWalletTransitionError(WalletServiceError):
    pass


class WalletNotEmptyError(WalletServiceError):
    pass


class WalletService:
    _allowed_transitions = {
        "ACTIVE": {"FROZEN", "CLOSED"},
        "FROZEN": {"ACTIVE", "CLOSED"},
    }

    def __init__(self, session: Session):
        self.session = session

    def update_status(self, wallet_id: str, *, status: str, actor_id: str) -> WalletModel:
        wallet = self.session.scalar(select(WalletModel).where(WalletModel.id == wallet_id).with_for_update())
        if wallet is None:
            raise WalletMissingError("wallet not found")
        if status == wallet.status:
            return wallet
        if status not in self._allowed_transitions.get(wallet.status, set()):
            raise InvalidWalletTransitionError(f"invalid wallet transition: {wallet.status} -> {status}")
        if status == "CLOSED" and wallet.balance != 0:
            raise WalletNotEmptyError("wallet balance must be zero before closing")

        wallet.status = status
        wallet.updated_at = utc_now()
        write_audit_event(
            self.session,
            aggregate_type=AuditAggregateType.WALLET,
            aggregate_id=wallet.id,
            event_type=f"WALLET_{status}",
            actor_id=actor_id,
            actor_type=ActorType.USER,
        )
        self.session.commit()
        self.session.refresh(wallet)
        return wallet
