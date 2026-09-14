import csv
import io
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..infrastructure.database import get_session
from ..infrastructure.models import (
    AuditEventModel,
    AuthorizationModel,
    ComplianceReviewModel,
    LedgerEntryModel,
    LedgerTransactionModel,
    TransferModel,
    UserModel,
    WalletModel,
)
from ..infrastructure.security import (
    LoginRateLimitedError,
    check_login_rate_limit,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    record_login_failure,
    reset_login_attempts,
)
from ..services.auth_service import AuthService, EmailAlreadyRegisteredError, InvalidCredentialsError
from ..services.transfer_service import (
    DuplicateAuthorizationError,
    IdempotencyConflictError,
    InsufficientFundsError,
    SelfAuthorizationError,
    TransferAlreadyReversedError,
    TransferNotApprovedError,
    TransferNotCompletedError,
    TransferNotFoundError,
    TransferNotPendingAuthorizationError,
    TransferNotPendingComplianceError,
    TransferService,
    ValidationError,
    WalletNotFoundError,
)
from ..services.ledger_service import (
    JournalEntryLine,
    JournalInsufficientFundsError,
    JournalValidationError,
    JournalWalletNotFoundError,
    LedgerService,
    UnbalancedJournalEntryError,
)
from ..services.wallet_service import (
    InvalidWalletTransitionError,
    WalletMissingError,
    WalletNotEmptyError,
    WalletService,
)
from .deps import get_current_user, require_roles
from .schemas import (
    AuditEventResponse,
    AuthorizationCreate,
    AuthorizationResponse,
    ComplianceReviewCreate,
    ComplianceReviewResponse,
    JournalEntryCreate,
    LedgerEntryResponse,
    LedgerTransactionResponse,
    RefreshTokenRequest,
    Token,
    TransferCreate,
    TransferResponse,
    UserRegister,
    UserResponse,
    WalletCreate,
    WalletResponse,
    WalletStatusUpdate,
)


router = APIRouter()


def _csv_response(fieldnames: list[str], rows: list[dict], filename: str) -> StreamingResponse:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: Session = Depends(get_session),
) -> dict:
    client_key = request.client.host if request.client else "unknown"
    try:
        check_login_rate_limit(client_key)
    except LoginRateLimitedError as error:
        raise HTTPException(status_code=429, detail=str(error)) from error

    try:
        user = AuthService(session).authenticate(email=form_data.username, password=form_data.password)
    except InvalidCredentialsError as error:
        record_login_failure(client_key)
        raise HTTPException(
            status_code=401, detail=str(error), headers={"WWW-Authenticate": "Bearer"}
        ) from error
    reset_login_attempts(client_key)
    return {"access_token": create_access_token(user.id, user.role), "refresh_token": create_refresh_token(user.id)}


