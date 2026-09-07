from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any
from uuid import UUID, uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WalletStatus(StrEnum):
    ACTIVE = "ACTIVE"
    FROZEN = "FROZEN"
    CLOSED = "CLOSED"


class TransferStatus(StrEnum):
    PENDING_COMPLIANCE = "PENDING_COMPLIANCE"
    PENDING_AUTHORIZATION = "PENDING_AUTHORIZATION"
    APPROVED = "APPROVED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class ComplianceDecision(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FLAGGED = "FLAGGED"


class AuthorizationDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class LedgerDirection(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class ActorType(StrEnum):
    USER = "USER"
    SERVICE = "SERVICE"
    SYSTEM = "SYSTEM"


class AuditAggregateType(StrEnum):
    WALLET = "WALLET"
    TRANSFER = "TRANSFER"
    LEDGER_TRANSACTION = "LEDGER_TRANSACTION"


@dataclass
class Wallet:
    owner_id: UUID
    currency: str
    balance: Decimal = Decimal("0")
    status: WalletStatus = WalletStatus.ACTIVE
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.currency = self.currency.upper()
        self._validate_balance(self.balance)
        if not self.currency or len(self.currency) != 3:
            raise ValueError("currency must be a three-letter ISO 4217 code")

    def debit(self, amount: Decimal) -> None:
        self._validate_amount(amount)
        if self.status is not WalletStatus.ACTIVE:
            raise ValueError("only active wallets can be debited")
        if self.balance < amount:
            raise ValueError("insufficient funds")
        self.balance -= amount
        self.updated_at = utc_now()

    def credit(self, amount: Decimal) -> None:
        self._validate_amount(amount)
        if self.status is not WalletStatus.ACTIVE:
            raise ValueError("only active wallets can be credited")
        self.balance += amount
        self.updated_at = utc_now()

    @staticmethod
    def _validate_amount(amount: Decimal) -> None:
        if not isinstance(amount, Decimal) or amount <= 0:
            raise ValueError("amount must be a positive Decimal")

    @staticmethod
    def _validate_balance(balance: Decimal) -> None:
        if not isinstance(balance, Decimal) or balance < 0:
            raise ValueError("balance must be a non-negative Decimal")


@dataclass
class Transfer:
    idempotency_key: str
    source_wallet_id: UUID
    destination_wallet_id: UUID
    amount: Decimal
    currency: str
    requested_by: UUID
    status: TransferStatus = TransferStatus.PENDING_COMPLIANCE
    failure_code: str | None = None
    failure_reason: str | None = None
    requested_at: datetime = field(default_factory=utc_now)
    approved_at: datetime | None = None
    completed_at: datetime | None = None
    rejected_at: datetime | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    _allowed_transitions = {
        TransferStatus.PENDING_COMPLIANCE: {
            TransferStatus.REJECTED,
            TransferStatus.PENDING_AUTHORIZATION,
        },
        TransferStatus.PENDING_AUTHORIZATION: {
            TransferStatus.REJECTED,
            TransferStatus.APPROVED,
        },
        TransferStatus.APPROVED: {
            TransferStatus.COMPLETED,
            TransferStatus.FAILED,
        },
    }

    def __post_init__(self) -> None:
        self.currency = self.currency.upper()
        if not self.idempotency_key:
            raise ValueError("idempotency_key is required")
        if self.source_wallet_id == self.destination_wallet_id:
            raise ValueError("source and destination wallets must differ")
        if not isinstance(self.amount, Decimal) or self.amount <= 0:
            raise ValueError("amount must be a positive Decimal")
        if not self.currency or len(self.currency) != 3:
            raise ValueError("currency must be a three-letter ISO 4217 code")

    def transition_to(
        self,
        status: TransferStatus,
        *,
        failure_code: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        if status not in self._allowed_transitions.get(self.status, set()):
            raise ValueError(f"invalid transfer transition: {self.status} -> {status}")
        now = utc_now()
        self.status = status
        self.updated_at = now
        if status is TransferStatus.APPROVED:
            self.approved_at = now
        elif status is TransferStatus.COMPLETED:
            self.completed_at = now
        elif status is TransferStatus.REJECTED:
            self.rejected_at = now
        elif status is TransferStatus.FAILED:
            self.failure_code = failure_code
            self.failure_reason = failure_reason


@dataclass(frozen=True)
class LedgerEntry:
    ledger_transaction_id: UUID
    wallet_id: UUID
    direction: LedgerDirection
    amount: Decimal
    currency: str
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal) or self.amount <= 0:
            raise ValueError("ledger entry amount must be a positive Decimal")
        if not self.currency or len(self.currency) != 3:
            raise ValueError("currency must be a three-letter ISO 4217 code")
        object.__setattr__(self, "currency", self.currency.upper())


@dataclass
class LedgerTransaction:
    transfer_id: UUID
    currency: str
    posted_at: datetime = field(default_factory=utc_now)
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)
    entries: tuple[LedgerEntry, ...] = ()

    def __post_init__(self) -> None:
        self.currency = self.currency.upper()
        if not self.currency or len(self.currency) != 3:
            raise ValueError("currency must be a three-letter ISO 4217 code")
        self.validate()

    def add_entry(self, entry: LedgerEntry) -> None:
        if entry.ledger_transaction_id != self.id:
            raise ValueError("entry references a different ledger transaction")
        if entry.currency != self.currency:
            raise ValueError("all ledger entries must use the transaction currency")
        candidate_entries = (*self.entries, entry)
        if len(candidate_entries) >= 2:
            self._validate_entries(candidate_entries)
        self.entries = candidate_entries

    def validate(self) -> None:
        if not self.entries:
            return
        self._validate_entries(self.entries)

    @staticmethod
    def _validate_entries(entries: tuple[LedgerEntry, ...]) -> None:
        if len(entries) < 2:
            raise ValueError("ledger transaction must have at least two entries")
        debits = sum(
            (entry.amount for entry in entries if entry.direction is LedgerDirection.DEBIT),
            Decimal("0"),
        )
        credits = sum(
            (entry.amount for entry in entries if entry.direction is LedgerDirection.CREDIT),
            Decimal("0"),
        )
        if debits != credits:
            raise ValueError("ledger transaction must be balanced")


