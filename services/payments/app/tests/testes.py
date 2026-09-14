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
from services.payments.app.infrastructure import database, security
from services.payments.app.infrastructure.database import Base
from services.payments.app.services import transfer_service
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
	security._login_attempts.clear()

	with TestClient(app) as test_client:
		yield test_client

	Base.metadata.drop_all(bind=engine)
	engine.dispose()


def register_and_login(client: TestClient, *, role: str = "requester") -> tuple[str, dict]:
	email = f"{uuid4()}@example.com"
	password = "correct horse battery staple"
	registered = client.post(
		"/api/v1/auth/register",
		json={"email": email, "password": password, "role": role},
	)
	assert registered.status_code == 201
	logged_in = client.post("/api/v1/auth/login", data={"username": email, "password": password})
	assert logged_in.status_code == 200
	token = logged_in.json()["access_token"]
	return registered.json()["id"], {"Authorization": f"Bearer {token}"}


def create_wallet(client: TestClient, headers: dict, *, balance: str = "0", currency: str = "USD") -> dict:
	response = client.post(
		"/api/v1/wallets",
		headers=headers,
		json={"owner_id": str(uuid4()), "currency": currency, "initial_balance": balance},
	)
	assert response.status_code == 201
	return response.json()


def test_register_and_login_returns_working_token(client: TestClient) -> None:
	_, headers = register_and_login(client)

	wallet = create_wallet(client, headers)

	assert wallet["status"] == "ACTIVE"


def test_login_rejects_wrong_password(client: TestClient) -> None:
	email = f"{uuid4()}@example.com"
	client.post("/api/v1/auth/register", json={"email": email, "password": "correct-password", "role": "requester"})

	response = client.post("/api/v1/auth/login", data={"username": email, "password": "wrong-password"})

	assert response.status_code == 401


def test_login_rate_limits_after_repeated_failures(client: TestClient) -> None:
	email = f"{uuid4()}@example.com"
	client.post("/api/v1/auth/register", json={"email": email, "password": "correct-password", "role": "requester"})

	for _ in range(security.LOGIN_RATE_LIMIT_MAX_ATTEMPTS):
		response = client.post("/api/v1/auth/login", data={"username": email, "password": "wrong-password"})
		assert response.status_code == 401

	limited = client.post("/api/v1/auth/login", data={"username": email, "password": "wrong-password"})

	assert limited.status_code == 429


def test_login_returns_refresh_token_that_mints_new_access_token(client: TestClient) -> None:
	email = f"{uuid4()}@example.com"
	password = "correct horse battery staple"
	client.post("/api/v1/auth/register", json={"email": email, "password": password, "role": "requester"})
	logged_in = client.post("/api/v1/auth/login", data={"username": email, "password": password})
	refresh_token = logged_in.json()["refresh_token"]
	new_wallet_payload = {"owner_id": str(uuid4()), "currency": "USD", "initial_balance": "0"}

	refreshed = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
	rejected_reuse_as_access = client.post(
		"/api/v1/wallets", headers={"Authorization": f"Bearer {refresh_token}"}, json=new_wallet_payload
	)
	create_with_new_access = client.post(
		"/api/v1/wallets",
		headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"},
		json=new_wallet_payload,
	)

	assert refreshed.status_code == 200
	assert rejected_reuse_as_access.status_code == 401
	assert create_with_new_access.status_code == 201


def test_refresh_rejects_access_token(client: TestClient) -> None:
	_, headers = register_and_login(client)
	access_token = headers["Authorization"].removeprefix("Bearer ")

	response = client.post("/api/v1/auth/refresh", json={"refresh_token": access_token})

	assert response.status_code == 401


def test_register_rejects_duplicate_email(client: TestClient) -> None:
	email = f"{uuid4()}@example.com"
	payload = {"email": email, "password": "correct horse battery staple", "role": "requester"}

	first = client.post("/api/v1/auth/register", json=payload)
	duplicate = client.post("/api/v1/auth/register", json=payload)

	assert first.status_code == 201
	assert duplicate.status_code == 409


def test_create_wallet_rejects_missing_token(client: TestClient) -> None:
	response = client.post(
		"/api/v1/wallets",
		json={"owner_id": str(uuid4()), "currency": "USD", "initial_balance": "0"},
	)

	assert response.status_code == 401


