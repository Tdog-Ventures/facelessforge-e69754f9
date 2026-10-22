# Dodo Mongo grant concurrency fix

The Mongo mirror now enforces unique string tenant/user account identities before
changing either balance. It bootstraps missing accounts with an atomic upsert,
then increments each account only when that webhook transaction identity is not
already present in its processed_dodo_transactions array. The marker and balance
change share one document write. The audit uses a deterministic Mongo _id upsert.
Distinct concurrent grants sum correctly; replay and partial Mongo-mirror retries
do not repeat increments already recorded by this implementation.

## Deployment prerequisite

The unique partial indexes cover ff_credits.tenant_id and subscriptions.user_id
when those fields are strings. Existing duplicates must be reviewed and reconciled
before deployment; index-creation errors deliberately abort the mirror before
balance changes. This branch does not inspect or migrate production records.
Do not suppress index errors. Account transaction marker arrays grow with billing
events; monitor document growth and migrate to a transactional grant ledger before
approaching Mongo document limits. Previously applied transactions have no new
marker, so this patch does not retrospectively deduplicate historical grants.

## Remaining limitations

This is account-level Mongo atomicity, not end-to-end exactly-once fulfillment.
The webhook caller still marks Redis processed before fulfillment, expires that
marker, can fail open on Redis errors, grants Redis before the best-effort Mongo
mirror, and acknowledges Mongo-mirror failures. Thus automatic webhook replay may
never reach this retry-safe helper after a partial failure, and Redis can diverge
from Mongo. Manual reconciliation or a separate durable workflow change remains
necessary. Different webhook IDs describing the same purchase are distinct grant
identities here. The two accounts and audit are separate writes; they converge
only if the helper is retried successfully. No Redis behavior changed.

## Verification

The new offline tests first failed against the original code for lost concurrent
updates, duplicate first-account creation, repeated grants, and partial failure.
With the fix, six offline atomicity cases, three Mongo 7 integration cases, and
four existing refund cases passed (13 total). Real Mongo tests use a new isolated,
network-disabled disposable container and fresh test-only databases; they never
load application environment files. Set DODO_TEST_MONGO_URL to opt into integration
tests against a disposable server. Without it, those three cases are skipped.
TestSprite against a deployed candidate was not run: no candidate was deployed.