@router.post("/auth/refresh", response_model=Token)
def refresh_access_token(payload: RefreshTokenRequest, session: Session = Depends(get_session)) -> dict:
    invalid_token_error = HTTPException(
        status_code=401, detail="invalid refresh token", headers={"WWW-Authenticate": "Bearer"}
    )
    try:
        claims = decode_access_token(payload.refresh_token)
    except jwt.PyJWTError:
        raise invalid_token_error from None
    if claims.get("type") != "refresh":
        raise invalid_token_error

    user = session.get(UserModel, claims.get("sub"))
    if user is None:
        raise invalid_token_error
    return {"access_token": create_access_token(user.id, user.role), "refresh_token": create_refresh_token(user.id)}


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
def list_wallets(
    status_filter: str | None = Query(default=None, alias="status"),
    currency: str | None = Query(default=None),
    owner_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[WalletModel]:
    query = select(WalletModel)
    if status_filter is not None:
        query = query.where(WalletModel.status == status_filter)
    if currency is not None:
        query = query.where(WalletModel.currency == currency.upper())
    if owner_id is not None:
        query = query.where(WalletModel.owner_id == str(owner_id))
    query = query.order_by(WalletModel.created_at.desc()).limit(limit).offset(offset)
    return list(session.scalars(query))


@router.get("/wallets/{wallet_id}", response_model=WalletResponse)
def get_wallet(wallet_id: UUID, session: Session = Depends(get_session)) -> WalletModel:
    wallet = session.get(WalletModel, str(wallet_id))
    if wallet is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    return wallet


@router.patch("/wallets/{wallet_id}/status", response_model=WalletResponse)
def update_wallet_status(
    wallet_id: UUID,
    payload: WalletStatusUpdate,
    session: Session = Depends(get_session),
    current_user: UserModel = Depends(require_roles("compliance_officer", "admin")),
) -> WalletModel:
    try:
        return WalletService(session).update_status(str(wallet_id), status=payload.status, actor_id=current_user.id)
    except WalletMissingError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (InvalidWalletTransitionError, WalletNotEmptyError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/wallets/{wallet_id}/ledger-entries", response_model=list[LedgerEntryResponse])
def list_wallet_ledger_entries(
    wallet_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[LedgerEntryModel]:
    wallet = session.get(WalletModel, str(wallet_id))
    if wallet is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    return list(
        session.scalars(
            select(LedgerEntryModel)
            .where(LedgerEntryModel.wallet_id == str(wallet_id))
            .order_by(LedgerEntryModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )


@router.get("/wallets/{wallet_id}/ledger-entries/export")
def export_wallet_ledger_entries(wallet_id: UUID, session: Session = Depends(get_session)) -> StreamingResponse:
    wallet = session.get(WalletModel, str(wallet_id))
    if wallet is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    entries = session.scalars(
        select(LedgerEntryModel)
        .where(LedgerEntryModel.wallet_id == str(wallet_id))
        .order_by(LedgerEntryModel.created_at.desc())
    )
    rows = [
        {
            "id": entry.id,
            "ledger_transaction_id": entry.ledger_transaction_id,
            "wallet_id": entry.wallet_id,
            "direction": entry.direction,
            "amount": str(entry.amount),
            "currency": entry.currency,
            "created_at": entry.created_at.isoformat(),
        }
        for entry in entries
    ]
    fieldnames = ["id", "ledger_transaction_id", "wallet_id", "direction", "amount", "currency", "created_at"]
    return _csv_response(fieldnames, rows, f"wallet-{wallet_id}-ledger-entries.csv")


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
def list_transfers(
    status_filter: str | None = Query(default=None, alias="status"),
    currency: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[TransferModel]:
    query = select(TransferModel)
    if status_filter is not None:
        query = query.where(TransferModel.status == status_filter)
    if currency is not None:
        query = query.where(TransferModel.currency == currency.upper())
    query = query.order_by(TransferModel.created_at.desc()).limit(limit).offset(offset)
    return list(session.scalars(query))


@router.get("/transfers/export")
def export_transfers(
    status_filter: str | None = Query(default=None, alias="status"),
    currency: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    query = select(TransferModel)
    if status_filter is not None:
        query = query.where(TransferModel.status == status_filter)
    if currency is not None:
        query = query.where(TransferModel.currency == currency.upper())
    transfers = session.scalars(query.order_by(TransferModel.created_at.desc()))
    fieldnames = [
        "id",
        "idempotency_key",
        "source_wallet_id",
        "destination_wallet_id",
        "amount",
        "currency",
        "status",
        "requested_by",
        "requested_at",
        "completed_at",
        "failure_code",
        "reversal_of_transfer_id",
    ]
    rows = [
        {
            "id": transfer.id,
            "idempotency_key": transfer.idempotency_key,
            "source_wallet_id": transfer.source_wallet_id,
            "destination_wallet_id": transfer.destination_wallet_id,
            "amount": str(transfer.amount),
            "currency": transfer.currency,
            "status": transfer.status,
            "requested_by": transfer.requested_by,
            "requested_at": transfer.requested_at.isoformat(),
            "completed_at": transfer.completed_at.isoformat() if transfer.completed_at else "",
            "failure_code": transfer.failure_code or "",
            "reversal_of_transfer_id": transfer.reversal_of_transfer_id or "",
        }
        for transfer in transfers
    ]
    return _csv_response(fieldnames, rows, "transfers.csv")


@router.get("/transfers/{transfer_id}", response_model=TransferResponse)
def get_transfer(transfer_id: UUID, session: Session = Depends(get_session)) -> TransferModel:
    transfer = session.get(TransferModel, str(transfer_id))
    if transfer is None:
        raise HTTPException(status_code=404, detail="transfer not found")
    return transfer


@router.get("/transfers/{transfer_id}/audit-events", response_model=list[AuditEventResponse])
def list_transfer_audit_events(
    transfer_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[AuditEventModel]:
    transfer = session.get(TransferModel, str(transfer_id))
    if transfer is None:
        raise HTTPException(status_code=404, detail="transfer not found")
    return list(
        session.scalars(
            select(AuditEventModel)
            .where(
                AuditEventModel.aggregate_type == "TRANSFER",
                AuditEventModel.aggregate_id == str(transfer_id),
            )
            .order_by(AuditEventModel.occurred_at)
            .limit(limit)
            .offset(offset)
        )
    )


@router.get("/transfers/{transfer_id}/compliance-reviews", response_model=list[ComplianceReviewResponse])
def list_transfer_compliance_reviews(
    transfer_id: UUID, session: Session = Depends(get_session)
) -> list[ComplianceReviewModel]:
    transfer = session.get(TransferModel, str(transfer_id))
    if transfer is None:
        raise HTTPException(status_code=404, detail="transfer not found")
    return list(
        session.scalars(
            select(ComplianceReviewModel)
            .where(ComplianceReviewModel.transfer_id == str(transfer_id))
            .order_by(ComplianceReviewModel.created_at)
        )
    )


@router.get("/transfers/{transfer_id}/authorizations", response_model=list[AuthorizationResponse])
def list_transfer_authorizations(
    transfer_id: UUID, session: Session = Depends(get_session)
) -> list[AuthorizationModel]:
    transfer = session.get(TransferModel, str(transfer_id))
    if transfer is None:
        raise HTTPException(status_code=404, detail="transfer not found")
    return list(
        session.scalars(
            select(AuthorizationModel)
            .where(AuthorizationModel.transfer_id == str(transfer_id))
            .order_by(AuthorizationModel.authorized_at)
        )
    )


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
    except (TransferNotPendingAuthorizationError, DuplicateAuthorizationError) as error:
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


@router.post("/transfers/{transfer_id}/reverse", response_model=TransferResponse, status_code=status.HTTP_201_CREATED)
def reverse_transfer(
    transfer_id: UUID,
    session: Session = Depends(get_session),
    current_user: UserModel = Depends(require_roles("compliance_officer", "admin")),
) -> TransferModel:
    try:
        return TransferService(session).reverse_transfer(str(transfer_id), requested_by=current_user.id)
    except TransferNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (TransferNotCompletedError, TransferAlreadyReversedError, IdempotencyConflictError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (InsufficientFundsError, ValidationError, WalletNotFoundError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/ledger/journal-entries", response_model=LedgerTransactionResponse, status_code=status.HTTP_201_CREATED)
def record_manual_journal_entry(
    payload: JournalEntryCreate,
    session: Session = Depends(get_session),
    current_user: UserModel = Depends(require_roles("admin")),
) -> LedgerTransactionModel:
    try:
        return LedgerService(session).record_manual_journal_entry(
            currency=payload.currency,
            lines=[
                JournalEntryLine(wallet_id=str(line.wallet_id), direction=line.direction, amount=line.amount)
                for line in payload.entries
            ],
            memo=payload.memo,
            entered_by=current_user.id,
        )
    except JournalWalletNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (UnbalancedJournalEntryError, JournalInsufficientFundsError, JournalValidationError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error