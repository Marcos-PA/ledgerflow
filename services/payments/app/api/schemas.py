from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class TransferCreate(BaseModel):
    source_wallet_id: UUID
    destination_wallet_id: UUID
    amount: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    requested_by: UUID

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


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