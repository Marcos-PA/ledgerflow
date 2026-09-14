import os
from collections.abc import Generator
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from services.payments.app.domain import (
	ActorType,
	AuditAggregateType,
	AuditEvent,
	Authorization,
	AuthorizationDecision,
	LedgerDirection,
	LedgerEntry,
	LedgerTransaction,
	Transfer,
	TransferStatus,
	Wallet,
	WalletStatus,
)
from services.payments.app.infrastructure import database
from services.payments.app.infrastructure.database import Base
from services.payments.app.infrastructure.models import (
	AuditEventModel,
	LedgerEntryModel,
	LedgerTransactionModel,
	TransferModel,
	WalletModel,
)
from services.payments.app.main import app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
	test_database_url = os.environ.get("TEST_DATABASE_URL")
	if test_database_url:
		engine = create_engine(test_database_url)
	else:
		engine = create_engine(
			"sqlite://",
			connect_args={"check_same_thread": False},
			poolclass=StaticPool,
		)
	session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
	monkeypatch.setattr(database, "engine", engine)
	monkeypatch.setattr(database, "SessionLocal", session_factory)
	Base.metadata.create_all(bind=engine)

	with TestClient(app) as test_client:
		yield test_client

	Base.metadata.drop_all(bind=engine)
	engine.dispose()


def create_wallet(client: TestClient, *, balance: str = "0", currency: str = "USD") -> dict:
	response = client.post(
		"/api/v1/wallets",
		json={"owner_id": str(uuid4()), "currency": currency, "initial_balance": balance},
	)
	assert response.status_code == 201
	return response.json()


@pytest.mark.parametrize("currency", ["USD", "eur"])
def test_health_and_wallet_creation(client: TestClient, currency: str) -> None:
	assert client.get("/api/v1/health").json() == {"status": "ok"}
	wallet = create_wallet(client, currency=currency)

	assert wallet["currency"] == currency.upper()
	assert wallet["status"] == "ACTIVE"
	assert Decimal(wallet["balance"]) == Decimal("0")


def test_create_wallet_rejects_duplicate_owner_and_currency(client: TestClient) -> None:
	owner_id = str(uuid4())
	payload = {"owner_id": owner_id, "currency": "USD", "initial_balance": "10"}

	first = client.post("/api/v1/wallets", json=payload)
	duplicate = client.post("/api/v1/wallets", json=payload)

	assert first.status_code == 201
	assert duplicate.status_code == 409


def test_get_wallet_returns_wallet_and_not_found(client: TestClient) -> None:
	wallet = create_wallet(client, balance="12.50")

	found = client.get(f"/api/v1/wallets/{wallet['id']}")
	missing = client.get(f"/api/v1/wallets/{uuid4()}")

	assert found.status_code == 200
	assert Decimal(found.json()["balance"]) == Decimal("12.50")
	assert missing.status_code == 404


def test_create_transfer_returns_pending_transfer_and_is_idempotent(client: TestClient) -> None:
	source = create_wallet(client, balance="100")
	destination = create_wallet(client)
	payload = {
		"source_wallet_id": source["id"],
		"destination_wallet_id": destination["id"],
		"amount": "25.00",
		"currency": "usd",
		"requested_by": str(uuid4()),
	}

	first = client.post("/api/v1/transfers", headers={"Idempotency-Key": "transfer-1"}, json=payload)
	retry = client.post("/api/v1/transfers", headers={"Idempotency-Key": "transfer-1"}, json=payload)

	assert first.status_code == 201
	assert retry.status_code == 201
	assert first.json()["id"] == retry.json()["id"]
	assert first.json()["status"] == "PENDING_COMPLIANCE"


