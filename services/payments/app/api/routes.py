from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..infrastructure.database import get_session
from ..infrastructure.models import TransferModel, UserModel, WalletModel
from ..infrastructure.security import create_access_token
from ..services.auth_service import AuthService, EmailAlreadyRegisteredError, InvalidCredentialsError
from ..services.transfer_service import (
    IdempotencyConflictError,
    InsufficientFundsError,
    SelfAuthorizationError,
    TransferNotApprovedError,
    TransferNotFoundError,
    TransferNotPendingAuthorizationError,
    TransferNotPendingComplianceError,
    TransferService,
    ValidationError,
    WalletNotFoundError,
)
from .deps import get_current_user, require_roles
from .schemas import (
    AuthorizationCreate,
    ComplianceReviewCreate,
    Token,
    TransferCreate,
    TransferResponse,
    UserRegister,
    UserResponse,
    WalletCreate,
    WalletResponse,
)


router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/auth/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(payload: UserRegister, session: Session = Depends(get_session)) -> UserModel:
    try:
        return AuthService(session).register(email=payload.email, password=payload.password, role=payload.role)
    except EmailAlreadyRegisteredError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/auth/login", response_model=Token)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: Session = Depends(get_session),
) -> dict:
    try:
        user = AuthService(session).authenticate(email=form_data.username, password=form_data.password)
    except InvalidCredentialsError as error:
        raise HTTPException(
            status_code=401, detail=str(error), headers={"WWW-Authenticate": "Bearer"}
        ) from error
    return {"access_token": create_access_token(user.id, user.role)}


@router.post("/wallets", response_model=WalletResponse, status_code=status.HTTP_201_CREATED)
def create_wallet(
    payload: WalletCreate,
    session: Session = Depends(get_session),
    current_user: UserModel = Depends(get_current_user),
) -> WalletModel:
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


@router.get("/wallets", response_model=list[WalletResponse])
def list_wallets(session: Session = Depends(get_session)) -> list[WalletModel]:
    return list(session.scalars(select(WalletModel).order_by(WalletModel.created_at.desc())))


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
    current_user: UserModel = Depends(get_current_user),
) -> TransferModel:
    try:
        return TransferService(session).initiate_transfer(
            idempotency_key=idempotency_key,
            source_wallet_id=str(payload.source_wallet_id),
            destination_wallet_id=str(payload.destination_wallet_id),
            amount=payload.amount,
            currency=payload.currency,
            requested_by=current_user.id,
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


@router.get("/transfers", response_model=list[TransferResponse])
def list_transfers(session: Session = Depends(get_session)) -> list[TransferModel]:
    return list(session.scalars(select(TransferModel).order_by(TransferModel.created_at.desc())))


@router.get("/transfers/{transfer_id}", response_model=TransferResponse)
def get_transfer(transfer_id: UUID, session: Session = Depends(get_session)) -> TransferModel:
    transfer = session.get(TransferModel, str(transfer_id))
    if transfer is None:
        raise HTTPException(status_code=404, detail="transfer not found")
    return transfer


@router.post("/transfers/{transfer_id}/compliance-review", response_model=TransferResponse)
def review_compliance(
    transfer_id: UUID,
    payload: ComplianceReviewCreate,
    session: Session = Depends(get_session),
    current_user: UserModel = Depends(require_roles("compliance_officer", "admin")),
) -> TransferModel:
    try:
        return TransferService(session).review_compliance(
            str(transfer_id),
            decision=payload.decision,
            reviewer_id=current_user.id,
            reason_code=payload.reason_code,
            reason=payload.reason,
        )
    except TransferNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except TransferNotPendingComplianceError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/transfers/{transfer_id}/authorize", response_model=TransferResponse)
def authorize_transfer(
    transfer_id: UUID,
    payload: AuthorizationCreate,
    session: Session = Depends(get_session),
    current_user: UserModel = Depends(require_roles("authorizer", "admin")),
) -> TransferModel:
    try:
        return TransferService(session).authorize_transfer(
            str(transfer_id),
            authorizer_id=current_user.id,
            decision=payload.decision,
            policy_version=payload.policy_version,
            reason=payload.reason,
        )
    except TransferNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except TransferNotPendingAuthorizationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except SelfAuthorizationError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/transfers/{transfer_id}/settle", response_model=TransferResponse)
def settle_transfer(
    transfer_id: UUID,
    session: Session = Depends(get_session),
    current_user: UserModel = Depends(get_current_user),
) -> TransferModel:
    try:
        return TransferService(session).settle_transfer(str(transfer_id))
    except TransferNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except TransferNotApprovedError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except InsufficientFundsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (ValidationError, WalletNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error