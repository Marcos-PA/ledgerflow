from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..infrastructure.database import get_session
from ..infrastructure.models import TransferModel, WalletModel
from ..services.transfer_service import (
    IdempotencyConflictError,
    InsufficientFundsError,
    TransferService,
    ValidationError,
    WalletNotFoundError,
)
from .schemas import TransferCreate, TransferResponse, WalletCreate, WalletResponse


router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/wallets", response_model=WalletResponse, status_code=status.HTTP_201_CREATED)
def create_wallet(payload: WalletCreate, session: Session = Depends(get_session)) -> WalletModel:
    existing = session.scalar(
        select(WalletModel).where(
            WalletModel.owner_id == str(payload.owner_id),
            WalletModel.currency == payload.currency,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="wallet already exists for owner and currency")

    wallet = WalletModel(
        owner_id=str(payload.owner_id),
        currency=payload.currency,
        balance=payload.initial_balance,
    )
    session.add(wallet)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        if "uq_wallet_owner_currency" in str(error.orig):
            raise HTTPException(status_code=409, detail="wallet already exists for owner and currency") from error
        raise
    session.refresh(wallet)
    return wallet


@router.get("/wallets/{wallet_id}", response_model=WalletResponse)
def get_wallet(wallet_id: UUID, session: Session = Depends(get_session)) -> WalletModel:
    wallet = session.get(WalletModel, str(wallet_id))
    if wallet is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    return wallet


@router.post("/transfers", response_model=TransferResponse, status_code=status.HTTP_201_CREATED)
def create_transfer(
    payload: TransferCreate,
    idempotency_key: str = Header(min_length=1),
    session: Session = Depends(get_session),
) -> TransferModel:
    try:
        return TransferService(session).initiate_transfer(
            idempotency_key=idempotency_key,
            source_wallet_id=str(payload.source_wallet_id),
            destination_wallet_id=str(payload.destination_wallet_id),
            amount=payload.amount,
            currency=payload.currency,
            requested_by=str(payload.requested_by),
        )
    except WalletNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except IdempotencyConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except InsufficientFundsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValidationError as error:
        status_code = 409 if "active" in str(error) else 400
        raise HTTPException(status_code=status_code, detail=str(error)) from error


@router.get("/transfers/{transfer_id}", response_model=TransferResponse)
def get_transfer(transfer_id: UUID, session: Session = Depends(get_session)) -> TransferModel:
    transfer = session.get(TransferModel, str(transfer_id))
    if transfer is None:
        raise HTTPException(status_code=404, detail="transfer not found")
    return transfer