@pytest.mark.parametrize("currency", ["USD", "eur"])
def test_health_and_wallet_creation(client: TestClient, currency: str) -> None:
	assert client.get("/api/v1/health").json() == {"status": "ok"}
	_, headers = register_and_login(client)

	wallet = create_wallet(client, headers, currency=currency)

	assert wallet["currency"] == currency.upper()
	assert wallet["status"] == "ACTIVE"
	assert Decimal(wallet["balance"]) == Decimal("0")


def test_create_wallet_rejects_duplicate_owner_and_currency(client: TestClient) -> None:
	_, headers = register_and_login(client)
	payload = {"owner_id": str(uuid4()), "currency": "USD", "initial_balance": "10"}

	first = client.post("/api/v1/wallets", headers=headers, json=payload)
	duplicate = client.post("/api/v1/wallets", headers=headers, json=payload)

	assert first.status_code == 201
	assert duplicate.status_code == 409


def test_get_wallet_returns_wallet_and_not_found(client: TestClient) -> None:
	_, headers = register_and_login(client)
	wallet = create_wallet(client, headers, balance="12.50")

	found = client.get(f"/api/v1/wallets/{wallet['id']}")
	missing = client.get(f"/api/v1/wallets/{uuid4()}")

	assert found.status_code == 200
	assert Decimal(found.json()["balance"]) == Decimal("12.50")
	assert missing.status_code == 404


def test_update_wallet_status_freezes_and_blocks_transfers(client: TestClient) -> None:
	_, headers = register_and_login(client)
	_, admin_headers = register_and_login(client, role="admin")
	source = create_wallet(client, headers, balance="20")
	destination = create_wallet(client, headers)

	frozen = client.patch(
		f"/api/v1/wallets/{source['id']}/status", headers=admin_headers, json={"status": "FROZEN"}
	)
	blocked_transfer = client.post(
		"/api/v1/transfers",
		headers={**headers, "Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "5",
			"currency": "USD",
		},
	)

	assert frozen.status_code == 200
	assert frozen.json()["status"] == "FROZEN"
	assert blocked_transfer.status_code == 409


def test_update_wallet_status_rejects_wrong_role(client: TestClient) -> None:
	_, headers = register_and_login(client)
	wallet = create_wallet(client, headers)

	response = client.patch(f"/api/v1/wallets/{wallet['id']}/status", headers=headers, json={"status": "FROZEN"})

	assert response.status_code == 403


def test_update_wallet_status_rejects_closing_nonzero_balance(client: TestClient) -> None:
	_, headers = register_and_login(client)
	_, admin_headers = register_and_login(client, role="admin")
	wallet = create_wallet(client, headers, balance="10")

	response = client.patch(
		f"/api/v1/wallets/{wallet['id']}/status", headers=admin_headers, json={"status": "CLOSED"}
	)

	assert response.status_code == 409


def test_update_wallet_status_rejects_invalid_transition(client: TestClient) -> None:
	_, headers = register_and_login(client)
	_, admin_headers = register_and_login(client, role="admin")
	wallet = create_wallet(client, headers)
	client.patch(f"/api/v1/wallets/{wallet['id']}/status", headers=admin_headers, json={"status": "CLOSED"})

	response = client.patch(
		f"/api/v1/wallets/{wallet['id']}/status", headers=admin_headers, json={"status": "ACTIVE"}
	)

	assert response.status_code == 409


def test_create_transfer_rejects_missing_token(client: TestClient) -> None:
	_, headers = register_and_login(client)
	source = create_wallet(client, headers, balance="100")
	destination = create_wallet(client, headers)

	response = client.post(
		"/api/v1/transfers",
		headers={"Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "25.00",
			"currency": "USD",
		},
	)

	assert response.status_code == 401


def test_create_transfer_returns_pending_transfer_and_is_idempotent(client: TestClient) -> None:
	requester_id, headers = register_and_login(client)
	source = create_wallet(client, headers, balance="100")
	destination = create_wallet(client, headers)
	payload = {
		"source_wallet_id": source["id"],
		"destination_wallet_id": destination["id"],
		"amount": "25.00",
		"currency": "usd",
	}

	first = client.post(
		"/api/v1/transfers", headers={**headers, "Idempotency-Key": "transfer-1"}, json=payload
	)
	retry = client.post(
		"/api/v1/transfers", headers={**headers, "Idempotency-Key": "transfer-1"}, json=payload
	)

	assert first.status_code == 201
	assert retry.status_code == 201
	assert first.json()["id"] == retry.json()["id"]
	assert first.json()["status"] == "PENDING_COMPLIANCE"
	assert first.json()["requested_by"] == requester_id


