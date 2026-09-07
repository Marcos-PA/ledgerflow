from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..infrastructure.database import get_session
from ..infrastructure.models import TransferModel, WalletModel
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
    existing = session.scalar(select(TransferModel).where(TransferModel.idempotency_key == idempotency_key))
    if existing is not None:
        return existing

    source = session.get(WalletModel, str(payload.source_wallet_id))
    destination = session.get(WalletModel, str(payload.destination_wallet_id))
    if source is None or destination is None:
        raise HTTPException(status_code=404, detail="source or destination wallet not found")
    if source.id == destination.id:
        raise HTTPException(status_code=400, detail="source and destination wallets must differ")
    if source.status != "ACTIVE" or destination.status != "ACTIVE":
        raise HTTPException(status_code=409, detail="both wallets must be active")
    if source.currency != payload.currency or destination.currency != payload.currency:
        raise HTTPException(status_code=400, detail="wallet currencies must match transfer currency")
    if source.balance < payload.amount:
        raise HTTPException(status_code=409, detail="insufficient funds")

    transfer = TransferModel(
        idempotency_key=idempotency_key,
        source_wallet_id=source.id,
        destination_wallet_id=destination.id,
        amount=payload.amount,
        currency=payload.currency,
        requested_by=str(payload.requested_by),
    )
    session.add(transfer)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        existing = session.scalar(select(TransferModel).where(TransferModel.idempotency_key == idempotency_key))
        if existing is not None:
            return existing
        raise error
    session.refresh(transfer)
    return transfer


@router.get("/transfers/{transfer_id}", response_model=TransferResponse)
def get_transfer(transfer_id: UUID, session: Session = Depends(get_session)) -> TransferModel:
    transfer = session.get(TransferModel, str(transfer_id))
    if transfer is None:
        raise HTTPException(status_code=404, detail="transfer not found")
    return transfer