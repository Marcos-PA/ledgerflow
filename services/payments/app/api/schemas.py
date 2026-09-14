from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from ..services.auth_service import ROLES


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    role: str = Field(pattern="^(" + "|".join(ROLES) + ")$")


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    role: str
    created_at: datetime


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class WalletCreate(BaseModel):
    owner_id: UUID
    currency: str = Field(min_length=3, max_length=3)
    initial_balance: Decimal = Field(default=Decimal("0"), ge=0)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class WalletResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_id: UUID
    currency: str
    balance: Decimal
    status: str
    created_at: datetime
    updated_at: datetime


class WalletStatusUpdate(BaseModel):
    status: str = Field(pattern="^(ACTIVE|FROZEN|CLOSED)$")


class TransferCreate(BaseModel):
    source_wallet_id: UUID
    destination_wallet_id: UUID
    amount: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class ComplianceReviewCreate(BaseModel):
    decision: str = Field(pattern="^(APPROVED|REJECTED|FLAGGED)$")
    reason_code: str | None = None
    reason: str | None = None


class AuthorizationCreate(BaseModel):
    decision: str = Field(pattern="^(APPROVED|REJECTED)$")
    policy_version: str = Field(min_length=1)
    reason: str | None = None


class TransferResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    idempotency_key: str
    source_wallet_id: UUID
    destination_wallet_id: UUID
    amount: Decimal
    currency: str
    status: str
    requested_by: UUID
    requested_at: datetime
    approved_at: datetime | None
    completed_at: datetime | None
    rejected_at: datetime | None
    failure_code: str | None
    failure_reason: str | None
    reversal_of_transfer_id: UUID | None


class LedgerEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ledger_transaction_id: UUID
    wallet_id: UUID
    direction: str
    amount: Decimal
    currency: str
    created_at: datetime


class AuditEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    aggregate_type: str
    aggregate_id: UUID
    event_type: str
    actor_id: UUID | None
    actor_type: str
    reason: str | None
    metadata_json: dict
    occurred_at: datetime
    previous_event_hash: str | None
    event_hash: str


class ComplianceReviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    transfer_id: UUID
    reviewer_id: UUID | None
    decision: str
    reason_code: str | None
    reason: str | None
    reviewed_at: datetime | None
    created_at: datetime


class AuthorizationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    transfer_id: UUID
    authorizer_id: UUID
    decision: str
    policy_version: str
    reason: str | None
    authorized_at: datetime


class JournalEntryLineCreate(BaseModel):
    wallet_id: UUID
    direction: str = Field(pattern="^(DEBIT|CREDIT)$")
    amount: Decimal = Field(gt=0)


class JournalEntryCreate(BaseModel):
    currency: str = Field(min_length=3, max_length=3)
    memo: str | None = None
    entries: list[JournalEntryLineCreate] = Field(min_length=2)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class LedgerTransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    transfer_id: UUID | None
    currency: str
    memo: str | None
    entered_by: UUID | None
    posted_at: datetime
    entries: list[LedgerEntryResponse]