def test_create_transfer_rejects_idempotency_key_with_different_data(client: TestClient) -> None:
	_, headers = register_and_login(client)
	source = create_wallet(client, headers, balance="100")
	destination = create_wallet(client, headers)
	base_payload = {
		"source_wallet_id": source["id"],
		"destination_wallet_id": destination["id"],
		"amount": "25.00",
		"currency": "USD",
	}

	first = client.post(
		"/api/v1/transfers",
		headers={**headers, "Idempotency-Key": "conflicting-transfer"},
		json=base_payload,
	)
	conflicting = client.post(
		"/api/v1/transfers",
		headers={**headers, "Idempotency-Key": "conflicting-transfer"},
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
	_, headers = register_and_login(client)
	source = create_wallet(client, headers, balance="100")
	destination = create_wallet(client, headers)
	payload = {
		"source_wallet_id": source["id"],
		"destination_wallet_id": destination["id"],
		"amount": "25.00",
		"currency": "USD",
		**change,
	}

	response = client.post(
		"/api/v1/transfers", headers={**headers, "Idempotency-Key": str(uuid4())}, json=payload
	)

	assert response.status_code == expected_status


def test_list_wallets_returns_all_wallets(client: TestClient) -> None:
	_, headers = register_and_login(client)
	first = create_wallet(client, headers, currency="USD")
	second = create_wallet(client, headers, currency="EUR")

	response = client.get("/api/v1/wallets")

	assert response.status_code == 200
	ids = {wallet["id"] for wallet in response.json()}
	assert {first["id"], second["id"]} <= ids


def test_list_wallets_filters_by_currency_and_paginates(client: TestClient) -> None:
	_, headers = register_and_login(client)
	create_wallet(client, headers, currency="USD")
	eur_wallet = create_wallet(client, headers, currency="EUR")

	filtered = client.get("/api/v1/wallets", params={"currency": "eur"})
	limited = client.get("/api/v1/wallets", params={"limit": 1})

	assert filtered.status_code == 200
	assert {wallet["id"] for wallet in filtered.json()} == {eur_wallet["id"]}
	assert limited.status_code == 200
	assert len(limited.json()) == 1


def test_list_transfers_returns_all_transfers(client: TestClient) -> None:
	_, headers = register_and_login(client)
	source = create_wallet(client, headers, balance="20")
	destination = create_wallet(client, headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**headers, "Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "5",
			"currency": "USD",
		},
	)

	response = client.get("/api/v1/transfers")

	assert response.status_code == 200
	ids = {transfer["id"] for transfer in response.json()}
	assert created.json()["id"] in ids


def test_list_transfers_filters_by_status(client: TestClient) -> None:
	_, headers = register_and_login(client)
	source = create_wallet(client, headers, balance="20")
	destination = create_wallet(client, headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**headers, "Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "5",
			"currency": "USD",
		},
	)

	pending = client.get("/api/v1/transfers", params={"status": "PENDING_COMPLIANCE"})
	completed = client.get("/api/v1/transfers", params={"status": "COMPLETED"})

	assert created.json()["id"] in {transfer["id"] for transfer in pending.json()}
	assert created.json()["id"] not in {transfer["id"] for transfer in completed.json()}


def test_get_transfer_returns_transfer_and_not_found(client: TestClient) -> None:
	_, headers = register_and_login(client)
	source = create_wallet(client, headers, balance="20")
	destination = create_wallet(client, headers)
	response = client.post(
		"/api/v1/transfers",
		headers={**headers, "Idempotency-Key": "lookup-transfer"},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "5",
			"currency": "USD",
		},
	)
	transfer_id = response.json()["id"]

	found = client.get(f"/api/v1/transfers/{transfer_id}")
	missing = client.get(f"/api/v1/transfers/{uuid4()}")

	assert found.status_code == 200
	assert found.json()["id"] == transfer_id
	assert missing.status_code == 404


