"""Strict, declarative bank-CSV adapter. It maps configured columns and never guesses formats."""
from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from sovereign_db import DomainError

try:
    from rapidfuzz import fuzz
except ImportError:
    fuzz = None  # type: ignore

logger = logging.getLogger("facelessforge.bank_adapter")

def fuzzy_reference_score(expected: str, received: str) -> float:
    if not expected or not received:
        return 0.0
    exp = expected.strip().upper()
    rec = received.strip().upper()
    if exp == rec:
        return 100.0
    if fuzz is None:
        return 100.0 if exp == rec else 0.0
    try:
        return max(float(fuzz.ratio(exp, rec)), float(fuzz.partial_ratio(exp, rec)))
    except Exception:
        return float(fuzz.ratio(exp, rec))

def fuzzy_match_reference(expected: str, received: str, threshold: int = 85) -> tuple[bool, float]:
    score = fuzzy_reference_score(expected, received)
    logger.info(f"FUZZY MATCH: expected={expected} got={received} score={score:.1f}")
    return score >= threshold, score

REQUIRED_LOGICAL_FIELDS = ("date", "description", "amount")


class BankCSVAdapter:
    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.encoding = str(settings.get("encoding", "utf-8-sig"))
        self.delimiter = str(settings.get("delimiter", ","))
        self.columns = settings.get("columns", {})
        self.date_formats = settings.get("date_formats", ["%Y-%m-%d"])
        self.amount_kind = settings.get("amount_kind", "credit_only")
        if self.amount_kind not in ("credit_only", "signed_credit_positive"):
            raise DomainError("bank_import.amount_kind must be credit_only or signed_credit_positive.")
        if len(self.delimiter) != 1:
            raise DomainError("bank_import.delimiter must contain exactly one character.")
        missing = [field for field in REQUIRED_LOGICAL_FIELDS if not isinstance(self.columns.get(field), str) or not self.columns[field].strip()]
        if missing:
            raise DomainError(f"bank_import.columns must define: {', '.join(missing)}.")
        if not isinstance(self.date_formats, list) or not self.date_formats:
            raise DomainError("bank_import.date_formats must be a non-empty list.")

    @staticmethod
    def _headers(fieldnames: list[str | None] | None) -> dict[str, str]:
        return {str(value or "").strip().casefold(): str(value or "").strip() for value in (fieldnames or [])}

    def _physical_columns(self, fieldnames: list[str | None] | None) -> dict[str, str]:
        headers = self._headers(fieldnames)
        mapped: dict[str, str] = {}
        for logical, configured in self.columns.items():
            physical = headers.get(configured.strip().casefold())
            if not physical:
                raise DomainError(f"Bank CSV is missing configured column {configured!r} for {logical}.")
            mapped[logical] = physical
        return mapped

    def _normalise_date(self, value: str, row_number: int) -> str:
        stripped = value.strip()
        for pattern in self.date_formats:
            try:
                return datetime.strptime(stripped, pattern).date().isoformat()
            except ValueError:
                continue
        raise DomainError(f"Row {row_number}: date {value!r} did not match the configured date formats.")

    def _normalise_amount(self, value: str, row_number: int) -> str:
        stripped = value.strip()
        if not stripped:
            return "0.00"
        candidate = stripped.replace("$", "").replace(",", "")
        if self.amount_kind == "credit_only":
            return candidate
        if self.amount_kind == "signed_credit_positive":
            return candidate
        raise DomainError(f"Row {row_number}: unsupported amount policy.")

    def rows(self, path: Path) -> Iterator[tuple[int, dict[str, str]]]:
        try:
            handle = path.open("r", encoding=self.encoding, newline="")
        except UnicodeError as exc:
            raise DomainError(f"Cannot decode {path.name} using configured encoding {self.encoding!r}.") from exc
        with handle:
            reader = csv.DictReader(handle, delimiter=self.delimiter)
            mapped = self._physical_columns(reader.fieldnames)
            for row_number, raw in enumerate(reader, start=2):
                if raw is None:
                    continue
                date_value = str(raw.get(mapped["date"], "") or "")
                description = str(raw.get(mapped["description"], "") or "").strip()
                amount = self._normalise_amount(str(raw.get(mapped["amount"], "") or ""), row_number)
                if not description:
                    raise DomainError(f"Row {row_number}: description is empty.")
                yield row_number, {"date": self._normalise_date(date_value, row_number), "description": description, "amount": amount}


def adapter_from_config(config: dict[str, Any]) -> BankCSVAdapter:
    settings = config.get("bank_import")
    if not isinstance(settings, dict):
        raise DomainError("Missing bank_import configuration. Configure a bank-specific adapter before processing CSV files.")
    return BankCSVAdapter(settings)