def test_create_transfer_rejects_idempotency_key_with_different_data(client: TestClient) -> None:
	source = create_wallet(client, balance="100")
	destination = create_wallet(client)
	base_payload = {
		"source_wallet_id": source["id"],
		"destination_wallet_id": destination["id"],
		"amount": "25.00",
		"currency": "USD",
		"requested_by": str(uuid4()),
	}

	first = client.post(
		"/api/v1/transfers",
		headers={"Idempotency-Key": "conflicting-transfer"},
		json=base_payload,
	)
	conflicting = client.post(
		"/api/v1/transfers",
		headers={"Idempotency-Key": "conflicting-transfer"},
		json={**base_payload, "amount": "30.00"},
	)

	assert first.status_code == 201
	assert conflicting.status_code == 409
	assert conflicting.json()["detail"] == "idempotency key was used with different transfer data"


@pytest.mark.parametrize(
	("change", "expected_status"),
	[
		({"amount": "100.01"}, 409),
		({"currency": "EUR"}, 400),
	],
)
def test_create_transfer_rejects_invalid_money_rules(
	client: TestClient,
	change: dict[str, str],
	expected_status: int,
) -> None:
	source = create_wallet(client, balance="100")
	destination = create_wallet(client)
	payload = {
		"source_wallet_id": source["id"],
		"destination_wallet_id": destination["id"],
		"amount": "25.00",
		"currency": "USD",
		"requested_by": str(uuid4()),
		**change,
	}

	response = client.post("/api/v1/transfers", headers={"Idempotency-Key": str(uuid4())}, json=payload)

	assert response.status_code == expected_status


def test_list_wallets_returns_all_wallets(client: TestClient) -> None:
	first = create_wallet(client, currency="USD")
	second = create_wallet(client, currency="EUR")

	response = client.get("/api/v1/wallets")

	assert response.status_code == 200
	ids = {wallet["id"] for wallet in response.json()}
	assert {first["id"], second["id"]} <= ids


def test_list_transfers_returns_all_transfers(client: TestClient) -> None:
	source = create_wallet(client, balance="20")
	destination = create_wallet(client)
	created = client.post(
		"/api/v1/transfers",
		headers={"Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "5",
			"currency": "USD",
			"requested_by": str(uuid4()),
		},
	)

	response = client.get("/api/v1/transfers")

	assert response.status_code == 200
	ids = {transfer["id"] for transfer in response.json()}
	assert created.json()["id"] in ids


def test_get_transfer_returns_transfer_and_not_found(client: TestClient) -> None:
	source = create_wallet(client, balance="20")
	destination = create_wallet(client)
	response = client.post(
		"/api/v1/transfers",
		headers={"Idempotency-Key": "lookup-transfer"},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "5",
			"currency": "USD",
			"requested_by": str(uuid4()),
		},
	)
	transfer_id = response.json()["id"]

	found = client.get(f"/api/v1/transfers/{transfer_id}")
	missing = client.get(f"/api/v1/transfers/{uuid4()}")

	assert found.status_code == 200
	assert found.json()["id"] == transfer_id
	assert missing.status_code == 404


def test_review_compliance_approves_and_advances_transfer(client: TestClient) -> None:
	source = create_wallet(client, balance="50")
	destination = create_wallet(client)
	created = client.post(
		"/api/v1/transfers",
		headers={"Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
			"requested_by": str(uuid4()),
		},
	)

	reviewed = client.post(
		f"/api/v1/transfers/{created.json()['id']}/compliance-review",
		json={"decision": "APPROVED", "reviewer_id": str(uuid4())},
	)

	assert reviewed.status_code == 200
	assert reviewed.json()["status"] == "PENDING_AUTHORIZATION"


def test_review_compliance_rejects_transfer_and_records_failure_reason(client: TestClient) -> None:
	source = create_wallet(client, balance="50")
	destination = create_wallet(client)
	created = client.post(
		"/api/v1/transfers",
		headers={"Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
			"requested_by": str(uuid4()),
		},
	)

	reviewed = client.post(
		f"/api/v1/transfers/{created.json()['id']}/compliance-review",
		json={"decision": "REJECTED", "reason_code": "AML_HIT", "reason": "sanctions match"},
	)

	assert reviewed.status_code == 200
	assert reviewed.json()["status"] == "REJECTED"
	assert reviewed.json()["failure_code"] == "AML_HIT"