def test_review_compliance_rejects_wrong_role(client: TestClient) -> None:
	_, requester_headers = register_and_login(client)
	source = create_wallet(client, requester_headers, balance="50")
	destination = create_wallet(client, requester_headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**requester_headers, "Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
		},
	)

	reviewed = client.post(
		f"/api/v1/transfers/{created.json()['id']}/compliance-review",
		headers=requester_headers,
		json={"decision": "APPROVED"},
	)

	assert reviewed.status_code == 403


def test_review_compliance_approves_and_advances_transfer(client: TestClient) -> None:
	_, requester_headers = register_and_login(client)
	_, compliance_headers = register_and_login(client, role="compliance_officer")
	source = create_wallet(client, requester_headers, balance="50")
	destination = create_wallet(client, requester_headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**requester_headers, "Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
		},
	)

	reviewed = client.post(
		f"/api/v1/transfers/{created.json()['id']}/compliance-review",
		headers=compliance_headers,
		json={"decision": "APPROVED"},
	)

	assert reviewed.status_code == 200
	assert reviewed.json()["status"] == "PENDING_AUTHORIZATION"


def test_review_compliance_rejects_transfer_and_records_failure_reason(client: TestClient) -> None:
	_, requester_headers = register_and_login(client)
	_, compliance_headers = register_and_login(client, role="compliance_officer")
	source = create_wallet(client, requester_headers, balance="50")
	destination = create_wallet(client, requester_headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**requester_headers, "Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
		},
	)

	reviewed = client.post(
		f"/api/v1/transfers/{created.json()['id']}/compliance-review",
		headers=compliance_headers,
		json={"decision": "REJECTED", "reason_code": "AML_HIT", "reason": "sanctions match"},
	)

	assert reviewed.status_code == 200
	assert reviewed.json()["status"] == "REJECTED"
	assert reviewed.json()["failure_code"] == "AML_HIT"


def test_review_compliance_rejects_when_not_pending_compliance(client: TestClient) -> None:
	_, requester_headers = register_and_login(client)
	_, compliance_headers = register_and_login(client, role="compliance_officer")
	source = create_wallet(client, requester_headers, balance="50")
	destination = create_wallet(client, requester_headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**requester_headers, "Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
		},
	)
	transfer_id = created.json()["id"]
	client.post(
		f"/api/v1/transfers/{transfer_id}/compliance-review",
		headers=compliance_headers,
		json={"decision": "APPROVED"},
	)

	second = client.post(
		f"/api/v1/transfers/{transfer_id}/compliance-review",
		headers=compliance_headers,
		json={"decision": "APPROVED"},
	)

	assert second.status_code == 409


def test_authorize_transfer_rejects_wrong_role(client: TestClient) -> None:
	_, requester_headers = register_and_login(client)
	_, compliance_headers = register_and_login(client, role="compliance_officer")
	source = create_wallet(client, requester_headers, balance="50")
	destination = create_wallet(client, requester_headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**requester_headers, "Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
		},
	)
	transfer_id = created.json()["id"]
	client.post(
		f"/api/v1/transfers/{transfer_id}/compliance-review",
		headers=compliance_headers,
		json={"decision": "APPROVED"},
	)

	authorized = client.post(
		f"/api/v1/transfers/{transfer_id}/authorize",
		headers=requester_headers,
		json={"decision": "APPROVED", "policy_version": "policy-v1"},
	)

	assert authorized.status_code == 403


def test_authorize_transfer_approves_and_enables_settlement(client: TestClient) -> None:
	_, requester_headers = register_and_login(client)
	_, compliance_headers = register_and_login(client, role="compliance_officer")
	_, authorizer_headers = register_and_login(client, role="authorizer")
	source = create_wallet(client, requester_headers, balance="50")
	destination = create_wallet(client, requester_headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**requester_headers, "Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
		},
	)
	transfer_id = created.json()["id"]
	client.post(
		f"/api/v1/transfers/{transfer_id}/compliance-review",
		headers=compliance_headers,
		json={"decision": "APPROVED"},
	)

	authorized = client.post(
		f"/api/v1/transfers/{transfer_id}/authorize",
		headers=authorizer_headers,
		json={"decision": "APPROVED", "policy_version": "policy-v1"},
	)
	settled = client.post(f"/api/v1/transfers/{transfer_id}/settle", headers=requester_headers)

	assert authorized.status_code == 200
	assert authorized.json()["status"] == "APPROVED"
	assert settled.status_code == 200
	assert settled.json()["status"] == "COMPLETED"


