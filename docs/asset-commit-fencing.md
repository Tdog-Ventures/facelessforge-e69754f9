# Asset commit fencing on standalone MongoDB

## Topology and safety boundary

The review environment's read-only `hello`/`server_info` inspection returned
MongoDB **7.0.34**, topology **Single**, `setName: null`, `msg: null`.
Multi-document transactions are unavailable. The connection is the local Mongo
endpoint described in the deployment guide; no topology change is required.

`project_lease.acquire` and `asset_commit.run` compete on the **same project
document**. The latter atomically compares the holder/fence and installs a unique
`asset_commit_barrier`. Acquisition requires that barrier to be absent, including
when the ordinary lookup lease has expired. This is a durable critical section,
not an ownership read followed by an unrelated write:

- Takeover wins first: the old holder's barrier CAS fails; it performs no insert,
  original deletion, debit, reversal, rollback or operation-record mutation.
- Commit wins first: ownership cannot transfer until all awaited protected writes
  and compensation have finished and the exact barrier is cleared.

Stock lookup is outside the barrier. Auto-attach mutation/compensation, carried
attachment accounting, operation records, account creation and manual insertion /
scene status are inside it. Protected callbacks use the wrapped database and may
not spawn unawaited writes. Ordinary request cancellation drains the protected
task before clearing its pin. Existing per-attachment charge IDs are retained
without eviction; paid retries and partial top-up accounting remain idempotent.
Before scene mutation, `asset_commit_intent` durably captures the originals,
candidate documents, operation key and authoritative account location; manual
attach records its candidate and previous scene. This evidence stays with a
pinned commit and is removed only after acknowledged completion.

## Failure/recovery policy (intentional availability limitation)

A process death during commit, an unacknowledged write or a PyMongo write error
with unknown outcome leaves the barrier **pinned indefinitely**. No expiry,
background worker, request retry or lease release may unlock it. Later asset
writers return conflict; an ambiguous commit returns 503. This prevents delayed
commands from an old process changing a new holder's selection or balance.

There is deliberately no automatic crash recovery on this standalone topology.
Before an operator clears a pin:

1. Quiesce the original writer and drain/terminate its outstanding Mongo commands.
   A missing process or an expired timestamp by itself is not proof that a command
   is no longer executing on the database.
2. Capture the project holder/fence/barrier and the operation record, scene assets,
   authoritative account's `charged_attach_ids` and ledger audit copies.
3. Reconcile the interrupted scene: finish a complete replacement's unpaid IDs, or
   remove partial new rows and restore captured originals from `asset_commit_intent`.
   Never reverse a paid ID that remains selected. The balance and charge IDs are
   authoritative; ledger insertion is best effort and may need audit repair.
4. Only after reconciliation and command quiescence, clear the **exact captured**
   barrier using a project/holder/fence/barrier compare-and-set; never an unguarded
   update or a TTL. Retry the original idempotency key.

This runbook does not authorize production recovery/restarts. Automatic recovery
would require a fenced recovery executor and a command-quiescence protocol
(or a supported transaction topology). This availability/recovery
limitation must remain visible in the PR; the barrier is not a transaction and
does not provide atomic reader snapshots of an in-progress replacement.

## Verification

`backend/tests/test_asset_commit_fencing.py` bounds every async scenario and covers
lookup takeover, takeover immediately inside barrier CAS, same/different operation
keys, current-holder completion, replay without duplicate charges, preservation of
current assets/account/operation records, manual attach fencing, denial of expiry
takeover during deletion/debit/reversal/rollback, cancellation drain and an applied
debit whose acknowledgement was lost. The last test exercises explicit quiesced
recovery and same-key resume without charging the paid attachment twice.
