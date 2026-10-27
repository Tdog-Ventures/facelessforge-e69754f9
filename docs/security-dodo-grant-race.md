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

## Retry state machine

The webhook acknowledges only after fulfillment completes. The durable
ff_dodo_fulfillments record moves pending -> redis_granted -> completed:

- pending: no grant has been applied. The Redis Lua script sets its per-payment
  marker only when HINCRBY succeeds; a failed increment removes the marker so a
  later delivery can retry.
- redis_granted: Redis credited, Mongo mirror not yet confirmed. A retry skips
  the Redis step entirely and runs only the idempotent Mongo mirror.
- completed: both stores credited; replays return already_fulfilled.

Transient Redis/Mongo errors raise DodoGrantRetryable and return HTTP 503, so
Dodo redelivers. The first durable record's tenant, product, credits and email
are canonical. A redelivery naming another tenant is rejected before any
balance change.

## Remaining limitations

This is still not a cross-store transaction. Redis is credited before the
Mongo mirror; the durable redis_granted state makes the retry skip Redis, but a
crash after Redis and before the marker write can still leave Redis credited
while the record remains pending. In that case the next Lua call sees the
existing payment marker and returns 0 rather than double-crediting, so the
mirror can still complete exactly once. If Redis loses the account data after
the marker is durable, a retry will not rebuild the Redis balance; Mongo
remains the authoritative balance read by check_credits, but the two stores can
diverge until reconciled. If Dodo exhausts its bounded retries, or a
tenant-mismatched delivery is rejected, the record needs manual reconciliation.
Historical grants without transaction markers are not retrospectively
deduplicated.

## Verification

The focused billing suite runs against a network-disabled disposable Mongo
container (private Unix socket, no published ports) and a disposable Redis
process (private Unix socket, persistence disabled). The reconciled candidate
passed 49 focused checks with zero failures and zero skips. Set
DODO_TEST_MONGO_URL and DODO_TEST_REDIS_SERVER to opt into the real-store
cases; without them those cases skip.
