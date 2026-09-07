from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..infrastructure.models import TransferModel, WalletModel


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