from decimal import Decimal
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..infrastructure.models import (
    LedgerEntryModel,
    LedgerTransactionModel,
    TransferModel,
    WalletModel,
)


class TransferServiceError(Exception):
    pass


class IdempotencyConflictError(TransferServiceError):
    pass


class InsufficientFundsError(TransferServiceError):
    pass


class ValidationError(TransferServiceError):
    pass


class WalletNotFoundError(TransferServiceError):
    pass


class TransferNotFoundError(TransferServiceError):
    pass


class TransferNotApprovedError(TransferServiceError):
    pass


class TransferService:
    def __init__(self, session: Session):
        self.session = session

    def initiate_transfer(
        self,
        *,
        idempotency_key: str,
        source_wallet_id: str,
        destination_wallet_id: str,
        amount: Decimal,
        currency: str,
        requested_by: str,
    ) -> TransferModel:
        existing = self.session.scalar(
            select(TransferModel).where(TransferModel.idempotency_key == idempotency_key)
        )
        if existing is not None:
            self._ensure_same_request(
                existing,
                source_wallet_id=source_wallet_id,
                destination_wallet_id=destination_wallet_id,
                amount=amount,
                currency=currency,
                requested_by=requested_by,
            )
            return existing

        source = self.session.get(WalletModel, source_wallet_id)
        destination = self.session.get(WalletModel, destination_wallet_id)
        if source is None or destination is None:
            raise WalletNotFoundError("source or destination wallet not found")
        if source.id == destination.id:
            raise ValidationError("source and destination wallets must differ")
        if source.status != "ACTIVE" or destination.status != "ACTIVE":
            raise ValidationError("both wallets must be active")
        if source.currency != currency or destination.currency != currency:
            raise ValidationError("wallet currencies must match transfer currency")
        if source.balance < amount:
            raise InsufficientFundsError("insufficient funds")

        transfer = TransferModel(
            idempotency_key=idempotency_key,
            source_wallet_id=source.id,
            destination_wallet_id=destination.id,
            amount=amount,
            currency=currency,
            requested_by=requested_by,
        )
        self.session.add(transfer)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            existing = self.session.scalar(
                select(TransferModel).where(TransferModel.idempotency_key == idempotency_key)
            )
            if existing is None:
                raise
            self._ensure_same_request(
                existing,
                source_wallet_id=source_wallet_id,
                destination_wallet_id=destination_wallet_id,
                amount=amount,
                currency=currency,
                requested_by=requested_by,
            )
            return existing
        self.session.refresh(transfer)
        return transfer

    def settle_transfer(self, transfer_id: str) -> TransferModel:
        with self.session.begin():
            transfer = self.session.scalar(
                select(TransferModel)
                .where(TransferModel.id == transfer_id)
                .with_for_update()
            )
            if transfer is None:
                raise TransferNotFoundError("transfer not found")
            if transfer.status == "COMPLETED":
                return transfer
            if transfer.status != "APPROVED":
                raise TransferNotApprovedError("transfer must be approved before settlement")

            wallet_ids = sorted([transfer.source_wallet_id, transfer.destination_wallet_id])
            wallets = self.session.scalars(
                select(WalletModel)
                .where(WalletModel.id.in_(wallet_ids))
                .order_by(WalletModel.id)
                .with_for_update()
            ).all()
            wallets_by_id = {wallet.id: wallet for wallet in wallets}
            source = wallets_by_id.get(transfer.source_wallet_id)
            destination = wallets_by_id.get(transfer.destination_wallet_id)
            if source is None or destination is None:
                raise WalletNotFoundError("source or destination wallet not found")
            if source.status != "ACTIVE" or destination.status != "ACTIVE":
                raise ValidationError("both wallets must be active")
            if source.currency != transfer.currency or destination.currency != transfer.currency:
                raise ValidationError("wallet currencies must match transfer currency")
            if source.balance < transfer.amount:
                raise InsufficientFundsError("insufficient funds")

            source.balance -= transfer.amount
            destination.balance += transfer.amount
            now = datetime.now(timezone.utc)
            source.updated_at = now
            destination.updated_at = now

            ledger_transaction = LedgerTransactionModel(
                transfer_id=transfer.id,
                currency=transfer.currency,
                posted_at=now,
                created_at=now,
            )
            self.session.add(ledger_transaction)
            self.session.flush()
            self.session.add_all(
                [
                    LedgerEntryModel(
                        ledger_transaction_id=ledger_transaction.id,
                        wallet_id=source.id,
                        direction="DEBIT",
                        amount=transfer.amount,
                        currency=transfer.currency,
                        created_at=now,
                    ),
                    LedgerEntryModel(
                        ledger_transaction_id=ledger_transaction.id,
                        wallet_id=destination.id,
                        direction="CREDIT",
                        amount=transfer.amount,
                        currency=transfer.currency,
                        created_at=now,
                    ),
                ]
            )
            transfer.status = "COMPLETED"
            transfer.completed_at = now
            transfer.updated_at = now
            self.session.flush()
            return transfer

    @staticmethod
    def _ensure_same_request(
        transfer: TransferModel,
        *,
        source_wallet_id: str,
        destination_wallet_id: str,
        amount: Decimal,
        currency: str,
        requested_by: str,
    ) -> None:
        same_request = (
            transfer.source_wallet_id == source_wallet_id
            and transfer.destination_wallet_id == destination_wallet_id
            and transfer.amount == amount
            and transfer.currency == currency
            and transfer.requested_by == requested_by
        )
        if not same_request:
            raise IdempotencyConflictError("idempotency key was used with different transfer data")