@dataclass(frozen=True)
class ComplianceReview:
    transfer_id: UUID
    decision: ComplianceDecision
    reviewer_id: UUID | None = None
    reason_code: str | None = None
    reason: str | None = None
    reviewed_at: datetime | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class Authorization:
    transfer_id: UUID
    authorizer_id: UUID
    decision: AuthorizationDecision
    policy_version: str
    requested_by: UUID
    reason: str | None = None
    authorized_at: datetime = field(default_factory=utc_now)
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if self.authorizer_id == self.requested_by:
            raise ValueError("an authorizer cannot authorize their own transfer")
        if not self.policy_version:
            raise ValueError("policy_version is required")


@dataclass(frozen=True)
class AuditEvent:
    aggregate_type: AuditAggregateType
    aggregate_id: UUID
    event_type: str
    actor_type: ActorType
    metadata: dict[str, Any] = field(default_factory=dict)
    actor_id: UUID | None = None
    reason: str | None = None
    occurred_at: datetime = field(default_factory=utc_now)
    previous_event_hash: str | None = None
    id: UUID = field(default_factory=uuid4)
    event_hash: str = field(init=False)

    def __post_init__(self) -> None:
        payload = {
            "aggregate_id": str(self.aggregate_id),
            "aggregate_type": self.aggregate_type.value,
            "actor_id": str(self.actor_id) if self.actor_id else None,
            "actor_type": self.actor_type.value,
            "event_type": self.event_type,
            "metadata": self.metadata,
            "occurred_at": self.occurred_at.isoformat(),
            "previous_event_hash": self.previous_event_hash,
            "reason": self.reason,
        }
        canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        object.__setattr__(self, "event_hash", sha256(canonical_payload.encode()).hexdigest())