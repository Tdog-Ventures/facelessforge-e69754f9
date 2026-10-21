"""Regression tests: render-failure credit reversal (idempotent, auditable).

Covers:
- queue_render stamps credit_charge and writes a job-linked charge ledger row
- terminal render failure reverses the charge BEFORE the job is marked failed
- duplicate failure / repeated reversal calls never double-credit (idempotent)
- full-access (Dodo bypass) jobs and legacy unstamped jobs are no-ops
- stale-job recovery reverses crashed jobs
- known-good completed jobs (17253296, 3cd769ec shapes) are never touched
- queue-insert failure after a successful deduct refunds immediately
"""
import pytest

from app import render

_real_run_render_safe = render._run_render_safe


from backend.tests.fake_db import FakeCollection as ProjectCollection  # honours lease CAS filters


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, limit):
        return self._docs[:limit]


class FakeCollection:
    """Minimal async collection: equality/$in/$gte/$lt matching, unique refund_id."""

    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    def _match(self, doc, query):
        for key, cond in (query or {}).items():
            val = doc.get(key)
            if isinstance(cond, dict):
                if "$in" in cond and val not in cond["$in"]:
                    return False
                if "$gte" in cond and not (val is not None and val >= cond["$gte"]):
                    return False
                if "$lt" in cond and not (val is not None and val < cond["$lt"]):
                    return False
                if not any(op in cond for op in ("$in", "$gte", "$lt")) and val != cond:
                    return False
            elif val != cond:
                return False
        return True

    async def find_one(self, query, projection=None, sort=None):
        for doc in self.docs:
            if self._match(doc, query):
                return dict(doc)
        return None

    def find(self, query, projection=None):
        return _Cursor([dict(d) for d in self.docs if self._match(d, query)])

    async def insert_one(self, doc):
        doc = dict(doc)
        if doc.get("refund_id"):
            if any(d.get("refund_id") == doc["refund_id"] for d in self.docs):
                exc = Exception("E11000 duplicate key error collection: credit_transactions")
                exc.code = 11000  # type: ignore[attr-defined]
                raise exc
        self.docs.append(doc)

    async def update_one(self, query, update, upsert=False):
        for doc in self.docs:
            if self._match(doc, query):
                doc.update(update.get("$set", {}) or {})
                for k, v in (update.get("$inc", {}) or {}).items():
                    doc[k] = doc.get(k, 0) + v
                return
        if upsert:
            new = dict(query)
            new.update(update.get("$set", {}) or {})
            self.docs.append(new)

    async def update_many(self, query, update):
        n = 0
        for doc in self.docs:
            if self._match(doc, query):
                doc.update(update.get("$set", {}) or {})
                n += 1
        return type("R", (), {"modified_count": n})()

    async def count_documents(self, query):
        return sum(1 for d in self.docs if self._match(d, query))

    async def find_one_and_update(self, query, update, return_document=None):
        for doc in self.docs:
            if self._match(doc, query):
                doc.update(update.get("$set", {}) or {})
                for k, v in (update.get("$inc", {}) or {}).items():
                    doc[k] = doc.get(k, 0) + v
                return dict(doc)
        return None

    async def create_index(self, *args, **kwargs):
        return "refund_id_1"


def _db(subs=(), ffs=(), jobs=(), ledger=()):
    class FakeDB:
        subscriptions = FakeCollection(subs)
        ff_credits = FakeCollection(ffs)
        render_jobs = FakeCollection(jobs)
        credit_transactions = FakeCollection(ledger)
        # The render lease lives on the project record, so the fixture must hold one.
        projects = ProjectCollection("projects", [{"id": "project-1", "user_id": "user-1", "production_mode": "editorial"}])

    return FakeDB()


@pytest.fixture(autouse=True)
def _reset_refund_index_flag(monkeypatch):
    monkeypatch.setattr(render, "_REFUND_INDEX_READY", False)


async def _queue(monkeypatch, db, project_id="project-1", user="user-1"):
    """queue_render with the background worker stubbed out."""

    async def _noop_safe(_job_id, _project_id):
        return None

    monkeypatch.setattr(render, "get_db", lambda: db)
    monkeypatch.setattr(render, "_run_render_safe", _noop_safe)
    return await render.queue_render(project_id, requested_by=user)


