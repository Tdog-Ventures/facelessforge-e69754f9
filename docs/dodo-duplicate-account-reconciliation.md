# Dodo Duplicate-Account Reconciliation Plan

## Purpose

PR #22 (`codex/security-billing-grant-race`) creates two unique partial indexes:

- `ff_credits.tenant_id` unique where `tenant_id` is a string
- `subscriptions.user_id` unique where `user_id` is a string

These indexes fail to create if duplicate documents already exist. This plan
describes how to inventory, decide, and reconcile duplicates without losing
purchased credits or transaction history. **No production data is modified by
this document or by the accompanying read-only inventory script.**

## Read-only inventory

Run the provided script against a read-only Mongo connection string (or a
recent backup restored to an isolated host):

```bash
python3 scripts/inventory_dodo_duplicate_accounts.py \
  --mongo-url 'mongodb://readonly@host/facelessforge' \
  --out /tmp/dodo_duplicate_inventory.json
```

The script outputs, per collection, every identity that maps to more than one
document, together with the relevant fields of each duplicate document:
- `_id`, `tenant_id`/`user_id`, balances, plan, email, transaction IDs, dates.

## Decision rules for reconciliation

For each duplicate group, choose a **canonical** document and merge the others
into it. Suggested precedence (operator may override):

1. **Most recently updated** document (likely the authoritative record).
2. If tied, the document with the **largest remaining balance**.
3. If still tied, the document whose `_id` sorts last (deterministic tie-break).

## Merge procedure (dry-run first)

For each duplicate group in `ff_credits`:

1. Identify canonical doc `C` and duplicate docs `D1..Dn`.
2. Compute merged values:
   - `credits = sum(C.credits, D1.credits, ..., Dn.credits)`
   - `processed_dodo_transactions = union(all)`
   - `updated_at = now(UTC)`
   - Preserve `email`, `plan`, `tenant_id`, `user_id` from `C`.
3. Print the intended update and deletion list (dry-run).
4. After operator approval:
   - `db.ff_credits.update_one({_id: C._id}, {$set: merged_values, $addToSet: {processed_dodo_transactions: {$each: union}}})`
   - `db.ff_credits.delete_many({_id: {$in: [D1._id, ..., Dn._id]}})`

For each duplicate group in `subscriptions`:

1. Identify canonical doc `C` and duplicates `D1..Dn`.
2. Compute merged values:
   - `credits_remaining = sum(C.credits_remaining, D1.credits_remaining, ..., Dn.credits_remaining)`
   - `credits_monthly = max(C.credits_monthly, D1.credits_monthly, ..., Dn.credits_monthly)`
     (monthly quota should reflect the current plan, not a sum of old periods)
   - `processed_dodo_transactions = union(all)`
   - `current_period_end = max(C.current_period_end, D1.current_period_end, ..., Dn.current_period_end)`
   - `updated_at = now(UTC)`
3. Preserve `plan`, `status`, `user_id`, `paddle_*` IDs from `C`.
4. After dry-run approval, update `C` and delete duplicates as above.

## Safety checklist before executing

- [ ] Backup production MongoDB (mongodump) verified restorable.
- [ ] Inventory run in the last 24 hours; no new duplicates appeared.
- [ ] Reconciliation script reviewed and run in dry-run mode first.
- [ ] Rollback plan: restore from backup if balance totals diverge.
- [ ] Operator sign-off on the merge decisions.
- [ ] Execution during low-traffic window; pause Dodo webhooks during merge if
      possible, or accept that new grants arriving during merge may race with
      the new unique indexes and need retry.

## Post-reconciliation deployment

After duplicates are removed:

1. Deploy the candidate image containing PR #22.
2. The first Dodo webhook will create the unique partial indexes automatically.
3. Monitor `billing_dodo` logs for `DuplicateKeyError`; any remaining duplicate
   will abort the mirror before balance changes, preserving safety.

## Release blocker status

**This reconciliation is a deployment prerequisite.** The new billing atomicity
fix cannot create its unique indexes until duplicates are resolved. However, the
reconciliation itself does not require code changes; it requires an approved,
supervised data migration.
