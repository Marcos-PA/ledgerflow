from decimal import Decimal
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..domain import ActorType, AuditAggregateType, AuditEvent
from ..infrastructure.models import (
    AuditEventModel,
    AuthorizationModel,
    ComplianceReviewModel,
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


class TransferNotPendingComplianceError(TransferServiceError):
    pass


class TransferNotPendingAuthorizationError(TransferServiceError):
    pass


class SelfAuthorizationError(TransferServiceError):
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
            self.session.flush()
            self._write_audit_event(
                transfer_id=transfer.id,
                event_type="TRANSFER_REQUESTED",
                actor_id=requested_by,
                actor_type=ActorType.USER,
            )
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
        settlement_error: TransferServiceError | None = None

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

            failure_code: str | None = None
            failure_reason: str | None = None
            if source is None or destination is None:
                settlement_error = WalletNotFoundError("source or destination wallet not found")
                failure_code, failure_reason = "WALLET_NOT_FOUND", str(settlement_error)
            elif source.status != "ACTIVE" or destination.status != "ACTIVE":
                settlement_error = ValidationError("both wallets must be active")
                failure_code, failure_reason = "WALLET_NOT_ACTIVE", str(settlement_error)
            elif source.currency != transfer.currency or destination.currency != transfer.currency:
                settlement_error = ValidationError("wallet currencies must match transfer currency")
                failure_code, failure_reason = "CURRENCY_MISMATCH", str(settlement_error)
            elif source.balance < transfer.amount:
                settlement_error = InsufficientFundsError("insufficient funds")
                failure_code, failure_reason = "INSUFFICIENT_FUNDS", str(settlement_error)

            now = datetime.now(timezone.utc)
            if settlement_error is not None:
                transfer.status = "FAILED"
                transfer.failure_code = failure_code
                transfer.failure_reason = failure_reason
                transfer.updated_at = now
                self._write_audit_event(
                    transfer_id=transfer.id,
                    event_type="SETTLEMENT_FAILED",
                    actor_id=None,
                    actor_type=ActorType.SYSTEM,
                    reason=failure_reason,
                    metadata={"failure_code": failure_code},
                )
            else:
                source.balance -= transfer.amount
                destination.balance += transfer.amount
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
                self._write_audit_event(
                    transfer_id=transfer.id,
                    event_type="SETTLEMENT_COMPLETED",
                    actor_id=None,
                    actor_type=ActorType.SYSTEM,
                )
            self.session.flush()

        if settlement_error is not None:
            raise settlement_error
        return transfer

    def review_compliance(
        self,
        transfer_id: str,
        *,
        decision: str,
        reviewer_id: str | None = None,
        reason_code: str | None = None,
        reason: str | None = None,
    ) -> TransferModel:
        if decision not in ("APPROVED", "REJECTED", "FLAGGED"):
            raise ValidationError("decision must be APPROVED, REJECTED, or FLAGGED")

        with self.session.begin():
            transfer = self.session.scalar(
                select(TransferModel).where(TransferModel.id == transfer_id).with_for_update()
            )
            if transfer is None:
                raise TransferNotFoundError("transfer not found")
            if transfer.status != "PENDING_COMPLIANCE":
                raise TransferNotPendingComplianceError("transfer is not pending compliance review")

            now = datetime.now(timezone.utc)
            self.session.add(
                ComplianceReviewModel(
                    transfer_id=transfer.id,
                    reviewer_id=reviewer_id,
                    decision=decision,
                    reason_code=reason_code,
                    reason=reason,
                    reviewed_at=now,
                )
            )

            if decision == "APPROVED":
                transfer.status = "PENDING_AUTHORIZATION"
            elif decision == "REJECTED":
                transfer.status = "REJECTED"
                transfer.rejected_at = now
                transfer.failure_code = reason_code
                transfer.failure_reason = reason
            transfer.updated_at = now
            self._write_audit_event(
                transfer_id=transfer.id,
                event_type=f"COMPLIANCE_{decision}",
                actor_id=reviewer_id,
                actor_type=ActorType.USER if reviewer_id else ActorType.SYSTEM,
                reason=reason,
            )
            self.session.flush()
            return transfer

    def authorize_transfer(
        self,
        transfer_id: str,
        *,
        authorizer_id: str,
        decision: str,
        policy_version: str,
        reason: str | None = None,
    ) -> TransferModel:
        if decision not in ("APPROVED", "REJECTED"):
            raise ValidationError("decision must be APPROVED or REJECTED")

        with self.session.begin():
            transfer = self.session.scalar(
                select(TransferModel).where(TransferModel.id == transfer_id).with_for_update()
            )
            if transfer is None:
                raise TransferNotFoundError("transfer not found")
            if transfer.status != "PENDING_AUTHORIZATION":
                raise TransferNotPendingAuthorizationError("transfer is not pending authorization")
            if authorizer_id == transfer.requested_by:
                raise SelfAuthorizationError("an authorizer cannot authorize their own transfer")

            now = datetime.now(timezone.utc)
            self.session.add(
                AuthorizationModel(
                    transfer_id=transfer.id,
                    authorizer_id=authorizer_id,
                    decision=decision,
                    policy_version=policy_version,
                    reason=reason,
                    authorized_at=now,
                )
            )

            if decision == "APPROVED":
                transfer.status = "APPROVED"
                transfer.approved_at = now
            else:
                transfer.status = "REJECTED"
                transfer.rejected_at = now
                transfer.failure_reason = reason
            transfer.updated_at = now
            self._write_audit_event(
                transfer_id=transfer.id,
                event_type=f"AUTHORIZATION_{decision}",
                actor_id=authorizer_id,
                actor_type=ActorType.USER,
                reason=reason,
                metadata={"policy_version": policy_version},
            )
            self.session.flush()
            return transfer

    def _write_audit_event(
        self,
        *,
        transfer_id: str,
        event_type: str,
        actor_id: str | None,
        actor_type: ActorType,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        previous = self.session.scalar(
            select(AuditEventModel)
            .where(
                AuditEventModel.aggregate_type == AuditAggregateType.TRANSFER.value,
                AuditEventModel.aggregate_id == transfer_id,
            )
            .order_by(AuditEventModel.occurred_at.desc())
            .limit(1)
        )
        event = AuditEvent(
            aggregate_type=AuditAggregateType.TRANSFER,
            aggregate_id=UUID(transfer_id),
            event_type=event_type,
            actor_type=actor_type,
            actor_id=UUID(actor_id) if actor_id else None,
            reason=reason,
            metadata=metadata or {},
            previous_event_hash=previous.event_hash if previous else None,
        )
        self.session.add(
            AuditEventModel(
                id=str(event.id),
                aggregate_type=event.aggregate_type.value,
                aggregate_id=str(event.aggregate_id),
                event_type=event.event_type,
                actor_id=str(event.actor_id) if event.actor_id else None,
                actor_type=event.actor_type.value,
                reason=event.reason,
                metadata_json=event.metadata,
                occurred_at=event.occurred_at,
                previous_event_hash=event.previous_event_hash,
                event_hash=event.event_hash,
            )
        )

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