"""Compact append-only authority for decisions and their durable artifacts."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator, FormatChecker

from .experiment_contract import canonical_json_bytes, schema_path


LEDGER_SCHEMA_VERSION = "decision_ledger.v1"
DECISION_SCHEMA_VERSION = "decision_record.v1"
ARTIFACT_SCHEMA_VERSION = "decision_artifact_link.v1"
DECISION_KINDS = ("direction", "execution", "acceptance")
ARTIFACT_KINDS = ("contract", "code_state", "evidence", "report")
DECISION_STATUSES = (
    "proposed",
    "active",
    "completed",
    "rejected",
    "superseded",
)

_LEDGER_DIRECTORY = "decisions"
_LEDGER_FILE = "decision-ledger.jsonl"
_LOCK_FILE = "decision-ledger.lock"
_MAX_LEDGER_BYTES = 64 * 1024 * 1024
_MAX_LINE_BYTES = 64 * 1024
_STATUS_TRANSITIONS = {
    "proposed": {"proposed", "active", "rejected", "superseded"},
    "active": {"active", "completed", "rejected", "superseded"},
    "completed": {"completed", "superseded"},
    "rejected": {"rejected"},
    "superseded": {"superseded"},
}


class DecisionLedgerError(RuntimeError):
    """Base error for decision authority operations."""


class DecisionLedgerIntegrityError(DecisionLedgerError):
    """Raised when the append-only authority is malformed or inconsistent."""


def decision_record_sha256(record):
    validate_decision_record(record)
    return hashlib.sha256(canonical_json_bytes(record)).hexdigest()


def validate_decision_record(record):
    _validate_schema(record, "decision_record.schema.json", "decision record")
    decision_id = record["decision_id"]
    if record["parent_decision_id"] == decision_id:
        raise DecisionLedgerError("decision cannot be its own parent")
    if record["supersedes_decision_id"] == decision_id:
        raise DecisionLedgerError("decision cannot supersede itself")
    if (
        record["parent_decision_id"] is not None
        and record["parent_decision_id"] == record["supersedes_decision_id"]
    ):
        raise DecisionLedgerError(
            "decision parent and superseded decision must be distinct"
        )
    return record


def validate_decision_artifact_link(link):
    _validate_schema(
        link,
        "decision_artifact_link.schema.json",
        "decision artifact link",
    )
    expected_length = {
        "sha256": 64,
        "git_sha1": 40,
        "git_sha256": 64,
    }[link["digest_algorithm"]]
    if len(link["digest"]) != expected_length:
        raise DecisionLedgerError(
            "artifact digest length does not match digest_algorithm"
        )
    _validate_artifact_locator(link["locator"], link["artifact_kind"])
    return link


def decision_ledger_root(work_root):
    return Path(work_root).resolve() / _LEDGER_DIRECTORY


class DecisionLedger:
    """One compact project ledger with hash-chained immutable entries."""

    def __init__(self, work_root, *, create=False):
        self.work_root = Path(work_root).resolve()
        self.root = decision_ledger_root(self.work_root)
        if create:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            _ensure_regular_file(self.root / _LOCK_FILE, create=True)
            _ensure_regular_file(self.root / _LEDGER_FILE, create=True)
        _ensure_safe_directory(self.root)
        _ensure_regular_file(self.root / _LOCK_FILE)
        _ensure_regular_file(self.root / _LEDGER_FILE)
        self._load_entries()

    @classmethod
    def create(cls, work_root):
        return cls(work_root, create=True)

    @property
    def path(self):
        return self.root / _LEDGER_FILE

    def append_decision(self, record):
        record = copy.deepcopy(record)
        validate_decision_record(record)
        with self._lock():
            entries = self._load_entries()
            decisions = _decision_revisions(entries)
            key = (record["decision_id"], record["revision"])
            prior = decisions.get(key)
            if prior is not None:
                if prior != record:
                    raise DecisionLedgerIntegrityError(
                        "decision revision replay conflicts with authority"
                    )
                return {"created": False, "record": copy.deepcopy(prior)}
            self._validate_new_decision(record, decisions)
            self._append_entry("decision_revision", record, entries)
            return {"created": True, "record": copy.deepcopy(record)}

    def append_artifact_link(self, link):
        link = copy.deepcopy(link)
        validate_decision_artifact_link(link)
        with self._lock():
            entries = self._load_entries()
            links = _artifact_links(entries)
            prior = links.get(link["artifact_id"])
            if prior is not None:
                if prior != link:
                    raise DecisionLedgerIntegrityError(
                        "artifact link replay conflicts with authority"
                    )
                return {"created": False, "link": copy.deepcopy(prior)}
            latest = _latest_decisions(entries)
            if link["decision_id"] not in latest:
                raise DecisionLedgerError(
                    "artifact link references an unknown decision"
                )
            self._append_entry("artifact_link", link, entries)
            return {"created": True, "link": copy.deepcopy(link)}

    def entries(self):
        return copy.deepcopy(self._load_entries())

    def latest_decisions(self, *, statuses=None):
        latest = _latest_decisions(self._load_entries())
        if statuses is not None:
            statuses = set(statuses)
            unknown = statuses.difference(DECISION_STATUSES)
            if unknown:
                raise DecisionLedgerError(
                    "unsupported decision status filter: "
                    + ", ".join(sorted(unknown))
                )
            latest = {
                key: value
                for key, value in latest.items()
                if value["status"] in statuses
            }
        return [copy.deepcopy(latest[key]) for key in sorted(latest)]

    def artifact_links(self, decision_id=None):
        links = _artifact_links(self._load_entries()).values()
        if decision_id is not None:
            links = [
                link for link in links if link["decision_id"] == decision_id
            ]
        return sorted(
            (copy.deepcopy(link) for link in links),
            key=lambda item: item["artifact_id"],
        )

    def decision_graph(self, decision_id):
        entries = self._load_entries()
        latest = _latest_decisions(entries)
        if decision_id not in latest:
            raise DecisionLedgerError(f"unknown decision: {decision_id}")
        children = {}
        for record in latest.values():
            parent = record["parent_decision_id"]
            if parent is not None:
                children.setdefault(parent, []).append(record["decision_id"])
        links = self.artifact_links()
        links_by_decision = {}
        for link in links:
            links_by_decision.setdefault(link["decision_id"], []).append(link)

        def build(current, seen):
            if current in seen:
                raise DecisionLedgerIntegrityError("decision graph contains a cycle")
            return {
                "decision": copy.deepcopy(latest[current]),
                "artifacts": copy.deepcopy(links_by_decision.get(current, [])),
                "children": [
                    build(child, seen | {current})
                    for child in sorted(children.get(current, []))
                ],
            }

        return build(decision_id, set())

    def _validate_new_decision(self, record, revisions):
        decision_id = record["decision_id"]
        prior_revisions = sorted(
            (
                item
                for (candidate_id, _revision), item in revisions.items()
                if candidate_id == decision_id
            ),
            key=lambda item: item["revision"],
        )
        known_decisions = {candidate_id for candidate_id, _ in revisions}
        if not prior_revisions:
            if record["revision"] != 1:
                raise DecisionLedgerError("first decision revision must be 1")
            for field in ("parent_decision_id", "supersedes_decision_id"):
                reference = record[field]
                if reference is not None and reference not in known_decisions:
                    raise DecisionLedgerError(
                        f"{field} references an unknown decision"
                    )
            if _would_create_parent_cycle(record, _latest_from_revisions(revisions)):
                raise DecisionLedgerError("decision parent would create a cycle")
            return
        prior = prior_revisions[-1]
        if record["revision"] != prior["revision"] + 1:
            raise DecisionLedgerError("decision revisions must be contiguous")
        if record["previous_revision_sha256"] != decision_record_sha256(prior):
            raise DecisionLedgerError(
                "decision previous_revision_sha256 does not bind prior revision"
            )
        for field in (
            "decision_kind",
            "subject",
            "authority_level",
            "parent_decision_id",
            "supersedes_decision_id",
        ):
            if record[field] != prior[field]:
                raise DecisionLedgerError(
                    f"decision revision cannot change identity field: {field}"
                )
        if record["status"] not in _STATUS_TRANSITIONS[prior["status"]]:
            raise DecisionLedgerError(
                f"invalid decision status transition: {prior['status']} -> "
                f"{record['status']}"
            )

    def _append_entry(self, entry_kind, payload, entries):
        previous = entries[-1]["entry_sha256"] if entries else None
        body = {
            "ledger_schema_version": LEDGER_SCHEMA_VERSION,
            "sequence": len(entries) + 1,
            "previous_entry_sha256": previous,
            "entry_kind": entry_kind,
            "payload": payload,
        }
        entry = {
            **body,
            "entry_sha256": hashlib.sha256(canonical_json_bytes(body)).hexdigest(),
        }
        encoded = canonical_json_bytes(entry) + b"\n"
        if len(encoded) > _MAX_LINE_BYTES:
            raise DecisionLedgerError("decision ledger entry exceeds size limit")
        current_size = self.path.stat().st_size
        if current_size + len(encoded) > _MAX_LEDGER_BYTES:
            raise DecisionLedgerError("decision ledger exceeds size limit")
        descriptor = _open_regular_file(
            self.path,
            os.O_WRONLY | os.O_APPEND,
        )
        try:
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise DecisionLedgerIntegrityError(
                        "decision ledger append was incomplete"
                    )
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _load_entries(self):
        _ensure_regular_file(self.path)
        if self.path.stat().st_size > _MAX_LEDGER_BYTES:
            raise DecisionLedgerIntegrityError("decision ledger exceeds size limit")
        entries = []
        previous = None
        with self.path.open("rb") as stream:
            for sequence, line in enumerate(stream, start=1):
                if len(line) > _MAX_LINE_BYTES:
                    raise DecisionLedgerIntegrityError(
                        "decision ledger line exceeds size limit"
                    )
                if not line.endswith(b"\n"):
                    raise DecisionLedgerIntegrityError(
                        "decision ledger has an incomplete terminal line"
                    )
                try:
                    entry = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise DecisionLedgerIntegrityError(
                        "decision ledger contains invalid JSON"
                    ) from exc
                _validate_ledger_entry(entry, sequence, previous)
                entries.append(entry)
                previous = entry["entry_sha256"]
        _validate_decision_history(entries)
        return entries

    @contextmanager
    def _lock(self):
        lock_path = self.root / _LOCK_FILE
        _ensure_regular_file(lock_path)
        descriptor = _open_regular_file(lock_path, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def load_decision_ledger(work_root):
    return DecisionLedger(work_root)


def _validate_schema(value, filename, label):
    try:
        schema = json.loads(schema_path(filename).read_text(encoding="utf-8"))
        validator = Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        )
        errors = sorted(validator.iter_errors(value), key=lambda item: list(item.path))
    except (OSError, json.JSONDecodeError) as exc:
        raise DecisionLedgerError(f"{label} schema is unavailable") from exc
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.path) or "<root>"
        raise DecisionLedgerError(f"invalid {label} at {location}: {error.message}")


def _validate_artifact_locator(locator, artifact_kind):
    if "\x00" in locator or "\n" in locator or "\r" in locator:
        raise DecisionLedgerError("artifact locator contains unsafe characters")
    if locator.startswith("git:commit:"):
        oid = locator.removeprefix("git:commit:")
        if len(oid) not in {40, 64} or any(char not in "0123456789abcdef" for char in oid):
            raise DecisionLedgerError("artifact Git commit locator is invalid")
        if artifact_kind != "code_state":
            raise DecisionLedgerError("Git commit locator requires code_state kind")
        return
    if locator.startswith("git:ref:"):
        ref = locator.removeprefix("git:ref:")
        if not ref.startswith("refs/agentteam/") or any(
            part in {"", ".", ".."} for part in ref.split("/")
        ):
            raise DecisionLedgerError("artifact Git ref locator is invalid")
        if artifact_kind != "code_state":
            raise DecisionLedgerError("Git ref locator requires code_state kind")
        return
    if locator.startswith("path:"):
        relative = PurePosixPath(locator.removeprefix("path:"))
        if relative.is_absolute() or not relative.parts or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise DecisionLedgerError("artifact path locator must be safe and relative")
        return
    raise DecisionLedgerError("unsupported artifact locator scheme")


def _validate_ledger_entry(entry, expected_sequence, previous):
    required = {
        "ledger_schema_version",
        "sequence",
        "previous_entry_sha256",
        "entry_kind",
        "payload",
        "entry_sha256",
    }
    if not isinstance(entry, dict) or set(entry) != required:
        raise DecisionLedgerIntegrityError("decision ledger entry shape is invalid")
    if entry["ledger_schema_version"] != LEDGER_SCHEMA_VERSION:
        raise DecisionLedgerIntegrityError("decision ledger schema version is invalid")
    if entry["sequence"] != expected_sequence:
        raise DecisionLedgerIntegrityError("decision ledger sequence is invalid")
    if entry["previous_entry_sha256"] != previous:
        raise DecisionLedgerIntegrityError("decision ledger hash chain is invalid")
    if entry["entry_kind"] == "decision_revision":
        validate_decision_record(entry["payload"])
    elif entry["entry_kind"] == "artifact_link":
        validate_decision_artifact_link(entry["payload"])
    else:
        raise DecisionLedgerIntegrityError("decision ledger entry kind is invalid")
    body = {key: value for key, value in entry.items() if key != "entry_sha256"}
    expected = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    if entry["entry_sha256"] != expected:
        raise DecisionLedgerIntegrityError("decision ledger entry digest is invalid")


def _validate_decision_history(entries):
    revisions = {}
    links = {}
    for entry in entries:
        payload = entry["payload"]
        if entry["entry_kind"] == "decision_revision":
            key = (payload["decision_id"], payload["revision"])
            if key in revisions:
                raise DecisionLedgerIntegrityError(
                    "decision ledger contains a duplicate revision"
                )
            prior = _latest_from_revisions(revisions).get(
                payload["decision_id"]
            )
            known = {decision_id for decision_id, _ in revisions}
            if prior is None:
                if payload["revision"] != 1:
                    raise DecisionLedgerIntegrityError(
                        "first decision revision must be 1"
                    )
                for field in (
                    "parent_decision_id",
                    "supersedes_decision_id",
                ):
                    reference = payload[field]
                    if reference is not None and reference not in known:
                        raise DecisionLedgerIntegrityError(
                            f"decision history contains unknown {field}"
                        )
            else:
                if payload["revision"] != prior["revision"] + 1:
                    raise DecisionLedgerIntegrityError(
                        "decision revisions must be contiguous"
                    )
                if (
                    payload["previous_revision_sha256"]
                    != decision_record_sha256(prior)
                ):
                    raise DecisionLedgerIntegrityError(
                        "decision revision chain is invalid"
                    )
                for field in (
                    "decision_kind",
                    "subject",
                    "authority_level",
                    "parent_decision_id",
                    "supersedes_decision_id",
                ):
                    if payload[field] != prior[field]:
                        raise DecisionLedgerIntegrityError(
                            "decision revision changed identity field: "
                            + field
                        )
                if payload["status"] not in _STATUS_TRANSITIONS[prior["status"]]:
                    raise DecisionLedgerIntegrityError(
                        "decision history contains an invalid status transition"
                    )
            revisions[key] = payload
            if _would_create_parent_cycle(
                payload,
                _latest_from_revisions(revisions),
            ):
                raise DecisionLedgerIntegrityError(
                    "decision graph contains a cycle"
                )
        else:
            artifact_id = payload["artifact_id"]
            if artifact_id in links:
                raise DecisionLedgerIntegrityError(
                    "decision ledger contains a duplicate artifact link"
                )
            if payload["decision_id"] not in {
                decision_id for decision_id, _ in revisions
            }:
                raise DecisionLedgerIntegrityError(
                    "decision history contains a dangling artifact link"
                )
            links[artifact_id] = payload


def _decision_revisions(entries):
    return {
        (entry["payload"]["decision_id"], entry["payload"]["revision"]): entry[
            "payload"
        ]
        for entry in entries
        if entry["entry_kind"] == "decision_revision"
    }


def _latest_from_revisions(revisions):
    latest = {}
    for (decision_id, revision), record in revisions.items():
        if decision_id not in latest or revision > latest[decision_id]["revision"]:
            latest[decision_id] = record
    return latest


def _latest_decisions(entries):
    return _latest_from_revisions(_decision_revisions(entries))


def _artifact_links(entries):
    return {
        entry["payload"]["artifact_id"]: entry["payload"]
        for entry in entries
        if entry["entry_kind"] == "artifact_link"
    }


def _would_create_parent_cycle(record, latest):
    current = record["parent_decision_id"]
    seen = {record["decision_id"]}
    while current is not None:
        if current in seen:
            return True
        seen.add(current)
        parent = latest.get(current)
        current = parent["parent_decision_id"] if parent else None
    return False


def _ensure_safe_directory(path):
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise DecisionLedgerError(f"decision ledger directory is missing: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise DecisionLedgerError(f"decision ledger directory is unsafe: {path}")


def _ensure_regular_file(path, *, create=False):
    if create:
        descriptor = _open_regular_file(
            path,
            os.O_CREAT | os.O_RDWR,
            mode=0o600,
        )
        os.close(descriptor)
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise DecisionLedgerError(f"decision ledger file is missing: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DecisionLedgerError(f"decision ledger file is unsafe: {path}")


def _open_regular_file(path, flags, *, mode=0o600):
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags | no_follow, mode)
    except OSError as exc:
        raise DecisionLedgerError(
            f"decision ledger file cannot be opened safely: {path}"
        ) from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise DecisionLedgerError(f"decision ledger file is unsafe: {path}")
    return descriptor
