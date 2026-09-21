"""SQLite metadata and hash-verified immutable snapshots for a local workspace.

Publish snapshot files before committing references in SQLite. A crash may leave an
unreferenced file, but never a committed version pointing to a half-written file.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .engine import preview_repair, read_csv, validate_contract
from .models import (
    ColumnMap,
    ConflictError,
    DatasetVersion,
    ParseSettings,
    Preview,
    RepairPlan,
    ValidationError,
)


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "detective.sqlite3"
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
                    active_version_id TEXT NOT NULL, source_sha256 TEXT NOT NULL,
                    original_path TEXT NOT NULL, delimiter TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id),
                    parent_version_id TEXT REFERENCES versions(id), created_at TEXT NOT NULL,
                    path TEXT NOT NULL, sha256 TEXT NOT NULL, reason TEXT NOT NULL,
                    kind TEXT NOT NULL, plan_json TEXT, changes_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS version_dataset ON versions(dataset_id, created_at);
                CREATE TABLE IF NOT EXISTS requests (
                    dataset_id TEXT NOT NULL REFERENCES datasets(id), key TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL, version_id TEXT NOT NULL REFERENCES versions(id),
                    PRIMARY KEY(dataset_id,key)
                );
                CREATE TABLE IF NOT EXISTS imports (
                    key TEXT PRIMARY KEY, payload_sha256 TEXT NOT NULL,
                    dataset_id TEXT NOT NULL REFERENCES datasets(id)
                );
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValidationError("Workspace path is outside the storage directory")
        return path

    def _publish(self, version: DatasetVersion) -> tuple[str, str]:
        relative = f"datasets/{version.dataset_id}/versions/{version.version_id}.json"
        payload = canonical(version.to_dict())
        atomic_write(self._path(relative), payload)
        return relative, digest(payload)

    def _insert_version(self, conn, version, relative, sha256, kind, plan=None, changes=None):
        conn.execute(
            "INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (version.version_id, version.dataset_id, version.parent_version_id,
             version.created_at, relative, sha256, version.description, kind,
             json.dumps(plan, ensure_ascii=False) if plan is not None else None,
             json.dumps(changes or [], ensure_ascii=False)),
        )

    def create_dataset(
        self, name: str, data: bytes, mapping: ColumnMap, settings: ParseSettings,
        delimiter: str = ",", import_key: str | None = None,
    ) -> DatasetVersion:
        name = name.strip()
        if not name or len(name) > 160:
            raise ValidationError("Give the investigation a name of 1–160 characters")
        columns, rows = read_csv(data, delimiter)
        validate_contract(columns, mapping, settings)
        payload_sha = digest(canonical({"name": name, "source": digest(data), "mapping": asdict(mapping),
                                        "settings": asdict(settings), "delimiter": delimiter}))
        version = DatasetVersion(uuid.uuid4().hex, uuid.uuid4().hex, None, columns, rows,
                                 mapping, settings, now(), "Original import")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if import_key:
                old = conn.execute("SELECT * FROM imports WHERE key=?", (import_key,)).fetchone()
                if old:
                    if old["payload_sha256"] != payload_sha:
                        raise ConflictError("Import key has already been used for different content")
                    return self.active_version(old["dataset_id"])
            original = f"datasets/{version.dataset_id}/original.csv"
            atomic_write(self._path(original), data)
            relative, sha256 = self._publish(version)
            conn.execute("INSERT INTO datasets VALUES (?,?,?,?,?,?,?)", (
                version.dataset_id, name, version.created_at, version.version_id,
                digest(data), original, delimiter,
            ))
            self._insert_version(conn, version, relative, sha256, "import")
            if import_key:
                conn.execute("INSERT INTO imports VALUES (?,?,?)", (import_key, payload_sha, version.dataset_id))
        return version

    def list_datasets(self) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM datasets ORDER BY rowid DESC")]

    def dataset(self, dataset_id: str) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        if row is None:
            raise ValidationError("Investigation not found")
        return dict(row)

    def load_version(self, version_id: str, dataset_id: str | None = None) -> DatasetVersion:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
        if row is None or (dataset_id and row["dataset_id"] != dataset_id):
            raise ValidationError("Version not found in this investigation")
        path = self._path(row["path"])
        if not path.is_file():
            raise ValidationError("Snapshot is missing; restore the workspace from backup")
        content = path.read_bytes()
        if digest(content) != row["sha256"]:
            raise ValidationError("Snapshot checksum mismatch; the stored evidence has changed")
        version = DatasetVersion.from_dict(json.loads(content))
        if version.version_id != version_id or version.dataset_id != row["dataset_id"]:
            raise ValidationError("Snapshot identity does not match the version record")
        return version

    def active_version(self, dataset_id: str) -> DatasetVersion:
        return self.load_version(self.dataset(dataset_id)["active_version_id"], dataset_id)

    def history(self, dataset_id: str, *, through_version_id: str | None = None) -> list[dict]:
        self.dataset(dataset_id)
        with self.connect() as conn:
            if through_version_id is not None:
                target = conn.execute("SELECT rowid FROM versions WHERE id=? AND dataset_id=?",
                                      (through_version_id, dataset_id)).fetchone()
                if target is None:
                    raise ValidationError("Version not found in this investigation")
                return [dict(r) for r in conn.execute(
                    "SELECT * FROM versions WHERE dataset_id=? AND rowid<=? ORDER BY rowid DESC",
                    (dataset_id, target[0]),
                )]
            return [dict(r) for r in conn.execute(
                "SELECT * FROM versions WHERE dataset_id=? ORDER BY rowid DESC", (dataset_id,))]

    def preview(self, dataset_id: str, plan: RepairPlan) -> Preview:
        version = self.active_version(dataset_id)
        if plan.base_version_id != version.version_id:
            raise ConflictError("The data changed. Build a new preview from the current version.")
        return preview_repair(version, plan)

    @staticmethod
    def _prior_request(conn, dataset_id: str, key: str, payload_sha: str) -> str | None:
        if not key or len(key) > 200:
            raise ValidationError("A bounded idempotency key is required")
        old = conn.execute("SELECT * FROM requests WHERE dataset_id=? AND key=?", (dataset_id, key)).fetchone()
        if old:
            if old["payload_sha256"] != payload_sha:
                raise ConflictError("This action key was already used for a different request")
            return old["version_id"]
        return None

    def apply(
        self, dataset_id: str, plan: RepairPlan, expected_fingerprint: str, idempotency_key: str,
    ) -> DatasetVersion:
        # Keep the audited request detached from a caller's mutable row selection
        # for the entire hash / preview / transaction sequence.
        plan = RepairPlan.from_dict(plan.to_dict())
        payload_sha = digest(canonical({"plan": plan.to_dict(), "fingerprint": expected_fingerprint}))
        with self.connect() as conn:
            prior = self._prior_request(conn, dataset_id, idempotency_key, payload_sha)
        if prior:
            return self.load_version(prior, dataset_id)
        # Recompute against the immutable base, not the moving active pointer.
        # A racing retry may already have committed this same idempotency key.
        # The transaction below resolves retries before rejecting stale work.
        preview = preview_repair(self.load_version(plan.base_version_id, dataset_id), plan)
        if preview.fingerprint != expected_fingerprint:
            raise ConflictError("The submitted repair differs from the preview. Preview it again.")
        version = replace(preview.version, created_at=now(), description=plan.reason)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = self._prior_request(conn, dataset_id, idempotency_key, payload_sha)
            if prior:
                return self.load_version(prior, dataset_id)
            active = conn.execute("SELECT active_version_id FROM datasets WHERE id=?", (dataset_id,)).fetchone()
            if active is None or active[0] != plan.base_version_id:
                raise ConflictError("Another action changed the data. Build a fresh preview.")
            relative, sha256 = self._publish(version)
            self._insert_version(conn, version, relative, sha256, "repair", plan.to_dict(),
                                 [asdict(change) for change in preview.changes])
            conn.execute("UPDATE datasets SET active_version_id=? WHERE id=?", (version.version_id, dataset_id))
            conn.execute("INSERT INTO requests VALUES (?,?,?,?)", (
                dataset_id, idempotency_key, payload_sha, version.version_id,
            ))
        return version

    def restore(
        self, dataset_id: str, target_version_id: str, base_version_id: str,
        reason: str, idempotency_key: str,
    ) -> DatasetVersion:
        if not reason.strip() or len(reason) > 1000:
            raise ValidationError("Explain why this version is being restored (1–1000 characters)")
        payload_sha = digest(canonical({"restore": target_version_id, "base": base_version_id, "reason": reason}))
        with self.connect() as conn:
            prior = self._prior_request(conn, dataset_id, idempotency_key, payload_sha)
        if prior:
            return self.load_version(prior, dataset_id)
        target = self.load_version(target_version_id, dataset_id)
        version = replace(target, version_id=uuid.uuid4().hex, parent_version_id=base_version_id,
                          created_at=now(), description=reason)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = self._prior_request(conn, dataset_id, idempotency_key, payload_sha)
            if prior:
                return self.load_version(prior, dataset_id)
            current = conn.execute("SELECT active_version_id FROM datasets WHERE id=?", (dataset_id,)).fetchone()
            if current is None or current[0] != base_version_id:
                raise ConflictError("The data changed. Review the current version before restoring.")
            relative, sha256 = self._publish(version)
            self._insert_version(conn, version, relative, sha256, "restore", {"restored_from": target_version_id})
            conn.execute("UPDATE datasets SET active_version_id=? WHERE id=?", (version.version_id, dataset_id))
            conn.execute("INSERT INTO requests VALUES (?,?,?,?)", (
                dataset_id, idempotency_key, payload_sha, version.version_id,
            ))
        return version

    def original_bytes(self, dataset_id: str) -> bytes:
        dataset = self.dataset(dataset_id)
        data = self._path(dataset["original_path"]).read_bytes()
        if digest(data) != dataset["source_sha256"]:
            raise ValidationError("Original file checksum mismatch")
        return data