def test_authorize_transfer_rejects_self_authorization(client: TestClient) -> None:
	_, compliance_headers = register_and_login(client, role="compliance_officer")
	_, authorizer_headers = register_and_login(client, role="authorizer")
	source = create_wallet(client, authorizer_headers, balance="50")
	destination = create_wallet(client, authorizer_headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**authorizer_headers, "Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
		},
	)
	transfer_id = created.json()["id"]
	client.post(
		f"/api/v1/transfers/{transfer_id}/compliance-review",
		headers=compliance_headers,
		json={"decision": "APPROVED"},
	)

	authorized = client.post(
		f"/api/v1/transfers/{transfer_id}/authorize",
		headers=authorizer_headers,
		json={"decision": "APPROVED", "policy_version": "policy-v1"},
	)

	assert authorized.status_code == 403


def test_authorize_transfer_rejects_when_not_pending_authorization(client: TestClient) -> None:
	_, requester_headers = register_and_login(client)
	_, authorizer_headers = register_and_login(client, role="authorizer")
	source = create_wallet(client, requester_headers, balance="50")
	destination = create_wallet(client, requester_headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**requester_headers, "Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
		},
	)
	transfer_id = created.json()["id"]

	authorized = client.post(
		f"/api/v1/transfers/{transfer_id}/authorize",
		headers=authorizer_headers,
		json={"decision": "APPROVED", "policy_version": "policy-v1"},
	)

	assert authorized.status_code == 409


def test_authorize_transfer_requires_n_of_m_approvals(
	client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
	monkeypatch.setattr(transfer_service, "AUTHORIZATION_THRESHOLD", 2)
	_, requester_headers = register_and_login(client)
	_, compliance_headers = register_and_login(client, role="compliance_officer")
	_, first_authorizer = register_and_login(client, role="authorizer")
	_, second_authorizer = register_and_login(client, role="authorizer")
	source = create_wallet(client, requester_headers, balance="50")
	destination = create_wallet(client, requester_headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**requester_headers, "Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
		},
	)
	transfer_id = created.json()["id"]
	client.post(
		f"/api/v1/transfers/{transfer_id}/compliance-review",
		headers=compliance_headers,
		json={"decision": "APPROVED"},
	)

	first_decision = client.post(
		f"/api/v1/transfers/{transfer_id}/authorize",
		headers=first_authorizer,
		json={"decision": "APPROVED", "policy_version": "policy-v1"},
	)
	second_decision = client.post(
		f"/api/v1/transfers/{transfer_id}/authorize",
		headers=second_authorizer,
		json={"decision": "APPROVED", "policy_version": "policy-v1"},
	)

	assert first_decision.status_code == 200
	assert first_decision.json()["status"] == "PENDING_AUTHORIZATION"
	assert second_decision.status_code == 200
	assert second_decision.json()["status"] == "APPROVED"


def test_authorize_transfer_rejects_duplicate_authorizer(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(transfer_service, "AUTHORIZATION_THRESHOLD", 2)
	_, requester_headers = register_and_login(client)
	_, compliance_headers = register_and_login(client, role="compliance_officer")
	_, authorizer_headers = register_and_login(client, role="authorizer")
	source = create_wallet(client, requester_headers, balance="50")
	destination = create_wallet(client, requester_headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**requester_headers, "Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
		},
	)
	transfer_id = created.json()["id"]
	client.post(
		f"/api/v1/transfers/{transfer_id}/compliance-review",
		headers=compliance_headers,
		json={"decision": "APPROVED"},
	)
	client.post(
		f"/api/v1/transfers/{transfer_id}/authorize",
		headers=authorizer_headers,
		json={"decision": "APPROVED", "policy_version": "policy-v1"},
	)

	duplicate = client.post(
		f"/api/v1/transfers/{transfer_id}/authorize",
		headers=authorizer_headers,
		json={"decision": "APPROVED", "policy_version": "policy-v1"},
	)

	assert duplicate.status_code == 409


