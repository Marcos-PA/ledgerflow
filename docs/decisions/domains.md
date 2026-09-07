# Domain Decision: Wallet and Ledger

## Context

The system moves money between wallets, applies compliance and authorization
controls, and must provide a complete, tamper-evident audit trail. A mutable
wallet balance alone is not sufficient because it cannot explain how a balance
was produced or safely recover from retries and failures.

## Decision

Separate the transfer workflow from the accounting record:

- `Transfer` represents the requested business operation and its workflow.
- `LedgerTransaction` represents the immutable accounting operation created
  only when the transfer is approved for settlement.
- `LedgerEntry` represents each debit or credit posting in a ledger
  transaction.
- `Wallet.balance` is a derived, transactionally maintained projection and
  must never be edited independently of ledger postings.
- Compliance reviews, authorizations, and audit events are separate
  append-only records.

The relationship between these components is shown in
[`domains.mmd`](./domains.mmd).

## Identifiers and common fields

- Entity identifiers use UUIDs.
- Every persisted entity has `created_at` and `updated_at` timestamps unless
  explicitly stated otherwise.
- Timestamps are stored in UTC.
- Actors are represented by an `actor_id` (user, service, or system identity)
  and an `actor_type`.

## Wallet

A wallet holds funds for one owner in one currency.

- `id`: UUID
- `owner_id`: UUID
- `currency`: ISO 4217 currency code
- `balance`: Decimal, non-negative, with a fixed scale defined per currency
- `status`: `ACTIVE | FROZEN | CLOSED`
- `created_at`: datetime
- `updated_at`: datetime

Invariants:

- A wallet has exactly one currency.
- `balance` is updated atomically with the corresponding ledger postings.
- A `FROZEN` or `CLOSED` wallet cannot send or receive transfers.
- The database must enforce uniqueness for `(owner_id, currency)` if an owner may have at most one wallet per currency.

## Transfer

`Transfer` is the user-facing workflow for moving funds. It is not the source
of truth for accounting.

- `id`: UUID
- `idempotency_key`: unique key supplied by the caller
- `source_wallet_id`: UUID
- `destination_wallet_id`: UUID
- `amount`: positive Decimal
- `currency`: ISO 4217 currency code
- `status`: see lifecycle below
- `failure_code`: nullable machine-readable failure reason
- `failure_reason`: nullable human-readable explanation
- `requested_by`: actor identity
- `requested_at`: datetime
- `approved_at`: nullable datetime
- `completed_at`: nullable datetime
- `rejected_at`: nullable datetime
- `created_at`: datetime
- `updated_at`: datetime

Invariants:

- `amount > 0`.
- Source and destination wallets must be different.
- Both wallets must exist, be active, and have the transfer currency.
- Insufficient funds reject the transfer; overdrafts are not allowed.
- Reusing an `idempotency_key` returns the original transfer and cannot create
  a second accounting operation.
- A transfer is settled exactly once.

### Transfer lifecycle

Allowed transitions:

```text
PENDING_COMPLIANCE -> REJECTED
PENDING_COMPLIANCE -> PENDING_AUTHORIZATION
PENDING_AUTHORIZATION -> REJECTED
PENDING_AUTHORIZATION -> APPROVED
APPROVED -> COMPLETED
APPROVED -> FAILED
```

`COMPLETED`, `FAILED`, and `REJECTED` are terminal states. A completed
transfer is never changed to `FLAGGED` or `REJECTED`. A correction or refund
is represented by a new transfer with its own ledger transaction.

## LedgerTransaction

An immutable accounting transaction records the settlement of one transfer.

- `id`: UUID
- `transfer_id`: UUID, unique
- `currency`: ISO 4217 currency code
- `posted_at`: datetime
- `created_at`: datetime

Rules:

- A ledger transaction has exactly two or more entries.
- The sum of debit entries equals the sum of credit entries.
- All entries in a ledger transaction use the same currency.
- Ledger transactions and entries are append-only; corrections use reversing
  entries or a new transfer.
- A ledger transaction is created in the same database transaction as the
  balance projection update.

## LedgerEntry

Each entry is one side of a balanced accounting operation.

- `id`: UUID
- `ledger_transaction_id`: UUID
- `wallet_id`: UUID
- `direction`: `DEBIT | CREDIT`
- `amount`: positive Decimal
- `currency`: ISO 4217 currency code
- `created_at`: datetime

For a wallet transfer, the source wallet receives a `DEBIT` and the
destination wallet receives a `CREDIT` for the same amount.

## ComplianceReview

Compliance determines whether a transfer may proceed.

- `id`: UUID
- `transfer_id`: UUID
- `reviewer_id`: nullable actor identity
- `decision`: `PENDING | APPROVED | REJECTED | FLAGGED`
- `reason_code`: nullable machine-readable code
- `reason`: nullable explanation
- `reviewed_at`: nullable datetime
- `created_at`: datetime

Reviews are append-only. A flagged transfer remains outside settlement until a
new review explicitly approves it. Multiple reviews may exist; the latest
authoritative review is identified by the workflow transition, not by deleting
earlier reviews.

## Authorization

Authorization records the approval required to settle a transfer.

- `id`: UUID
- `transfer_id`: UUID
- `authorizer_id`: actor identity
- `decision`: `APPROVED | REJECTED`
- `reason`: nullable explanation
- `authorized_at`: datetime

An authorizer cannot authorize their own transfer. Authorization records are
append-only and must reference the policy or permission version used.

## AuditEvent

Audit events provide an immutable history of security- and money-relevant
actions.

- `id`: UUID
- `aggregate_type`: `WALLET | TRANSFER | LEDGER_TRANSACTION`
- `aggregate_id`: UUID
- `event_type`: event name
- `actor_id`: nullable actor identity for system events
- `actor_type`: `USER | SERVICE | SYSTEM`
- `reason`: nullable explanation
- `metadata`: structured JSON with non-sensitive context
- `occurred_at`: datetime
- `previous_event_hash`: nullable hash
- `event_hash`: hash of the canonical event payload

Audit events are append-only and must not contain secrets or unnecessary
personal data. Events are written in the same transaction as the state change
they describe.

## Consistency and failure handling

- Compliance, authorization, settlement, ledger postings, and balance
  updates are explicit workflow steps.
- Settlement locks both wallets in a deterministic order to prevent races.
- Settlement uses a database transaction and a unique `transfer_id` on
  `LedgerTransaction` to prevent double posting.
- External notifications are emitted after commit using an outbox or
  equivalent durable mechanism.
- Failed or retried requests do not create partial ledger postings.

## Consequences

This model provides traceable balances, safe retries, explicit state
transitions, and auditable compliance decisions. It requires transactional
database constraints, an outbox for integration events, and a balance
projection that can be rebuilt and reconciled from immutable ledger entries.
