"""Local, dependency-free state and controls for the ETHINX sovereign revenue loop."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
from calendar import monthrange
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

try:
    from rapidfuzz import fuzz as _fuzz
except ImportError:
    _fuzz = None  # type: ignore

logger = logging.getLogger("sovereign.fuzzy")

# Fuzzy thresholds per spec
FUZZY_AUTO_APPROVE = 85
FUZZY_REVIEW_MIN = 75  # 75-84 => REVIEW_REQUIRED


def _fuzzy_score(expected: str, received: str) -> int:
    """Score expected reference against received description/reference using rapidfuzz.ratio."""
    if _fuzz is None:
        # fallback to exact
        return 100 if expected.strip().upper() == received.strip().upper() else 0
    exp = expected.strip().upper()
    rec = received.strip().upper()
    # direct ratio if short
    if len(rec) < 30:
        return int(_fuzz.ratio(exp, rec))
    # Tokenize description into plausible reference chunks plus whole string
    candidates = re.findall(r"[A-Z0-9\-]{4,}", rec)
    best = int(_fuzz.ratio(exp, rec))
    for cand in candidates:
        s = int(_fuzz.ratio(exp, cand))
        if s > best:
            best = s
    # also try whole rec via partial for embedded refs, but spec says fuzz.ratio
    return best


def fuzzy_reference_match(expected: str, received: str) -> tuple[str, int]:
    """Return (decision, score) where decision in auto_approve/review_required/reject."""
    score = _fuzzy_score(expected, received)
    logger.info("FUZZY MATCH: expected=%s got=%s score=%s", expected, received, score)
    # also print for audit per spec
    print(f"FUZZY MATCH: expected={expected} got={received} score={score}", flush=True)
    if score >= FUZZY_AUTO_APPROVE:
        return "auto_approve", score
    if score >= FUZZY_REVIEW_MIN:
        return "review_required", score
    return "reject", score


class DomainError(ValueError):
    """A caller-visible business-rule failure."""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def reference(prefix: str) -> str:
    """Return a bank-safe, low-collision reference (for example ETHX-AB12CD34)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    token = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{prefix.upper()}-{token}"


def parse_money_cents(amount_raw: str) -> int:
    normalised = amount_raw.strip().replace("$", "").replace(",", "")
    try:
        amount = Decimal(normalised)
    except InvalidOperation as exc:
        raise DomainError(f"Invalid amount: {amount_raw!r}") from exc
    if amount.as_tuple().exponent < -2:
        raise DomainError("Amount must have no more than two decimal places.")
    return int(amount * 100)


def parse_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