def test_transfer_lifecycle_writes_chained_audit_events(client: TestClient) -> None:
	_, requester_headers = register_and_login(client)
	_, compliance_headers = register_and_login(client, role="compliance_officer")
	_, authorizer_headers = register_and_login(client, role="authorizer")
	source = create_wallet(client, requester_headers, balance="50")
	destination = create_wallet(client, requester_headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**requester_headers, "Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
		},
	)
	transfer_id = created.json()["id"]
	client.post(
		f"/api/v1/transfers/{transfer_id}/compliance-review",
		headers=compliance_headers,
		json={"decision": "APPROVED"},
	)
	client.post(
		f"/api/v1/transfers/{transfer_id}/authorize",
		headers=authorizer_headers,
		json={"decision": "APPROVED", "policy_version": "policy-v1"},
	)
	client.post(f"/api/v1/transfers/{transfer_id}/settle", headers=requester_headers)

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
	_, headers = register_and_login(client)
	source = create_wallet(client, headers, balance="100")
	destination = create_wallet(client, headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**headers, "Idempotency-Key": "settlement-transfer"},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "25.00",
			"currency": "USD",
		},
	)
	approve_transfer(created.json()["id"])

	settled = client.post(f"/api/v1/transfers/{created.json()['id']}/settle", headers=headers)

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
	_, headers = register_and_login(client)
	source = create_wallet(client, headers, balance="100")
	destination = create_wallet(client, headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**headers, "Idempotency-Key": "failed-settlement"},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "25.00",
			"currency": "USD",
		},
	)
	transfer_id = created.json()["id"]
	approve_transfer(transfer_id)
	with database.SessionLocal() as session:
		wallet = session.get(WalletModel, source["id"])
		assert wallet is not None
		wallet.balance = Decimal("0")
		session.commit()

	settled = client.post(f"/api/v1/transfers/{transfer_id}/settle", headers=headers)

	assert settled.status_code == 409
	found = client.get(f"/api/v1/transfers/{transfer_id}")
	assert found.json()["status"] == "FAILED"
	assert found.json()["failure_code"] == "INSUFFICIENT_FUNDS"


def test_settle_transfer_rejects_pending_transfer_without_ledger_entries(client: TestClient) -> None:
	_, headers = register_and_login(client)
	source = create_wallet(client, headers, balance="100")
	destination = create_wallet(client, headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**headers, "Idempotency-Key": "pending-settlement"},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "25.00",
			"currency": "USD",
		},
	)

	settled = client.post(f"/api/v1/transfers/{created.json()['id']}/settle", headers=headers)

	assert settled.status_code == 409
	with database.SessionLocal() as session:
		ledger_transaction = session.scalar(
			select(LedgerTransactionModel).where(
				LedgerTransactionModel.transfer_id == created.json()["id"]
			)
		)

	assert ledger_transaction is None


def test_reverse_transfer_creates_swapped_compensating_transfer(client: TestClient) -> None:
	_, headers = register_and_login(client)
	_, admin_headers = register_and_login(client, role="admin")
	source = create_wallet(client, headers, balance="100")
	destination = create_wallet(client, headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**headers, "Idempotency-Key": "reversal-original"},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "25.00",
			"currency": "USD",
		},
	)
	transfer_id = created.json()["id"]
	approve_transfer(transfer_id)
	client.post(f"/api/v1/transfers/{transfer_id}/settle", headers=headers)

	reversal = client.post(f"/api/v1/transfers/{transfer_id}/reverse", headers=admin_headers)

	assert reversal.status_code == 201
	body = reversal.json()
	assert body["reversal_of_transfer_id"] == transfer_id
	assert body["source_wallet_id"] == destination["id"]
	assert body["destination_wallet_id"] == source["id"]
	assert body["amount"] == "25.0000"
	assert body["status"] == "PENDING_COMPLIANCE"


def test_reverse_transfer_rejects_non_completed_transfer(client: TestClient) -> None:
	_, headers = register_and_login(client)
	_, admin_headers = register_and_login(client, role="admin")
	source = create_wallet(client, headers, balance="100")
	destination = create_wallet(client, headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**headers, "Idempotency-Key": "reversal-pending"},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "25.00",
			"currency": "USD",
		},
	)

	response = client.post(f"/api/v1/transfers/{created.json()['id']}/reverse", headers=admin_headers)

	assert response.status_code == 409