def test_review_compliance_rejects_when_not_pending_compliance(client: TestClient) -> None:
	source = create_wallet(client, balance="50")
	destination = create_wallet(client)
	created = client.post(
		"/api/v1/transfers",
		headers={"Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
			"requested_by": str(uuid4()),
		},
	)
	transfer_id = created.json()["id"]
	client.post(f"/api/v1/transfers/{transfer_id}/compliance-review", json={"decision": "APPROVED"})

	second = client.post(f"/api/v1/transfers/{transfer_id}/compliance-review", json={"decision": "APPROVED"})

	assert second.status_code == 409


def test_authorize_transfer_approves_and_enables_settlement(client: TestClient) -> None:
	source = create_wallet(client, balance="50")
	destination = create_wallet(client)
	requested_by = str(uuid4())
	created = client.post(
		"/api/v1/transfers",
		headers={"Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
			"requested_by": requested_by,
		},
	)
	transfer_id = created.json()["id"]
	client.post(f"/api/v1/transfers/{transfer_id}/compliance-review", json={"decision": "APPROVED"})

	authorized = client.post(
		f"/api/v1/transfers/{transfer_id}/authorize",
		json={"authorizer_id": str(uuid4()), "decision": "APPROVED", "policy_version": "policy-v1"},
	)
	settled = client.post(f"/api/v1/transfers/{transfer_id}/settle")

	assert authorized.status_code == 200
	assert authorized.json()["status"] == "APPROVED"
	assert settled.status_code == 200
	assert settled.json()["status"] == "COMPLETED"


def test_authorize_transfer_rejects_self_authorization(client: TestClient) -> None:
	source = create_wallet(client, balance="50")
	destination = create_wallet(client)
	requested_by = str(uuid4())
	created = client.post(
		"/api/v1/transfers",
		headers={"Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
			"requested_by": requested_by,
		},
	)
	transfer_id = created.json()["id"]
	client.post(f"/api/v1/transfers/{transfer_id}/compliance-review", json={"decision": "APPROVED"})

	authorized = client.post(
		f"/api/v1/transfers/{transfer_id}/authorize",
		json={"authorizer_id": requested_by, "decision": "APPROVED", "policy_version": "policy-v1"},
	)

	assert authorized.status_code == 403


def test_authorize_transfer_rejects_when_not_pending_authorization(client: TestClient) -> None:
	source = create_wallet(client, balance="50")
	destination = create_wallet(client)
	created = client.post(
		"/api/v1/transfers",
		headers={"Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
			"requested_by": str(uuid4()),
		},
	)
	transfer_id = created.json()["id"]

	authorized = client.post(
		f"/api/v1/transfers/{transfer_id}/authorize",
		json={"authorizer_id": str(uuid4()), "decision": "APPROVED", "policy_version": "policy-v1"},
	)

	assert authorized.status_code == 409


def test_transfer_lifecycle_writes_chained_audit_events(client: TestClient) -> None:
	source = create_wallet(client, balance="50")
	destination = create_wallet(client)
	created = client.post(
		"/api/v1/transfers",
		headers={"Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
			"requested_by": str(uuid4()),
		},
	)
	transfer_id = created.json()["id"]
	client.post(f"/api/v1/transfers/{transfer_id}/compliance-review", json={"decision": "APPROVED"})
	client.post(
		f"/api/v1/transfers/{transfer_id}/authorize",
		json={"authorizer_id": str(uuid4()), "decision": "APPROVED", "policy_version": "policy-v1"},
	)
	client.post(f"/api/v1/transfers/{transfer_id}/settle")

	with database.SessionLocal() as session:
		events = session.scalars(
			select(AuditEventModel)
			.where(AuditEventModel.aggregate_id == transfer_id)
			.order_by(AuditEventModel.occurred_at)
		).all()

	assert [event.event_type for event in events] == [
		"TRANSFER_REQUESTED",
		"COMPLIANCE_APPROVED",
		"AUTHORIZATION_APPROVED",
		"SETTLEMENT_COMPLETED",
	]
	assert events[0].previous_event_hash is None
	for previous, current in zip(events, events[1:]):
		assert current.previous_event_hash == previous.event_hash
	assert len({event.event_hash for event in events}) == len(events)


