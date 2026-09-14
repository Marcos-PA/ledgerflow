from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain import ActorType, AuditAggregateType
from ..infrastructure.models import LedgerEntryModel, LedgerTransactionModel, WalletModel
from .audit_service import write_audit_event


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LedgerServiceError(Exception):
    pass


class UnbalancedJournalEntryError(LedgerServiceError):
    pass


class JournalWalletNotFoundError(LedgerServiceError):
    pass


class JournalInsufficientFundsError(LedgerServiceError):
    pass


class JournalValidationError(LedgerServiceError):
    pass


@dataclass(frozen=True)
class JournalEntryLine:
    wallet_id: str
    direction: str
    amount: Decimal


class LedgerService:
    def __init__(self, session: Session):
        self.session = session

    def record_manual_journal_entry(
        self,
        *,
        currency: str,
        lines: list[JournalEntryLine],
        memo: str | None,
        entered_by: str,
    ) -> LedgerTransactionModel:
        if len(lines) < 2:
            raise UnbalancedJournalEntryError("a journal entry needs at least two lines")
        debits = sum((line.amount for line in lines if line.direction == "DEBIT"), Decimal("0"))
        credits = sum((line.amount for line in lines if line.direction == "CREDIT"), Decimal("0"))
        if debits != credits:
            raise UnbalancedJournalEntryError("debits and credits must balance")

        wallet_ids = sorted({line.wallet_id for line in lines})
        wallets = self.session.scalars(
            select(WalletModel).where(WalletModel.id.in_(wallet_ids)).order_by(WalletModel.id).with_for_update()
        ).all()
        wallets_by_id = {wallet.id: wallet for wallet in wallets}
        missing = set(wallet_ids) - wallets_by_id.keys()
        if missing:
            raise JournalWalletNotFoundError(f"wallet(s) not found: {', '.join(sorted(missing))}")
        for wallet in wallets:
            if wallet.status != "ACTIVE":
                raise JournalValidationError(f"wallet {wallet.id} is not active")
            if wallet.currency != currency:
                raise JournalValidationError(f"wallet {wallet.id} currency does not match journal entry")

        for line in lines:
            if line.direction == "DEBIT" and wallets_by_id[line.wallet_id].balance < line.amount:
                raise JournalInsufficientFundsError(f"wallet {line.wallet_id} has insufficient funds")

        now = utc_now()
        ledger_transaction = LedgerTransactionModel(
            transfer_id=None,
            currency=currency,
            memo=memo,
            entered_by=entered_by,
            posted_at=now,
            created_at=now,
        )
        self.session.add(ledger_transaction)
        self.session.flush()

        for line in lines:
            wallet = wallets_by_id[line.wallet_id]
            if line.direction == "DEBIT":
                wallet.balance -= line.amount
            else:
                wallet.balance += line.amount
            wallet.updated_at = now
            self.session.add(
                LedgerEntryModel(
                    ledger_transaction_id=ledger_transaction.id,
                    wallet_id=line.wallet_id,
                    direction=line.direction,
                    amount=line.amount,
                    currency=currency,
                    created_at=now,
                )
            )

        write_audit_event(
            self.session,
            aggregate_type=AuditAggregateType.LEDGER_TRANSACTION,
            aggregate_id=ledger_transaction.id,
            event_type="MANUAL_JOURNAL_ENTRY_RECORDED",
            actor_id=entered_by,
            actor_type=ActorType.USER,
            reason=memo,
        )
        self.session.commit()
        self.session.refresh(ledger_transaction)
        return ledger_transaction