def test_reverse_transfer_rejects_double_reversal(client: TestClient) -> None:
	_, headers = register_and_login(client)
	_, admin_headers = register_and_login(client, role="admin")
	source = create_wallet(client, headers, balance="100")
	destination = create_wallet(client, headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**headers, "Idempotency-Key": "reversal-double"},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "25.00",
			"currency": "USD",
		},
	)
	transfer_id = created.json()["id"]
	approve_transfer(transfer_id)
	client.post(f"/api/v1/transfers/{transfer_id}/settle", headers=headers)
	client.post(f"/api/v1/transfers/{transfer_id}/reverse", headers=admin_headers)

	second_attempt = client.post(f"/api/v1/transfers/{transfer_id}/reverse", headers=admin_headers)

	assert second_attempt.status_code == 409


def test_record_manual_journal_entry_updates_balances(client: TestClient) -> None:
	_, headers = register_and_login(client)
	_, admin_headers = register_and_login(client, role="admin")
	first = create_wallet(client, headers, balance="100", currency="USD")
	second = create_wallet(client, headers, balance="0", currency="USD")

	response = client.post(
		"/api/v1/ledger/journal-entries",
		headers=admin_headers,
		json={
			"currency": "USD",
			"memo": "correcting entry",
			"entries": [
				{"wallet_id": first["id"], "direction": "DEBIT", "amount": "15.00"},
				{"wallet_id": second["id"], "direction": "CREDIT", "amount": "15.00"},
			],
		},
	)

	assert response.status_code == 201
	body = response.json()
	assert body["transfer_id"] is None
	assert body["memo"] == "correcting entry"
	assert len(body["entries"]) == 2
	with database.SessionLocal() as session:
		stored_first = session.get(WalletModel, first["id"])
		stored_second = session.get(WalletModel, second["id"])
	assert stored_first.balance == Decimal("85.0000")
	assert stored_second.balance == Decimal("15.0000")


def test_record_manual_journal_entry_rejects_unbalanced_entries(client: TestClient) -> None:
	_, headers = register_and_login(client)
	_, admin_headers = register_and_login(client, role="admin")
	first = create_wallet(client, headers, balance="100", currency="USD")
	second = create_wallet(client, headers, balance="0", currency="USD")

	response = client.post(
		"/api/v1/ledger/journal-entries",
		headers=admin_headers,
		json={
			"currency": "USD",
			"entries": [
				{"wallet_id": first["id"], "direction": "DEBIT", "amount": "15.00"},
				{"wallet_id": second["id"], "direction": "CREDIT", "amount": "10.00"},
			],
		},
	)

	assert response.status_code == 409


def test_record_manual_journal_entry_rejects_wrong_role(client: TestClient) -> None:
	_, headers = register_and_login(client)
	first = create_wallet(client, headers, balance="100", currency="USD")
	second = create_wallet(client, headers, balance="0", currency="USD")

	response = client.post(
		"/api/v1/ledger/journal-entries",
		headers=headers,
		json={
			"currency": "USD",
			"entries": [
				{"wallet_id": first["id"], "direction": "DEBIT", "amount": "15.00"},
				{"wallet_id": second["id"], "direction": "CREDIT", "amount": "15.00"},
			],
		},
	)

	assert response.status_code == 403


def test_list_wallet_ledger_entries_returns_entries_after_settlement(client: TestClient) -> None:
	_, headers = register_and_login(client)
	source = create_wallet(client, headers, balance="100")
	destination = create_wallet(client, headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**headers, "Idempotency-Key": "ledger-entries-transfer"},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "25.00",
			"currency": "USD",
		},
	)
	approve_transfer(created.json()["id"])
	client.post(f"/api/v1/transfers/{created.json()['id']}/settle", headers=headers)

	source_entries = client.get(f"/api/v1/wallets/{source['id']}/ledger-entries")
	destination_entries = client.get(f"/api/v1/wallets/{destination['id']}/ledger-entries")

	assert source_entries.status_code == 200
	assert [entry["direction"] for entry in source_entries.json()] == ["DEBIT"]
	assert [entry["direction"] for entry in destination_entries.json()] == ["CREDIT"]


