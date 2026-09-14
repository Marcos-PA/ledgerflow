from decimal import Decimal
from datetime import datetime, timezone
import os
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..domain import ActorType, AuditAggregateType
from ..infrastructure.models import (
    AuthorizationModel,
    ComplianceReviewModel,
    LedgerEntryModel,
    LedgerTransactionModel,
    TransferModel,
    WalletModel,
)
from .audit_service import write_audit_event


# ponytail: single process-wide N-of-M threshold via env var instead of a
# per-wallet/per-amount policy table — upgrade to a real policy model if
# different transfer classes ever need different thresholds.
AUTHORIZATION_THRESHOLD = int(os.getenv("AUTHORIZATION_THRESHOLD", "1"))


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


class TransferNotCompletedError(TransferServiceError):
    pass


class TransferAlreadyReversedError(TransferServiceError):
    pass


class DuplicateAuthorizationError(TransferServiceError):
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
        reversal_of_transfer_id: str | None = None,
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
            reversal_of_transfer_id=reversal_of_transfer_id,
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

    def reverse_transfer(self, transfer_id: str, *, requested_by: str) -> TransferModel:
        original = self.session.scalar(select(TransferModel).where(TransferModel.id == transfer_id))
        if original is None:
            raise TransferNotFoundError("transfer not found")
        if original.status != "COMPLETED":
            raise TransferNotCompletedError("only completed transfers can be reversed")
        already_reversed = self.session.scalar(
            select(TransferModel).where(TransferModel.reversal_of_transfer_id == original.id)
        )
        if already_reversed is not None:
            raise TransferAlreadyReversedError("transfer has already been reversed")

        return self.initiate_transfer(
            idempotency_key=f"reversal:{original.id}",
            source_wallet_id=original.destination_wallet_id,
            destination_wallet_id=original.source_wallet_id,
            amount=original.amount,
            currency=original.currency,
            requested_by=requested_by,
            reversal_of_transfer_id=original.id,
        )

    def settle_transfer(self, transfer_id: str) -> TransferModel:
        settlement_error: TransferServiceError | None = None

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
        self.session.commit()

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
        self.session.commit()
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

        transfer = self.session.scalar(
            select(TransferModel).where(TransferModel.id == transfer_id).with_for_update()
        )
        if transfer is None:
            raise TransferNotFoundError("transfer not found")
        if transfer.status != "PENDING_AUTHORIZATION":
            raise TransferNotPendingAuthorizationError("transfer is not pending authorization")
        if authorizer_id == transfer.requested_by:
            raise SelfAuthorizationError("an authorizer cannot authorize their own transfer")
        already_authorized = self.session.scalar(
            select(AuthorizationModel).where(
                AuthorizationModel.transfer_id == transfer.id,
                AuthorizationModel.authorizer_id == authorizer_id,
            )
        )
        if already_authorized is not None:
            raise DuplicateAuthorizationError("authorizer has already submitted a decision for this transfer")

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

        approvals = 0
        if decision == "APPROVED":
            approvals = (
                self.session.scalar(
                    select(func.count())
                    .select_from(AuthorizationModel)
                    .where(
                        AuthorizationModel.transfer_id == transfer.id,
                        AuthorizationModel.decision == "APPROVED",
                    )
                )
                or 0
            ) + 1  # the row above is not flushed yet
            if approvals >= AUTHORIZATION_THRESHOLD:
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
            metadata={"policy_version": policy_version, "approvals": approvals, "threshold": AUTHORIZATION_THRESHOLD},
        )
        self.session.commit()
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
        write_audit_event(
            self.session,
            aggregate_type=AuditAggregateType.TRANSFER,
            aggregate_id=transfer_id,
            event_type=event_type,
            actor_id=actor_id,
            actor_type=actor_type,
            reason=reason,
            metadata=metadata,
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