"""In-memory stand-in for the Motor collections used by the asset/render paths.

Unlike the ad-hoc fakes elsewhere, this one honours filters: compare-and-set
updates, ``$or``/``$in``/``$lt``/``$gte`` predicates, ``matched_count`` and
unique keys (``_id`` always, plus any declared unique index). Behavioural tests
rely on it to observe real conflicts, duplicate-key failures and fenced writes.
"""
from __future__ import annotations

import copy
import inspect
import itertools
from types import SimpleNamespace
from typing import Callable, Iterable, Optional

_IDS = itertools.count(1)


class DuplicateKeyError(Exception):
    """Mirrors pymongo.errors.DuplicateKeyError (code 11000)."""

    code = 11000


class FailingWrite(Exception):
    """Injected storage/database failure (not a duplicate)."""


def _get(doc: dict, dotted: str):
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _has(doc: dict, dotted: str) -> bool:
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def _cond_matches(value, has_value: bool, cond) -> bool:
    is_operator = isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond)
    if isinstance(value, list) and not is_operator:
        # MongoDB equality on an array field matches when any element equals the value.
        return cond in value
    if is_operator:
        for op, arg in cond.items():
            if op == "$in" and value not in arg:
                return False
            if op == "$nin" and value in arg:
                return False
            if op == "$ne":
                # On an array field, $ne means no element equals the value.
                if (arg in value) if isinstance(value, list) else (value == arg):
                    return False
            if op == "$exists" and bool(arg) != has_value:
                return False
            if op in ("$lt", "$lte", "$gt", "$gte"):
                if value is None:
                    return False
                try:
                    ok = {
                        "$lt": value < arg, "$lte": value <= arg,
                        "$gt": value > arg, "$gte": value >= arg,
                    }[op]
                except TypeError:
                    return False
                if not ok:
                    return False
            if op == "$eq" and value != arg:
                return False
        return True
    # Plain equality: None matches a missing field, as in MongoDB.
    return (value if has_value else None) == cond


def matches(doc: dict, query: Optional[dict]) -> bool:
    for key, cond in (query or {}).items():
        if key == "$or":
            if not any(matches(doc, sub) for sub in cond):
                return False
            continue
        if key == "$and":
            if not all(matches(doc, sub) for sub in cond):
                return False
            continue
        if not _cond_matches(_get(doc, key), _has(doc, key), cond):
            return False
    return True


def _project(doc: dict, projection: Optional[dict]) -> dict:
    out = copy.deepcopy(doc)
    if not projection:
        return out
    include = {k for k, v in projection.items() if v and k != "_id"}
    if include:
        keep = set(include)
        if projection.get("_id", 1):
            keep.add("_id")
        return {k: v for k, v in out.items() if k in keep}
    if not projection.get("_id", 1):
        out.pop("_id", None)
    return out


def _apply_update(doc: dict, update: dict, *, inserting: bool = False) -> None:
    for key, value in (update.get("$set") or {}).items():
        doc[key] = copy.deepcopy(value)
    for key in (update.get("$unset") or {}):
        doc.pop(key, None)
    for key, delta in (update.get("$inc") or {}).items():
        doc[key] = (doc.get(key) or 0) + delta
    if inserting:
        for key, value in (update.get("$setOnInsert") or {}).items():
            doc[key] = copy.deepcopy(value)
    for key, spec in (update.get("$push") or {}).items():
        items = list(spec.get("$each", [spec])) if isinstance(spec, dict) else [spec]
        arr = list(doc.get(key) or []) + items
        if isinstance(spec, dict) and "$slice" in spec:
            arr = arr[spec["$slice"]:] if spec["$slice"] < 0 else arr[: spec["$slice"]]
        doc[key] = arr
    for key, value in (update.get("$pull") or {}).items():
        doc[key] = [item for item in (doc.get(key) or []) if item != value]
    for key, value in (update.get("$addToSet") or {}).items():
        arr = list(doc.get(key) or [])
        if value not in arr:
            arr.append(value)
        doc[key] = arr


def _result(**kw):
    return SimpleNamespace(**{"matched_count": 0, "modified_count": 0, "upserted_id": None, "deleted_count": 0, **kw})


class FakeCursor:
    def __init__(self, docs: list[dict]):
        self._docs = docs

    def sort(self, key, direction=1):
        if isinstance(key, list):
            field, direction = key[0]
        else:
            field = key
        self._docs = sorted(self._docs, key=lambda d: (d.get(field) is None, d.get(field)), reverse=direction < 0)
        return self

    async def to_list(self, n=None):
        docs = self._docs if n is None else self._docs[:n]
        return [copy.deepcopy(d) for d in docs]