def test_export_wallet_ledger_entries_returns_csv(client: TestClient) -> None:
	_, headers = register_and_login(client)
	source = create_wallet(client, headers, balance="100")
	destination = create_wallet(client, headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**headers, "Idempotency-Key": "export-ledger-entries"},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "25.00",
			"currency": "USD",
		},
	)
	approve_transfer(created.json()["id"])
	client.post(f"/api/v1/transfers/{created.json()['id']}/settle", headers=headers)

	response = client.get(f"/api/v1/wallets/{source['id']}/ledger-entries/export")

	assert response.status_code == 200
	assert response.headers["content-type"].startswith("text/csv")
	lines = response.text.strip().splitlines()
	assert lines[0] == "id,ledger_transaction_id,wallet_id,direction,amount,currency,created_at"
	assert len(lines) == 2
	assert "DEBIT" in lines[1]


def test_export_transfers_returns_csv(client: TestClient) -> None:
	_, headers = register_and_login(client)
	source = create_wallet(client, headers, balance="20")
	destination = create_wallet(client, headers)
	client.post(
		"/api/v1/transfers",
		headers={**headers, "Idempotency-Key": "export-transfers"},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "5",
			"currency": "USD",
		},
	)

	response = client.get("/api/v1/transfers/export", params={"status": "PENDING_COMPLIANCE"})

	assert response.status_code == 200
	assert response.headers["content-type"].startswith("text/csv")
	lines = response.text.strip().splitlines()
	assert lines[0].startswith("id,idempotency_key,source_wallet_id")
	assert len(lines) == 2


def test_list_wallet_ledger_entries_rejects_unknown_wallet(client: TestClient) -> None:
	response = client.get(f"/api/v1/wallets/{uuid4()}/ledger-entries")

	assert response.status_code == 404


def test_list_transfer_audit_events_returns_chained_history(client: TestClient) -> None:
	_, requester_headers = register_and_login(client)
	_, compliance_headers = register_and_login(client, role="compliance_officer")
	source = create_wallet(client, requester_headers, balance="50")
	destination = create_wallet(client, requester_headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**requester_headers, "Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
		},
	)
	transfer_id = created.json()["id"]
	client.post(
		f"/api/v1/transfers/{transfer_id}/compliance-review",
		headers=compliance_headers,
		json={"decision": "APPROVED"},
	)

	response = client.get(f"/api/v1/transfers/{transfer_id}/audit-events")

	assert response.status_code == 200
	events = response.json()
	assert [event["event_type"] for event in events] == ["TRANSFER_REQUESTED", "COMPLIANCE_APPROVED"]
	assert events[0]["previous_event_hash"] is None
	assert events[1]["previous_event_hash"] == events[0]["event_hash"]


def test_list_transfer_audit_events_rejects_unknown_transfer(client: TestClient) -> None:
	response = client.get(f"/api/v1/transfers/{uuid4()}/audit-events")

	assert response.status_code == 404


def test_list_transfer_compliance_reviews_and_authorizations(client: TestClient) -> None:
	_, requester_headers = register_and_login(client)
	_, compliance_headers = register_and_login(client, role="compliance_officer")
	_, authorizer_headers = register_and_login(client, role="authorizer")
	source = create_wallet(client, requester_headers, balance="50")
	destination = create_wallet(client, requester_headers)
	created = client.post(
		"/api/v1/transfers",
		headers={**requester_headers, "Idempotency-Key": str(uuid4())},
		json={
			"source_wallet_id": source["id"],
			"destination_wallet_id": destination["id"],
			"amount": "10",
			"currency": "USD",
		},
	)
	transfer_id = created.json()["id"]
	client.post(
		f"/api/v1/transfers/{transfer_id}/compliance-review",
		headers=compliance_headers,
		json={"decision": "APPROVED"},
	)
	client.post(
		f"/api/v1/transfers/{transfer_id}/authorize",
		headers=authorizer_headers,
		json={"decision": "APPROVED", "policy_version": "policy-v1"},
	)

	reviews = client.get(f"/api/v1/transfers/{transfer_id}/compliance-reviews")
	authorizations = client.get(f"/api/v1/transfers/{transfer_id}/authorizations")

	assert reviews.status_code == 200
	assert [review["decision"] for review in reviews.json()] == ["APPROVED"]
	assert authorizations.status_code == 200
	assert [authorization["decision"] for authorization in authorizations.json()] == ["APPROVED"]


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