def approve_transfer(transfer_id: str) -> None:
	with database.SessionLocal() as session:
		transfer = session.get(TransferModel, transfer_id)
		assert transfer is not None
		transfer.status = "APPROVED"
		session.commit()


def test_settle_transfer_updates_balances_and_creates_balanced_entries(client: TestClient) -> None:
	source = create_wallet(client, balance="100")
	destination = create_wallet(client)
	created = client.post(
		"/api/v1/transfers",
		headers={"Idempotency-Key": "settlement-transfer"},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "25.00",
			"currency": "USD",
			"requested_by": str(uuid4()),
		},
	)
	approve_transfer(created.json()["id"])

	settled = client.post(f"/api/v1/transfers/{created.json()['id']}/settle")

	assert settled.status_code == 200
	assert settled.json()["status"] == "COMPLETED"
	with database.SessionLocal() as session:
		stored_source = session.get(WalletModel, source["id"])
		stored_destination = session.get(WalletModel, destination["id"])
		ledger_transaction = session.scalar(
			select(LedgerTransactionModel).where(
				LedgerTransactionModel.transfer_id == created.json()["id"]
			)
		)
		assert ledger_transaction is not None
		entries = session.scalars(
			select(LedgerEntryModel).where(
				LedgerEntryModel.ledger_transaction_id == ledger_transaction.id
			)
		).all()

	assert stored_source is not None
	assert stored_destination is not None
	assert stored_source.balance == Decimal("75.0000")
	assert stored_destination.balance == Decimal("25.0000")
	assert len(entries) == 2
	assert {entry.direction for entry in entries} == {"DEBIT", "CREDIT"}
	assert sum(entry.amount for entry in entries if entry.direction == "DEBIT") == Decimal("25.0000")
	assert sum(entry.amount for entry in entries if entry.direction == "CREDIT") == Decimal("25.0000")


def test_settle_transfer_marks_transfer_failed_on_insufficient_funds(client: TestClient) -> None:
	source = create_wallet(client, balance="100")
	destination = create_wallet(client)
	created = client.post(
		"/api/v1/transfers",
		headers={"Idempotency-Key": "failed-settlement"},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "25.00",
			"currency": "USD",
			"requested_by": str(uuid4()),
		},
	)
	transfer_id = created.json()["id"]
	approve_transfer(transfer_id)
	with database.SessionLocal() as session:
		wallet = session.get(WalletModel, source["id"])
		assert wallet is not None
		wallet.balance = Decimal("0")
		session.commit()

	settled = client.post(f"/api/v1/transfers/{transfer_id}/settle")

	assert settled.status_code == 409
	found = client.get(f"/api/v1/transfers/{transfer_id}")
	assert found.json()["status"] == "FAILED"
	assert found.json()["failure_code"] == "INSUFFICIENT_FUNDS"


def test_settle_transfer_rejects_pending_transfer_without_ledger_entries(client: TestClient) -> None:
	source = create_wallet(client, balance="100")
	destination = create_wallet(client)
	created = client.post(
		"/api/v1/transfers",
		headers={"Idempotency-Key": "pending-settlement"},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "25.00",
			"currency": "USD",
			"requested_by": str(uuid4()),
		},
	)

	settled = client.post(f"/api/v1/transfers/{created.json()['id']}/settle")

	assert settled.status_code == 409
	with database.SessionLocal() as session:
		ledger_transaction = session.scalar(
			select(LedgerTransactionModel).where(
				LedgerTransactionModel.transfer_id == created.json()["id"]
			)
		)

	assert ledger_transaction is None


