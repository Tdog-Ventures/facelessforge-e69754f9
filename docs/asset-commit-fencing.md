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
The procedure below is for an operator-managed maintenance window. It was **not
performed on production** as part of this PR. Mongo snippets are examples with
incident-specific values, not an automatic unlock script.

### 1. Contain and capture; leave the barrier installed

- Restrict affected project reads/render submission as well as writes, so nobody
  consumes a partly replaced selection while it is being reconciled. Gate every
  writer to the project, including direct/admin asset routes, background workers,
  schedulers and queued retries; a lease only coordinates cooperating writers.
  Serialize recovery with any other operator. Include all affected account writers
  in the maintenance scope when reconciling credits shared across projects.
- Record the incident, application revision, project ID and exact captured
  `asset_lease_holder`, `asset_lease_fence`, `asset_commit_barrier`, timestamp and
  `asset_commit_intent`. Export a protected, type-preserving copy of these plus the
  operation record, all scene assets, authoritative account/charge IDs, legacy
  mirror and ledger rows. Do not put credentials or full account exports in a PR.
- Obtain the account location/filter from `asset_commit_intent.account`; do not
  assume subscriptions rather than the legacy `ff_credits` store. Missing intent
  may mean the process stopped before recording it (including an operation-record
  write). It is **not** proof that no command ran; use logs and stored state.
- Do not expire/remove the pin to make the endpoint work. Keep it in place through
  command drainage, reconciliation and post-reconciliation checks.

### 2. Establish command quiescence on both the clients and Mongo server

A dead request/PID, client timeout, expired lease, quiet balance, arbitrary sleep,
or one empty `currentOp` result is **not sufficient**. Motor may have submitted a
command before the client disappeared; `killOp` requests interruption rather than
proving completion. Establish all of the following and save the evidence:

1. **No source can submit/retry the old work.** Identify the API/worker processes,
   hosts/containers and Mongo connection pools that could have issued the commit.
   Drain their tasks to acknowledged completion where possible. Otherwise stop the
   affected writer instances under maintenance and disable their restart/retry/job
   producers, including supervisors and in-process pools. Prevent those instances
   reconnecting while recovery runs. Closing only the HTTP request is insufficient;
   `asset_commit.run` shields and drains its protected task after cancellation.
2. **Inventory server-side commands and connections with adequate privileges.** A
   DBA uses Mongo 7 `$currentOp` with `allUsers: true` (including idle connections /
   sessions for attribution), rather than an application user's partial view. For
   example, this is an inspection-only inventory from `mongosh`:

   ```javascript
   db.getSiblingDB("admin").aggregate([
     {$currentOp: {allUsers: true, idleConnections: true, idleSessions: true}},
     {$project: {
       opid: 1, active: 1, type: 1, ns: 1, client: 1, connectionId: 1,
       desc: 1, appName: 1, lsid: 1, secs_running: 1,
       waitingForLock: 1, killPending: 1,
       commandDb: "$command.$db", originatingDb: "$originatingCommand.$db",
       application: "$clientMetadata.application.name"
     }}
   ]).toArray()
   ```

   Correlate source endpoints, connection IDs/server connection logs, application
   metadata, namespaces, logical session IDs and request logs. Include operations
   waiting for locks, inserts/deletes, account debits/reversals, mirror/ledger and
   operation-record writes. Do **not** filter only on `project_id`: accounting
   commands often contain only a user/account filter. Application name or session
   ID alone may be shared and is not a per-request fencing token.
3. **Drain every identified old server operation.** Prefer waiting for completion
   while writers are stopped. A DBA may request interruption of a specifically
   identified operation (`killOp` using its observed `opid`) or identified session
   where appropriate. Check again until the operation is actually gone, including
   any lock-waiting/non-immediately-interruptible work; `ok: 1`/`killPending` alone
   is not completion. A command may have committed before interruption: determine
   its effect from the documents, never infer rollback from a disconnect.
4. **Close the delayed-submission gap.** Confirm the server has ended **all
   pre-incident connections from the stopped writer instances**, using connection
   inventory and server connection-end logs, and that their active/queued commands
   have drained. A still-open idle pool connection could submit work after an empty
   operation snapshot. Repeat the inventory after connection closure while the
   client/retry exclusion remains enforced. An idle logical-session catalog entry
   alone does not execute work, but its associated outstanding commands must be
   accounted for. Record the writer-stop, connection-close and command-drain times
   and the final inventories.