@pytest.mark.asyncio
async def test_queue_render_deducts_and_stamps_charge(monkeypatch):
    db = _db(subs=[{"user_id": "user-1", "credits_remaining": 10}])
    job = await _queue(monkeypatch, db)

    assert db.subscriptions.docs[0]["credits_remaining"] == 7
    stored = await db.render_jobs.find_one({"id": job["id"]})
    charge = stored["credit_charge"]
    assert charge == {
        "user_id": "user-1",
        "amount": render.RENDER_CREDIT_COST,
        "store": "subscriptions",
        "ledger_id": charge["ledger_id"],
    }
    charge_rows = [r for r in db.credit_transactions.docs if r["amount"] == -3]
    assert len(charge_rows) == 1
    assert charge_rows[0]["job_id"] == job["id"]
    assert charge_rows[0]["reason"] == f"render project-1"


@pytest.mark.asyncio
async def test_failed_render_reverses_before_marking_failed(monkeypatch):
    db = _db(subs=[{"user_id": "user-1", "credits_remaining": 10}])
    job = await _queue(monkeypatch, db)
    assert db.subscriptions.docs[0]["credits_remaining"] == 7

    async def fail(_job_id, _project_id):
        raise RuntimeError("R2 upload failure: SignatureDoesNotMatch")

    order = []

    async def capture_set_job(_job_id, **patch):
        # At the moment the job is marked failed the refund must already exist.
        refund = await db.credit_transactions.find_one({"refund_id": f"refund:{_job_id}"})
        order.append(("set_job", patch.get("status"), bool(refund)))

    # _queue stubbed the worker; restore the real _run_render_safe for this phase.
    monkeypatch.setattr(render, "_run_render_safe", _real_run_render_safe)
    monkeypatch.setattr(render, "get_db", lambda: db)
    monkeypatch.setattr(render, "_run_render", fail)
    monkeypatch.setattr(render, "_set_job", capture_set_job)

    await render._run_render_safe(job["id"], "project-1")

    assert order == [("set_job", "failed", True)], "reversal must land before status=failed"
    assert db.subscriptions.docs[0]["credits_remaining"] == 10
    refunds = [r for r in db.credit_transactions.docs if r.get("refund_id")]
    assert len(refunds) == 1
    assert refunds[0]["amount"] == 3
    assert refunds[0]["job_id"] == job["id"]
    assert refunds[0]["user_id"] == "user-1"
    assert "refund failed render project-1" in refunds[0]["reason"]
    stored = await db.render_jobs.find_one({"id": job["id"]})
    assert stored["credit_reversed_refund_id"] == f"refund:{job['id']}"


@pytest.mark.asyncio
async def test_double_reversal_never_double_credits(monkeypatch):
    db = _db(subs=[{"user_id": "user-1", "credits_remaining": 10}])
    job = await _queue(monkeypatch, db)

    first = await render.reverse_render_credits(job["id"], reason="first")
    second = await render.reverse_render_credits(job["id"], reason="duplicate event")
    third = await render.reverse_render_credits(job["id"], reason="retry")

    assert first["reversed"] is True
    assert second["reversed"] is False and second["reason"] == "already_reversed"
    assert third["reversed"] is False
    assert db.subscriptions.docs[0]["credits_remaining"] == 10
    assert len([r for r in db.credit_transactions.docs if r.get("refund_id")]) == 1


@pytest.mark.asyncio
async def test_full_access_failure_never_reverses(monkeypatch):
    db = _db(subs=[{
        "user_id": "user-1", "credits_remaining": 2, "full_access": True, "access_status": "active",
    }])
    job = await _queue(monkeypatch, db)
    # No deduction happened for full-access subscribers.
    assert db.subscriptions.docs[0]["credits_remaining"] == 2
    stored = await db.render_jobs.find_one({"id": job["id"]})
    assert stored["credit_charge"]["amount"] == 0
    assert stored["credit_charge"]["store"] == "none"

    result = await render.reverse_render_credits(job["id"], reason="failed anyway")
    assert result == {"reversed": False, "amount": 0, "user_id": "user-1", "reason": "no_charge_to_reverse"}
    assert not [r for r in db.credit_transactions.docs if r.get("refund_id")]


@pytest.mark.asyncio
async def test_legacy_job_without_charge_stamp_is_noop(monkeypatch):
    db = _db(jobs=[{
        "id": "legacy-1", "project_id": "p-old", "requested_by": "user-1", "status": "failed",
    }])
    monkeypatch.setattr(render, "get_db", lambda: db)
    result = await render.reverse_render_credits("legacy-1", reason="legacy failure")
    assert result["reversed"] is False
    assert not [r for r in db.credit_transactions.docs if r.get("refund_id")]


