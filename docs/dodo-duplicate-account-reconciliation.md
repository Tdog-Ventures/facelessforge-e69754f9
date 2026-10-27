# Dodo Duplicate-Account Reconciliation Plan

## Scope

PR #22 requires unique partial indexes on string identities:
- `ff_credits.tenant_id`, named `ff_credits_tenant_unique`.
- `subscriptions.user_id`, named `subscriptions_user_unique`.

The inventory script is read-only. This document does not authorize or perform a production migration.

## Inventory

Run `scripts/inventory_dodo_duplicate_accounts.py` with a read-only connection
against a restored backup first. Use a private output directory and restrictive
permissions: the report contains customer identities and account history.
A URI without a database selects `facelessforge`; confirm the intended database
before any later production scan. Inspect the JSON summary: exit zero means the
scan succeeded, not that no duplicates exist.

## Reconstruct balances before selecting a survivor

For each identity, export every duplicate document and its transaction markers.
Establish entitlement from unique successful purchases, actual consumption,
refunds/reversals and documented adjustments. Do not sum duplicate balances:
two documents can represent the same purchase. Do not select an authoritative
balance merely because it is larger or more recent.

Choose one surviving document after reconciling the history. Preserve valid
provider identifiers and current plan/period information from verified records.
Unresolved payments or usage are a stop condition requiring a documented
decision; never invent a balancing adjustment.

## Dry-run and execution

1. Verify a restorable backup and produce a per-identity dry-run showing the
   source documents, unique payment history, calculated balance, transaction
   marker union, surviving document and exact deletion IDs.
2. Before the final inventory and writes, pause **all relevant account writers**
   and drain in-flight work, including webhooks, credit consumption, refunds
   and other provisioning paths. Keep them paused through reconciliation and
   index verification; a low-traffic window alone does not prevent lost writes.
3. Re-read the source documents and abort if they differ from the approved
   dry-run. Update the survivor before deleting duplicates. Check matched
   counts and resulting balances before each deletion. Keep a migration journal
   so an interrupted run resumes from source IDs rather than adding totals again.
4. In a Mongo update, exclude `processed_dodo_transactions` from `$set` when
   applying its union through `$addToSet`. Each field must appear under only one
   update operator. Preserve the union even where balance reconstruction counts
   a duplicated purchase only once.
5. Re-run inventory. Establish **both** named unique partial indexes explicitly,
   then inspect their keys, uniqueness and partial filters before reopening writes.
   Do not rely on the first paying customer's webhook to establish the indexes.
6. Verify the release candidate's grant, replay, refund and customer credit-gate
   checks in isolation before deploying it. Restore queued processing only after
   the migration and deployment checks pass.

## Historical payment cutover

Payment-keyed fulfillment does not automatically migrate grants made by the old
webhook-ID-based implementation. Reconcile historical successful Dodo payments
that may be redelivered, mapping each payment to its verified tenant, product,
credit amount and applied/reversed state before enabling the new handler.

An old processed webhook marker alone does not prove Mongo mirroring completed:
the old implementation recorded markers before a best-effort mirror. Do not
seed every old marker as a completed fulfillment or replay old events blindly.
Use verified payment and grant history, preserve existing consumption, and
exercise an already-granted historical payment redelivery on restored data.
Ambiguous historical state remains a deployment blocker.

## Recovery limits

These changes do not make Redis and Mongo one transaction. Test restart and
Redis-loss recovery separately; never clear payment markers and retry pending
payments without reconciling the surviving ledger and balance state. The Mongo
index preflight prevents Mongo balance mutations on duplicate-index errors, but
the outer fulfillment flow may already have credited Redis.

No production records, indexes, services or payments were changed by these
instructions or by the isolated verification.