The proof is **excluded producers + closed old transport connections + drained
server operations**, not the elapsed time between observations. The current code
does not tag every Mongo command with the barrier token. If ownership/connection
attribution, monitoring privileges or server command state is uncertain, leave
the pin installed. Escalate to a DBA-maintained wider application/database
maintenance window where exclusion and drainage can be demonstrated. Do not
guess, kill unrelated work, or clear the pin as a diagnostic.

### 3. Fence the recovery actor while retaining the pin

After quiescence is established, re-read the project and compare it to the captured
holder/fence/barrier. Any mismatch stops this recovery plan. Using a single-document
CAS, install a unique recovery holder and **increment** the fence, retaining the
same barrier and original intent. This revokes the old holder's future lease / pin
operations; it does not replace the command-quiescence proof for commands already
submitted to other collections.

For example, with the captured values supplied as correctly typed variables:

```javascript
const capturedGuard = {
  id: projectId,
  asset_lease_holder: capturedHolder,
  asset_lease_fence: capturedFence,
  asset_commit_barrier: capturedBarrier
};
const claimed = appDb.projects.updateOne(capturedGuard, {
  $set: {asset_lease_holder: recoveryToken},
  $inc: {asset_lease_fence: 1}
});
// Require acknowledged === true and matchedCount === 1; otherwise stop.
// Re-read and retain the new holder/fence/barrier as recoveryGuard.
```

All operator work remains exclusive with the original barrier installed. Do not
reset/decrement the fence or call the ordinary asset route to bypass a pinned
commit. If a recovery command itself has an unknown outcome, keep the pin and
re-establish quiescence/re-read state before deciding its next action.

### 4. Reconcile assets, charge IDs and operation results

- Build a row-by-row plan from the saved intent and actual rows. Preserve originals
  and paid carried top-up rows; distinguish newly attempted candidates by captured
  IDs and `attach_op`, not a scene-wide deletion. Intent lists attempted candidates;
  a duplicate-key rejection may have skipped one, so it does not prove insertion.
- Choose to finish a known complete replacement/top-up, or undo only the interrupted
  new work and restore captured originals. Verify every retained/removed ID and
  stock uniqueness before any deletion. If evidence is insufficient, remain pinned.
  Manual attach also needs its previous scene/status and licence audit reconciled.
- The authoritative account's balance and **durable `charged_attach_ids`** decide
  which IDs were paid. For a retained unpaid attachment, debit with the same atomic
  sufficient-balance + ID-not-charged predicate used by the application. For a truly
  rolled-back paid candidate, refund and remove its charged ID in **one** conditional
  account update matching that ID. Check the acknowledgement and match result for
  each action. Never refund an ID whose asset remains selected, reverse previously
  paid carried rows, evict unrelated IDs, or overwrite the balance from a snapshot.
  Cross-project credits may have changed independently of this incident.
- Reconcile the legacy mirror separately. Ledger rows are best-effort audit copies,
  not proof of a debit: a missing row does not authorize another charge. Preserve
  an incident audit of every correction and repair ledger copies as necessary.
- Reconcile only the interrupted operation/scene results to the observed selected
  and paid IDs. Preserve other completed scenes. Do not mark an unpaid operation
  completed or leave a stored success summary describing rows that were rolled
  back. Retain the original idempotency key for eventual resume/replay.

### 5. Verify, release the exact pin, and resume

Verify the intended selection, originals/paid carried rows, authoritative balance,
charged IDs, mirror and operation summary under maintenance. Save the reconciliation
record and original intent **before** removing the on-document recovery evidence.
Reconfirm writer exclusion/quiescence and exact recovery ownership. Then clear the
pin and release the holder in one guarded, acknowledged update:

```javascript
const released = appDb.projects.updateOne(recoveryGuard, {
  $set: {asset_lease_holder: null, asset_lease_expires: null},
  $unset: {
    asset_commit_barrier: "", asset_commit_started: "", asset_commit_intent: ""
  }
});
// recoveryGuard includes id, recovery holder, incremented fence, exact barrier.
// Require acknowledged === true and matchedCount === 1. Never decrement the fence.
```

If this acknowledgement is lost, inspect the document before retrying; a zero-match
CAS is not permission to issue an unguarded unlock. Keep maintenance in place until
the result is understood. Once verified, resume the original idempotency key in a
controlled way and check that selected IDs and balance remain stable on replay,
then reopen ordinary traffic/workers. This is not a request to generate or render
paid media as a recovery check.

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