@pytest.mark.asyncio
async def test_stale_job_recovery_reverses_charge(monkeypatch):
    from datetime import datetime, timedelta, timezone

    old = datetime(2026, 10, 1, tzinfo=timezone.utc)
    db = _db(
        subs=[{"user_id": "user-1", "credits_remaining": 4}],
        jobs=[{
            "id": "stale-1", "project_id": "project-1", "requested_by": "user-1",
            "status": "rendering", "updated_at": old - timedelta(hours=2),
            "credit_charge": {"user_id": "user-1", "amount": 3, "store": "subscriptions", "ledger_id": "l1"},
        }],
    )
    monkeypatch.setattr(render, "get_db", lambda: db)

    recovered = await render.recover_stale_jobs()

    assert recovered == 1
    stored = await db.render_jobs.find_one({"id": "stale-1"})
    assert stored["status"] == "failed"
    assert db.subscriptions.docs[0]["credits_remaining"] == 7
    assert len([r for r in db.credit_transactions.docs if r.get("refund_id")]) == 1


@pytest.mark.asyncio
async def test_known_good_completed_jobs_unaffected(monkeypatch):
    """Jobs 17253296 and 3cd769ec (accepted E2E renders) must never be reversed."""
    known_good = [
        {
            "id": "17253296-aaaa-bbbb-cccc-000000000001",
            "project_id": "known-good-1",
            "status": "completed",
            "duration": 30.07,
            "output_storage_key": "renders/known-good-1/out.mp4",
            "requested_by": "user-1",
            "release_eligible": True,
        },
        {
            "id": "3cd769ec-aaaa-bbbb-cccc-000000000002",
            "project_id": "known-good-2",
            "status": "completed",
            "duration": 35.1,
            "output_storage_key": "renders/known-good-2/out.mp4",
            "requested_by": "user-1",
            "release_eligible": True,
        },
    ]
    db = _db(subs=[{"user_id": "user-1", "credits_remaining": 5}], jobs=known_good)
    monkeypatch.setattr(render, "get_db", lambda: db)

    for job in known_good:
        result = await render.reverse_render_credits(job["id"], reason="must not touch completed jobs")
        assert result["reversed"] is False
        # cancel_render must also refuse (terminal completed) and never reverse
        assert await render.cancel_render(job["project_id"], job["id"]) is False

    assert db.subscriptions.docs[0]["credits_remaining"] == 5
    assert not [r for r in db.credit_transactions.docs if r.get("refund_id")]
    stored = await db.render_jobs.find_one({"id": known_good[0]["id"]})
    assert stored["status"] == "completed"
    assert "credit_reversed_at" not in stored


@pytest.mark.asyncio
async def test_queue_insert_failure_refunds_immediately(monkeypatch):
    db = _db(subs=[{"user_id": "user-1", "credits_remaining": 10}])
    inserted = []

    async def broken_insert(doc):
        inserted.append(doc)
        raise RuntimeError("disk full")

    db.render_jobs.insert_one = broken_insert  # type: ignore[assignment]
    monkeypatch.setattr(render, "get_db", lambda: db)

    with pytest.raises(RuntimeError, match="disk full"):
        await render.queue_render("project-1", requested_by="user-1")

    assert db.subscriptions.docs[0]["credits_remaining"] == 10
    refunds = [r for r in db.credit_transactions.docs if r.get("refund_id")]
    assert len(refunds) == 1
    assert refunds[0]["amount"] == 3
    assert "queue_insert_failed" in refunds[0]["reason"]


@pytest.mark.asyncio
async def test_ff_credits_store_reversed_into_ff_credits(monkeypatch):
    """When the debit hit ff_credits, the reversal must too (not subscriptions)."""
    db = _db(
        subs=[{"user_id": "user-1", "credits_remaining": 1}],  # < 3: forced to ff path
        ffs=[{"tenant_id": "user-1", "credits": 10}],
    )
    job = await _queue(monkeypatch, db)
    assert db.ff_credits.docs[0]["credits"] == 7

    await render.reverse_render_credits(job["id"], reason="failed render")

    assert db.ff_credits.docs[0]["credits"] == 10
    assert db.subscriptions.docs[0]["credits_remaining"] == 1
