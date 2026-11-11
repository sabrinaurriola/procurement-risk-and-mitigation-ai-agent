"""File store for assessment records. The record here is AUTHORITATIVE for every request (Notion/Gmail/events never change it).

Layout (root = runs/assessments/ by default, git-ignored):
  {request_id}.json      finished record  {request_hash, finished_at, envelope, trace}      written once, atomically
  {request_id}.run.json  running marker   {request_hash, mode, started_at, boot_id}         removed when the record is written
Writes go to a temporary file in the same directory, are flushed and fsynced, then os.replace()d, so a reader (or a crash) sees either the
old state or the complete new one, never a partial file. request_id is validated against a strict pattern before it reaches any path."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from src.api.models import REQUEST_ID_RE


def _retry_windows_sharing(fn, attempts: int = 10, delay: float = 0.02):
    """Windows raises PermissionError when a file is open in another thread/process at the instant of a replace or read. It is transient:
    retry a bounded number of times, then let the error propagate."""
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


class StoreError(Exception):
    """A stored file is unreadable. Carries no path or content (callers map it to a generic INTERNAL_ERROR)."""


class FileStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, request_id: str, suffix: str) -> Path:
        if not REQUEST_ID_RE.match(request_id):
            raise ValueError("invalid request_id")
        return self.root / f"{request_id}{suffix}"

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            raw = _retry_windows_sharing(lambda: path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StoreError(type(exc).__name__) from None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise StoreError("JSONDecodeError") from None
        if not isinstance(data, dict):
            raise StoreError("NotAnObject")
        return data

    def _write_atomic(self, path: Path, data: dict[str, Any]) -> None:
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            _retry_windows_sharing(lambda: os.replace(tmp, path))
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    # finished record
    def read_final(self, request_id: str) -> dict[str, Any] | None:
        return self._read(self._path(request_id, ".json"))

    def write_final(self, request_id: str, record: dict[str, Any]) -> None:
        self._write_atomic(self._path(request_id, ".json"), record)

    # running marker (lets a restarted server recognise a run whose worker died with the previous process)
    def read_run(self, request_id: str) -> dict[str, Any] | None:
        return self._read(self._path(request_id, ".run.json"))

    def write_run(self, request_id: str, marker: dict[str, Any]) -> None:
        self._write_atomic(self._path(request_id, ".run.json"), marker)

    def clear_run(self, request_id: str) -> None:
        try:
            self._path(request_id, ".run.json").unlink()
        except FileNotFoundError:
            pass