class SovereignStore:
    """SQLite-backed local records. All financially ambiguous records require review."""

    def __init__(self, database_path: str | Path, config: dict[str, Any]):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.initialise()

    @contextmanager
    def connection(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path, timeout=20, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialise(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    full_name TEXT NOT NULL,
                    company_name TEXT NOT NULL DEFAULT '',
                    plan_code TEXT NOT NULL CHECK(plan_code IN ('individual', 'agency')),
                    payment_reference TEXT NOT NULL UNIQUE,
                    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                    status TEXT NOT NULL DEFAULT 'awaiting_payment'
                      CHECK(status IN ('awaiting_payment', 'active', 'past_due', 'cancelled')),
                    access_state TEXT NOT NULL DEFAULT 'pending'
                      CHECK(access_state IN ('pending', 'payment_received', 'active', 'renewal_due', 'expired', 'cancelled', 'review_required', 'refunded')),
                    paid_through TEXT,
                    created_at TEXT NOT NULL,
                    activated_at TEXT,
                    cancelled_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_application_access_state ON applications(access_state);
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id INTEGER NOT NULL REFERENCES applications(id),
                    payment_reference TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                    bank_date TEXT NOT NULL,
                    bank_description TEXT NOT NULL,
                    import_fingerprint TEXT NOT NULL UNIQUE,
                    imported_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_payments_application ON payments(application_id);
                CREATE TABLE IF NOT EXISTS bank_import_rows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_file TEXT NOT NULL,
                    row_number INTEGER NOT NULL,
                    bank_date TEXT NOT NULL,
                    bank_description TEXT NOT NULL,
                    amount_cents INTEGER,
                    raw_json TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN ('matched', 'duplicate', 'unmatched', 'ambiguous', 'underpaid', 'overpaid', 'ignored', 'invalid')),
                    reason TEXT NOT NULL,
                    application_id INTEGER REFERENCES applications(id),
                    imported_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_bank_import_rows_fingerprint ON bank_import_rows(fingerprint);
                CREATE TABLE IF NOT EXISTS review_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_type TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'resolved', 'dismissed')),
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolution_note TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_review_queue_status ON review_queue(status, created_at);
                CREATE TABLE IF NOT EXISTS operator_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'acknowledged')),
                    created_at TEXT NOT NULL,
                    acknowledged_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_operator_notifications_status ON operator_notifications(status, created_at);
                CREATE TABLE IF NOT EXISTS content_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_date TEXT NOT NULL UNIQUE,
                    post_type TEXT NOT NULL,
                    linkedin_text TEXT NOT NULL,
                    x_text TEXT NOT NULL,
                    content_file TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft_ready'
                      CHECK(status IN ('draft_ready', 'published_external', 'rejected')),
                    external_reference TEXT
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                """
            )
            existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(applications)")}
            if "access_state" not in existing_columns:
                conn.execute("ALTER TABLE applications ADD COLUMN access_state TEXT NOT NULL DEFAULT 'pending'")
                conn.execute("""UPDATE applications SET access_state = CASE status
                    WHEN 'active' THEN 'active' WHEN 'past_due' THEN 'renewal_due'
                    WHEN 'cancelled' THEN 'cancelled' ELSE 'pending' END""")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_application_access_state ON applications(access_state)")

    def audit(self, conn: sqlite3.Connection, event_type: str, entity_type: str,
              entity_id: str | int, payload: dict[str, Any]) -> None:
        conn.execute(
            """INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, occurred_at)
               VALUES (?, ?, ?, ?, ?)""",
            (event_type, entity_type, str(entity_id), json.dumps(payload, sort_keys=True), utc_now()),
        )

    def queue_review(self, conn: sqlite3.Connection, queue_type: str, entity_type: str,
                     entity_id: str | int, dedupe_key: str, payload: dict[str, Any]) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO review_queue(queue_type, entity_type, entity_id, dedupe_key, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (queue_type, entity_type, str(entity_id), dedupe_key, json.dumps(payload, sort_keys=True), utc_now()),
        )

    def notify(self, conn: sqlite3.Connection, event_type: str, entity_type: str,
               entity_id: str | int, dedupe_key: str, payload: dict[str, Any]) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO operator_notifications(event_type, entity_type, entity_id, dedupe_key, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_type, entity_type, str(entity_id), dedupe_key, json.dumps(payload, sort_keys=True), utc_now()),
        )

    def plan(self, plan_code: str) -> dict[str, Any]:
        plan = self.config.get("plans", {}).get(plan_code)
        if not plan:
            raise DomainError("Unknown plan.")
        return plan

    def create_application(self, full_name: str, email: str, company_name: str,
                           plan_code: str) -> dict[str, Any]:
        full_name, email, company_name = full_name.strip(), email.strip().lower(), company_name.strip()
        if not full_name or len(full_name) > 120:
            raise DomainError("A valid full name is required.")
        if not email or "@" not in email or len(email) > 254:
            raise DomainError("A valid email address is required.")
        if len(company_name) > 160:
            raise DomainError("Company name is too long.")
        plan = self.plan(plan_code)
        prefix = self.config["bank_transfer"]["payment_reference_prefix"]
        with self.connection(immediate=True) as conn:
            current = conn.execute(
                """SELECT COUNT(*) AS count FROM applications
                   WHERE plan_code = ? AND access_state IN ('pending', 'payment_received', 'active', 'renewal_due', 'review_required')""",
                (plan_code,),
            ).fetchone()["count"]
            if current >= int(plan["seats"]):
                raise DomainError("This founding plan is fully allocated.")
            existing = conn.execute("SELECT * FROM applications WHERE email = ?", (email,)).fetchone()
            if existing:
                if existing["access_state"] == "pending":
                    return dict(existing)
                raise DomainError("This email already has an existing membership record.")
            now = utc_now()
            cursor = conn.execute(
                """INSERT INTO applications(email, full_name, company_name, plan_code, payment_reference,
                   amount_cents, status, access_state, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'awaiting_payment', 'pending', ?)""",
                (email, full_name, company_name, plan_code, reference(prefix), int(plan["amount_cents"]), now),
            )
            application_id = cursor.lastrowid
            row = conn.execute("SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()
            application = dict(row)
            self.audit(conn, "application_created", "application", application_id, {
                "plan_code": plan_code, "email": email, "payment_reference": application["payment_reference"],
            })
            self.notify(conn, "payment_instructions_ready", "application", application_id,
                        f"payment_instructions:{application_id}", {"email": email, "payment_reference": application["payment_reference"]})
            return application

    @staticmethod
    def fingerprint(row: dict[str, str], amount_cents: int) -> str:
        canonical = "|".join([row["date"].strip(), row["description"].strip(), str(amount_cents)])
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def candidate_applications(self, conn: sqlite3.Connection, description: str) -> list[sqlite3.Row]:
        try:
            from rapidfuzz import fuzz
        except ImportError:
            fuzz = None  # type: ignore
        import logging as _logging
        _logger = _logging.getLogger("facelessforge.revenue_loop")
        rows = conn.execute(
            """SELECT * FROM applications
               WHERE access_state NOT IN ('cancelled', 'refunded')
               ORDER BY id ASC"""
        ).fetchall()
        normalised = description.upper()
        # First try exact substring (backwards compat)
        exact = [row for row in rows if row["payment_reference"].upper() in normalised]
        if exact:
            for r in exact:
                _logger.info(f"FUZZY MATCH: expected={r['payment_reference']} got={description} score=100.0")
            return exact
        # Fuzzy fallback
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            ref = row["payment_reference"].strip().upper()
            if not ref:
                continue
            if fuzz is None:
                score = 100.0 if ref in normalised else 0.0
            else:
                try:
                    score = max(float(fuzz.ratio(ref, normalised)), float(fuzz.partial_ratio(ref, normalised)))
                except Exception:
                    score = float(fuzz.ratio(ref, normalised))
            _logger.info(f"FUZZY MATCH: expected={ref} got={description} score={score:.1f}")
            if score >= 75:
                scored.append((score, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        # Return all >=75 sorted; caller will decide 85 vs 75-84
        return [r for s, r in scored]

    def record_bank_row(self, conn: sqlite3.Connection, *, source_file: str, row_number: int,
                        row: dict[str, str], amount_cents: int | None, fingerprint: str,
                        outcome: str, reason: str, application_id: int | None) -> None:
        conn.execute(
            """INSERT INTO bank_import_rows(source_file, row_number, bank_date, bank_description, amount_cents,
               raw_json, fingerprint, outcome, reason, application_id, imported_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source_file, row_number, row.get("date", "").strip(), row.get("description", "").strip(), amount_cents,
             json.dumps(row, sort_keys=True), fingerprint, outcome, reason, application_id, utc_now()),
        )

    def import_payment(self, row: dict[str, str], source_file: str = "manual", row_number: int = 0) -> dict[str, Any]:
        required = ("date", "description", "amount")
        if any(not row.get(key, "").strip() for key in required):
            raise DomainError("Bank row must contain date, description, and amount.")
        description = row["description"].strip()
        amount_cents = parse_money_cents(row["amount"])
        fingerprint = self.fingerprint(row, amount_cents)
        with self.connection(immediate=True) as conn:
            already_matched = conn.execute(
                "SELECT 1 FROM payments WHERE import_fingerprint = ?", (fingerprint,)
            ).fetchone()
            if already_matched:
                self.record_bank_row(conn, source_file=source_file, row_number=row_number, row=row,
                                     amount_cents=amount_cents, fingerprint=fingerprint, outcome="duplicate",
                                     reason="already_matched", application_id=None)
                return {"outcome": "duplicate", "reason": "already_matched", "description": description}
            if amount_cents <= 0:
                self.record_bank_row(conn, source_file=source_file, row_number=row_number, row=row,
                                     amount_cents=amount_cents, fingerprint=fingerprint, outcome="ignored",
                                     reason="non_credit", application_id=None)
                return {"outcome": "ignored", "reason": "non_credit", "description": description}
            candidates = self.candidate_applications(conn, description)
            # Apply fuzzy threshold logic: filter by score already >=75 inside candidate_applications
            # Re-score to classify 85+ vs 75-84
            try:
                from rapidfuzz import fuzz as _fuzz
            except ImportError:
                _fuzz = None
            import logging as _logging2
            _logger2 = _logging2.getLogger("facelessforge.revenue_loop")
            def _score(ref, desc):
                if not ref or not desc:
                    return 0.0
                ru = ref.strip().upper()
                du = desc.strip().upper()
                if ru == du or ru in du:
                    return 100.0
                if _fuzz is None:
                    return 0.0
                try:
                    return max(float(_fuzz.ratio(ru, du)), float(_fuzz.partial_ratio(ru, du)))
                except Exception:
                    return float(_fuzz.ratio(ru, du))
            if len(candidates) == 0:
                self.record_bank_row(conn, source_file=source_file, row_number=row_number, row=row,
                                     amount_cents=amount_cents, fingerprint=fingerprint, outcome="unmatched",
                                     reason="reference_not_found", application_id=None)
                self.queue_review(conn, "payment_exception", "bank_row", fingerprint, f"bank:{fingerprint}", {
                    "reason": "reference_not_found", "description": description, "amount_cents": amount_cents,
                })
                return {"outcome": "unmatched", "reason": "reference_not_found", "description": description}
            # Count how many are auto-approve (>=85) vs review (75-84)
            auto_candidates = []
            review_candidates = []
            for cand in candidates:
                s = _score(cand["payment_reference"], description)
                if s >= 85:
                    auto_candidates.append(cand)
                elif s >= 75:
                    review_candidates.append(cand)
            # If multiple auto-approve candidates, ambiguous
            if len(auto_candidates) > 1:
                candidate_ids = [row["id"] for row in auto_candidates]
                self.record_bank_row(conn, source_file=source_file, row_number=row_number, row=row,
                                     amount_cents=amount_cents, fingerprint=fingerprint, outcome="ambiguous",
                                     reason="multiple_references_found", application_id=None)
                self.queue_review(conn, "payment_exception", "bank_row", fingerprint, f"bank:{fingerprint}", {
                    "reason": "multiple_references_found", "candidate_application_ids": candidate_ids,
                    "description": description, "amount_cents": amount_cents,
                })
                return {"outcome": "ambiguous", "reason": "multiple_references_found", "description": description}
            if len(auto_candidates) == 1 and len(review_candidates) == 0:
                # single auto-approve – narrow to that candidate for amount check
                candidates = auto_candidates
            elif len(auto_candidates) == 0 and len(review_candidates) >= 1:
                # 75-84 range -> REVIEW_REQUIRED, not auto-approve
                cand = review_candidates[0] if len(review_candidates)==1 else candidates[0]
                s = _score(cand["payment_reference"], description)
                _logger2.info(f"FUZZY REVIEW: expected={cand['payment_reference']} got={description} score={s:.1f}")
                self.record_bank_row(conn, source_file=source_file, row_number=row_number, row=row,
                                     amount_cents=amount_cents, fingerprint=fingerprint, outcome="unmatched",
                                     reason="fuzzy_review_required", application_id=cand["id"])
                conn.execute("UPDATE applications SET access_state = 'review_required' WHERE id = ? AND access_state = 'pending'", (cand["id"],))
                self.queue_review(conn, "payment_exception", "application", cand["id"], f"bank:{fingerprint}", {
                    "reason": "fuzzy_review_required", "score": s, "payment_reference": cand["payment_reference"], "description": description,
                })
                return {"outcome": "unmatched", "reason": "review_required", "score": s, "description": description, "payment_reference": cand["payment_reference"]}
            elif len(auto_candidates) == 0 and len(candidates) > 1:
                # multiple review-range -> ambiguous
                candidate_ids = [row["id"] for row in review_candidates or candidates]
                self.record_bank_row(conn, source_file=source_file, row_number=row_number, row=row,
                                     amount_cents=amount_cents, fingerprint=fingerprint, outcome="ambiguous",
                                     reason="multiple_references_found", application_id=None)
                self.queue_review(conn, "payment_exception", "bank_row", fingerprint, f"bank:{fingerprint}", {
                    "reason": "multiple_references_found", "candidate_application_ids": candidate_ids,
                    "description": description, "amount_cents": amount_cents,
                })
                return {"outcome": "ambiguous", "reason": "multiple_references_found", "description": description}
            if len(candidates) > 1:
                candidate_ids = [row["id"] for row in candidates]
                self.record_bank_row(conn, source_file=source_file, row_number=row_number, row=row,
                                     amount_cents=amount_cents, fingerprint=fingerprint, outcome="ambiguous",
                                     reason="multiple_references_found", application_id=None)
                self.queue_review(conn, "payment_exception", "bank_row", fingerprint, f"bank:{fingerprint}", {
                    "reason": "multiple_references_found", "candidate_application_ids": candidate_ids,
                    "description": description, "amount_cents": amount_cents,
                })
                return {"outcome": "ambiguous", "reason": "multiple_references_found", "description": description}
            app = candidates[0]
            if amount_cents != app["amount_cents"]:
                outcome = "underpaid" if amount_cents < app["amount_cents"] else "overpaid"
                self.record_bank_row(conn, source_file=source_file, row_number=row_number, row=row,
                                     amount_cents=amount_cents, fingerprint=fingerprint, outcome=outcome,
                                     reason="amount_mismatch", application_id=app["id"])
                self.queue_review(conn, "payment_exception", "application", app["id"], f"bank:{fingerprint}", {
                    "reason": "amount_mismatch", "outcome": outcome, "payment_reference": app["payment_reference"],
                    "expected_cents": app["amount_cents"], "received_cents": amount_cents, "bank_date": row["date"].strip(),
                })
                conn.execute("UPDATE applications SET access_state = 'review_required' WHERE id = ? AND access_state = 'pending'", (app["id"],))
                return {"outcome": outcome, "reason": "amount_mismatch", "description": description,
                        "payment_reference": app["payment_reference"], "expected_cents": app["amount_cents"], "received_cents": amount_cents}
            now = utc_now()
            paid_through = self.next_paid_through(app["paid_through"])
            conn.execute(
                """INSERT INTO payments(application_id, payment_reference, amount_cents, bank_date, bank_description,
                   import_fingerprint, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (app["id"], app["payment_reference"], amount_cents, row["date"].strip(), description, fingerprint, now),
            )
            self.record_bank_row(conn, source_file=source_file, row_number=row_number, row=row,
                                 amount_cents=amount_cents, fingerprint=fingerprint, outcome="matched",
                                 reason="exact_reference_and_amount_match", application_id=app["id"])
            conn.execute(
                """UPDATE applications SET status = 'active', access_state = 'active',
                   activated_at = COALESCE(activated_at, ?), paid_through = ? WHERE id = ?""",
                (now, paid_through, app["id"]),
            )
            self.audit(conn, "payment_matched", "application", app["id"], {
                "amount_cents": amount_cents, "bank_date": row["date"].strip(), "payment_reference": app["payment_reference"],
                "paid_through": paid_through,
            })
            self.notify(conn, "payment_matched", "application", app["id"], f"payment_matched:{fingerprint}", {
                "email": app["email"], "payment_reference": app["payment_reference"], "amount_cents": amount_cents,
                "paid_through": paid_through,
            })
            activated = conn.execute("SELECT * FROM applications WHERE id = ?", (app["id"],)).fetchone()
            return {"outcome": "matched", "application": dict(activated)}

    @staticmethod
    def next_paid_through(current_paid_through: str | None, as_of: datetime | None = None) -> str:
        base = as_of or datetime.now(UTC)
        if base.tzinfo is None:
            base = base.replace(tzinfo=UTC)
        if current_paid_through:
            prior = parse_timestamp(current_paid_through)
            if prior > base:
                base = prior
        year = base.year + (1 if base.month == 12 else 0)
        month = 1 if base.month == 12 else base.month + 1
        day = min(base.day, monthrange(year, month)[1])
        return base.replace(year=year, month=month, day=day, microsecond=0).isoformat()

    def evaluate_access(self, as_of: datetime | None = None) -> list[dict[str, Any]]:
        """Move only unambiguous, time-based access states; never infer a payment."""
        now = as_of or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        renewal_days = int(self.config.get("billing", {}).get("renewal_notice_days", 5))
        changes: list[dict[str, Any]] = []
        with self.connection(immediate=True) as conn:
            rows = conn.execute(
                """SELECT * FROM applications WHERE access_state IN ('active', 'renewal_due')
                   AND paid_through IS NOT NULL ORDER BY id ASC"""
            ).fetchall()
            for app in rows:
                paid_through = parse_timestamp(app["paid_through"])
                if paid_through <= now:
                    new_state, legacy_status, event = "expired", "past_due", "access_expired"
                elif paid_through <= now + timedelta(days=renewal_days) and app["access_state"] == "active":
                    new_state, legacy_status, event = "renewal_due", "past_due", "renewal_due"
                else:
                    continue
                conn.execute("UPDATE applications SET access_state = ?, status = ? WHERE id = ?", (new_state, legacy_status, app["id"]))
                payload = {"email": app["email"], "payment_reference": app["payment_reference"], "paid_through": app["paid_through"], "access_state": new_state}
                self.audit(conn, event, "application", app["id"], payload)
                self.notify(conn, event, "application", app["id"], f"{event}:{app['id']}:{app['paid_through'][:10]}", payload)
                changes.append({"application_id": app["id"], "event": event, "access_state": new_state})
        return changes

    def seat_availability(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        with self.connection() as conn:
            for code, plan in self.config.get("plans", {}).items():
                allocated = conn.execute(
                    """SELECT COUNT(*) AS count FROM applications WHERE plan_code = ?
                       AND access_state IN ('pending', 'payment_received', 'active', 'renewal_due', 'review_required')""",
                    (code,),
                ).fetchone()["count"]
                result[code] = {"capacity": int(plan["seats"]), "allocated": allocated, "remaining": max(0, int(plan["seats"]) - allocated)}
        return result

    def list_operator_notifications(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT id, event_type, entity_type, entity_id, payload_json, status, created_at, acknowledged_at
                   FROM operator_notifications ORDER BY id DESC LIMIT ?""", (limit,)
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def list_review_queue(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT id, queue_type, entity_type, entity_id, payload_json, status, created_at, resolved_at, resolution_note
                   FROM review_queue ORDER BY id DESC LIMIT ?""", (limit,)
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def acknowledge_notification(self, notification_id: int) -> None:
        with self.connection(immediate=True) as conn:
            updated = conn.execute(
                """UPDATE operator_notifications SET status = 'acknowledged', acknowledged_at = ?
                   WHERE id = ? AND status = 'pending'""", (utc_now(), notification_id)
            ).rowcount
            if updated != 1:
                raise DomainError("Notification not found or already acknowledged.")
            self.audit(conn, "operator_notification_acknowledged", "operator_notification", notification_id, {})

    def resolve_review_item(self, item_id: int, resolution_note: str) -> None:
        note = resolution_note.strip()
        if not note or len(note) > 1000:
            raise DomainError("A resolution note of 1 to 1000 characters is required.")
        with self.connection(immediate=True) as conn:
            updated = conn.execute(
                """UPDATE review_queue SET status = 'resolved', resolved_at = ?, resolution_note = ?
                   WHERE id = ? AND status = 'open'""", (utc_now(), note, item_id)
            ).rowcount
            if updated != 1:
                raise DomainError("Review item not found or already closed.")
            self.audit(conn, "review_item_resolved", "review_queue", item_id, {"resolution_note": note})

    def list_content(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT content_date, post_type, linkedin_text, x_text, content_file, created_at, status,
                   external_reference FROM content_posts ORDER BY content_date DESC LIMIT ?""", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def save_content(self, content_date: str, post_type: str, linkedin_text: str, x_text: str, content_file: str) -> int:
        with self.connection(immediate=True) as conn:
            cursor = conn.execute(
                """INSERT INTO content_posts(content_date, post_type, linkedin_text, x_text, content_file, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""", (content_date, post_type, linkedin_text, x_text, content_file, utc_now()),
            )
            content_id = cursor.lastrowid
            self.audit(conn, "content_generated", "content_post", content_id, {"content_date": content_date, "post_type": post_type, "content_file": content_file})
            return content_id

    def mark_content_published(self, content_date: str, external_reference: str) -> None:
        if not external_reference.strip():
            raise DomainError("An external post reference is required.")
        with self.connection(immediate=True) as conn:
            updated = conn.execute(
                """UPDATE content_posts SET status = 'published_external', external_reference = ?
                   WHERE content_date = ? AND status = 'draft_ready'""", (external_reference.strip(), content_date),
            ).rowcount
            if updated != 1:
                raise DomainError("Draft not found or cannot be marked published.")
            self.audit(conn, "external_publication_recorded", "content_post", content_date, {"external_reference": external_reference.strip()})

    def export_summary(self) -> dict[str, Any]:
        with self.connection() as conn:
            applications = conn.execute(
                """SELECT id, email, full_name, company_name, plan_code, payment_reference, amount_cents, status,
                   access_state, paid_through, created_at, activated_at FROM applications ORDER BY id ASC"""
            ).fetchall()
            payments = conn.execute(
                """SELECT p.id, a.email, p.payment_reference, p.amount_cents, p.bank_date, p.imported_at
                   FROM payments p JOIN applications a ON a.id = p.application_id ORDER BY p.id ASC"""
            ).fetchall()
        return {"applications": [dict(row) for row in applications], "payments": [dict(row) for row in payments]}

    def retention_review(self) -> dict[str, Any]:
        privacy = self.config.get("privacy", {})
        with self.connection() as conn:
            oldest = conn.execute("SELECT MIN(created_at) AS oldest_application FROM applications").fetchone()["oldest_application"]
            imports = conn.execute("SELECT MIN(imported_at) AS oldest_import FROM bank_import_rows").fetchone()["oldest_import"]
        return {
            "status": "review_required",
            "automatic_deletion": False,
            "application_retention_days": privacy.get("application_retention_days", "UNSET"),
            "bank_record_retention_days": privacy.get("bank_record_retention_days", "UNSET"),
            "oldest_application": oldest,
            "oldest_bank_import": imports,
            "instruction": "Set business-approved retention values and review before any deletion process is implemented.",
        }