@pytest.mark.parametrize(
	("operation", "amount", "expected_balance"),
	[("debit", Decimal("3"), Decimal("7")), ("credit", Decimal("4"), Decimal("14"))],
)
def test_wallet_balance_operations(operation: str, amount: Decimal, expected_balance: Decimal) -> None:
	wallet = Wallet(uuid4(), "usd", Decimal("10"))

	getattr(wallet, operation)(amount)

	assert wallet.balance == expected_balance


@pytest.mark.parametrize(
	("status", "operation", "message"),
	[
		(WalletStatus.FROZEN, "credit", "only active wallets"),
		(WalletStatus.ACTIVE, "debit", "insufficient funds"),
	],
)
def test_wallet_rejects_invalid_balance_operations(
	status: WalletStatus,
	operation: str,
	message: str,
) -> None:
	wallet = Wallet(uuid4(), "USD", Decimal("5"), status=status)

	with pytest.raises(ValueError, match=message):
		getattr(wallet, operation)(Decimal("10"))


def test_transfer_allows_valid_transitions_and_records_timestamps() -> None:
	transfer = Transfer("key", uuid4(), uuid4(), Decimal("5"), "USD", uuid4())

	transfer.transition_to(TransferStatus.PENDING_AUTHORIZATION)
	transfer.transition_to(TransferStatus.APPROVED)

	assert transfer.status is TransferStatus.APPROVED
	assert transfer.approved_at is not None


@pytest.mark.parametrize("target", [TransferStatus.COMPLETED, TransferStatus.FAILED])
def test_transfer_rejects_invalid_direct_terminal_transitions(target: TransferStatus) -> None:
	transfer = Transfer("key", uuid4(), uuid4(), Decimal("5"), "USD", uuid4())

	with pytest.raises(ValueError, match="invalid transfer transition"):
		transfer.transition_to(target)


def test_ledger_transaction_accepts_balanced_entries() -> None:
	transaction = LedgerTransaction(uuid4(), "USD")
	transaction.add_entry(LedgerEntry(transaction.id, uuid4(), LedgerDirection.DEBIT, Decimal("10"), "USD"))
	transaction.add_entry(LedgerEntry(transaction.id, uuid4(), LedgerDirection.CREDIT, Decimal("10"), "USD"))

	transaction.validate()

	assert len(transaction.entries) == 2


def test_ledger_transaction_rejects_unbalanced_entries() -> None:
	transaction = LedgerTransaction(uuid4(), "USD")
	transaction.add_entry(LedgerEntry(transaction.id, uuid4(), LedgerDirection.DEBIT, Decimal("10"), "USD"))

	with pytest.raises(ValueError, match="balanced"):
		transaction.add_entry(LedgerEntry(transaction.id, uuid4(), LedgerDirection.CREDIT, Decimal("9"), "USD"))


def test_authorization_allows_independent_authorizer() -> None:
	requester, authorizer = uuid4(), uuid4()

	authorization = Authorization(
		uuid4(), authorizer, AuthorizationDecision.APPROVED, "policy-v1", requester
	)

	assert authorization.policy_version == "policy-v1"


def test_authorization_rejects_self_authorization() -> None:
	actor = uuid4()

	with pytest.raises(ValueError, match="cannot authorize their own"):
		Authorization(uuid4(), actor, AuthorizationDecision.APPROVED, "policy-v1", actor)


def test_audit_events_have_distinct_hashes_and_chain_reference() -> None:
	first = AuditEvent(AuditAggregateType.TRANSFER, uuid4(), "CREATED", ActorType.SYSTEM)
	second = AuditEvent(
		AuditAggregateType.TRANSFER,
		first.aggregate_id,
		"APPROVED",
		ActorType.SERVICE,
		previous_event_hash=first.event_hash,
	)

	assert len(first.event_hash) == 64
	assert second.previous_event_hash == first.event_hash
	assert second.event_hash != first.event_hash