class FakeCollection:
    def __init__(self, name: str, docs: Optional[Iterable[dict]] = None, unique: Iterable[tuple] = ()):
        self.name = name
        self.docs: list[dict] = [copy.deepcopy(d) for d in (docs or [])]
        for doc in self.docs:
            doc.setdefault("_id", f"{name}-{next(_IDS)}")
        # Unique indexes as (fields, partial-filter predicate or None) pairs.
        self._unique: list[tuple[tuple, Optional[Callable[[dict], bool]]]] = [
            (tuple(fields), pred) for fields, pred in unique
        ]
        self.fail_next_insert_with: Optional[BaseException] = None
        self.fail_inserts_where: Optional[Callable[[dict], bool]] = None
        self.fail_updates_where: Optional[Callable[[dict, dict], bool]] = None
        # Hooks may be plain callables or coroutines; they run inside the write call,
        # which is how the tests interleave a second operation at an exact point.
        self.on_insert: Optional[Callable[[dict], object]] = None
        self.on_update: Optional[Callable[[dict, dict], object]] = None
        self.delete_calls: list[dict] = []
        self.insert_calls: list[dict] = []
        self.inc_calls: list[dict] = []

    # ---- uniqueness -------------------------------------------------------
    def _check_unique(self, doc: dict, ignore_index: Optional[int] = None) -> None:
        if any(d.get("_id") == doc.get("_id") for i, d in enumerate(self.docs) if i != ignore_index):
            raise DuplicateKeyError(f"E11000 duplicate _id in {self.name}")
        for fields, pred in self._unique:
            if pred is not None and not pred(doc):
                continue
            key = tuple(_get(doc, f) for f in fields)
            for i, other in enumerate(self.docs):
                if i == ignore_index:
                    continue
                if pred is not None and not pred(other):
                    continue
                if tuple(_get(other, f) for f in fields) == key:
                    raise DuplicateKeyError(f"E11000 duplicate key {fields}={key} in {self.name}")

    # ---- reads ------------------------------------------------------------
    def _matching(self, query):
        return [d for d in self.docs if matches(d, query)]

    async def find_one(self, query=None, projection=None, **_kw):
        for doc in self._matching(query or {}):
            return _project(doc, projection)
        return None

    def find(self, query=None, projection=None, **_kw):
        return FakeCursor([_project(d, projection) for d in self._matching(query or {})])

    async def count_documents(self, query=None, **_kw):
        return len(self._matching(query or {}))

    # ---- writes -----------------------------------------------------------
    async def _maybe_fail_insert(self, doc: dict) -> None:
        if self.fail_next_insert_with is not None:
            exc, self.fail_next_insert_with = self.fail_next_insert_with, None
            raise exc
        if self.fail_inserts_where is not None and self.fail_inserts_where(doc):
            raise FailingWrite(f"injected storage failure inserting into {self.name}")

    async def insert_one(self, doc: dict, **_kw):
        doc = copy.deepcopy(doc)
        doc.setdefault("_id", f"{self.name}-{next(_IDS)}")
        self.insert_calls.append(copy.deepcopy(doc))
        if self.on_insert is not None:
            hook_result = self.on_insert(doc)
            if inspect.isawaitable(hook_result):
                await hook_result
        await self._maybe_fail_insert(doc)
        self._check_unique(doc)
        self.docs.append(doc)
        return _result(inserted_id=doc["_id"])

    async def update_one(self, query, update, upsert=False, **_kw):
        if self.on_update is not None:
            hook_result = self.on_update(query, update)
            if inspect.isawaitable(hook_result):
                await hook_result
        if self.fail_updates_where is not None and self.fail_updates_where(query, update):
            raise FailingWrite(f"injected storage failure updating {self.name}")
        for doc in self.docs:
            if matches(doc, query):
                before = copy.deepcopy(doc)
                idx = self.docs.index(doc)
                trial = copy.deepcopy(doc)
                _apply_update(trial, update)
                self._check_unique(trial, ignore_index=idx)
                doc.clear()
                doc.update(trial)
                return _result(matched_count=1, modified_count=int(before != doc))
        if upsert:
            seed = {k: v for k, v in (query or {}).items() if not k.startswith("$") and not isinstance(v, dict)}
            new_doc = dict(seed)
            _apply_update(new_doc, update, inserting=True)
            new_doc.setdefault("_id", f"{self.name}-{next(_IDS)}")
            self._check_unique(new_doc)
            self.docs.append(new_doc)
            return _result(upserted_id=new_doc["_id"])
        return _result()

    async def find_one_and_update(self, query, update, return_document=None, **_kw):
        for doc in self.docs:
            if matches(doc, query):
                idx = self.docs.index(doc)
                trial = copy.deepcopy(doc)
                _apply_update(trial, update)
                self._check_unique(trial, ignore_index=idx)
                doc.clear()
                doc.update(trial)
                return copy.deepcopy(doc)
        return None

    async def create_index(self, *_args, **_kw):
        return "fake-index"

    async def delete_one(self, query, **_kw):
        self.delete_calls.append(dict(query))
        for i, doc in enumerate(self.docs):
            if matches(doc, query):
                del self.docs[i]
                return _result(deleted_count=1)
        return _result()

    async def delete_many(self, query, **_kw):
        self.delete_calls.append(copy.deepcopy(query))
        before = len(self.docs)
        self.docs[:] = [d for d in self.docs if not matches(d, query)]
        return _result(deleted_count=before - len(self.docs))


class FakeDatabase:
    """Attribute access creates collections on demand (like Motor)."""

    def __init__(self, **collections: FakeCollection):
        self.__dict__["_collections"] = dict(collections)

    def __getattr__(self, name: str) -> FakeCollection:
        if name.startswith("__"):
            raise AttributeError(name)
        colls = self.__dict__["_collections"]
        if name not in colls:
            colls[name] = FakeCollection(name)
        return colls[name]

    def add(self, name: str, coll: FakeCollection) -> FakeCollection:
        self.__dict__["_collections"][name] = coll
        return coll
