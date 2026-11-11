"""Embedders. The OpenAI embedder is the ONLY place that can spend embedding quota, and it is guarded:

* construction requires allow_paid=True (the CLI sets it only with ALLOW_PAID_CALLS=1 and an explicit --embed);
* every vector must have exactly 1,536 dimensions (matching procurement.policy_chunks.embedding);
* usage is appended (without text content) to runs/usage.jsonl; retries are owned by the SDK (max_retries=2).

HashingEmbedder is a deterministic, offline, TEST-ONLY stand-in (bag-of-words hashing). It is never used for live runs."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Protocol

EMBEDDING_MODEL = "text-embedding-3-small"
DIMENSIONS = 1536
USAGE_LOG = Path(__file__).resolve().parents[2] / "runs" / "usage.jsonl"
CACHE_PATH = Path(__file__).resolve().parents[2] / "runs" / "policy-query-embeddings.json"
STOP = set("the and for are with that this from have has was were not but all any can may must should would into its their".split())


class EmbeddingError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...
    def usage(self) -> dict[str, int]: ...


def to_vector_literal(vec: list[float]) -> str:
    """pgvector text input format, e.g. '[0.1,0.2,...]' (cast with ::extensions.vector in SQL)."""
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


class HashingEmbedder:
    model = "test-hashing-1536"

    def __init__(self):
        self._calls = 0

    def usage(self) -> dict[str, int]:
        return {"calls": self._calls, "input_tokens": 0}

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._calls += 1
        out = []
        for t in texts:
            v = [0.0] * DIMENSIONS
            for tok in re.findall(r"[a-z0-9]+", t.lower()):
                if len(tok) < 3 or tok in STOP:
                    continue
                v[int(hashlib.sha1(tok.encode()).hexdigest()[:8], 16) % DIMENSIONS] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out


class CachedEmbedder:
    """Query-embedding cache on disk. Texts already cached cost nothing; missing ones go to `inner` in ONE batch.
    With inner=None it is cache-only: a missing text raises EmbeddingError (the retriever then falls back to full-text)."""

    def __init__(self, inner: Embedder | None, path: Path = CACHE_PATH):
        self._inner, self._path = inner, path
        self.model = inner.model if inner else EMBEDDING_MODEL
        try:
            self._cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            self._cache = {}
        self.hits = self.misses = 0

    def usage(self) -> dict[str, int]:
        return self._inner.usage() if self._inner else {"calls": 0, "input_tokens": 0}

    def embed(self, texts: list[str]) -> list[list[float]]:
        missing = [t for t in dict.fromkeys(texts) if t not in self._cache]
        self.hits += len(texts) - len([t for t in texts if t in missing])
        if missing:
            if self._inner is None:
                raise EmbeddingError("QUERY_EMBEDDING_NOT_CACHED")
            self.misses += len(missing)
            for t, v in zip(missing, self._inner.embed(missing)):
                self._cache[t] = v
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._cache), encoding="utf-8")
        return [self._cache[t] for t in texts]


class OpenAIEmbedder:
    def __init__(self, *, allow_paid: bool, client=None, usage_log: Path | None = USAGE_LOG, request_id: str = "-",
                 max_total_tokens: int = 50_000):
        if not allow_paid:
            raise EmbeddingError("PAID_CALLS_NOT_AUTHORISED")
        self.model = EMBEDDING_MODEL
        if client is None:
            from openai import OpenAI
            key = os.getenv("OPENAI_API_KEY")
            if not key:
                raise EmbeddingError("MISSING_CREDENTIALS")
            client = OpenAI(api_key=key, max_retries=2, timeout=60.0)
        self._client, self._log, self._rid, self._cap = client, usage_log, request_id, max_total_tokens
        self._calls = self._tokens = 0

    def usage(self) -> dict[str, int]:
        return {"calls": self._calls, "input_tokens": self._tokens}

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._tokens >= self._cap:
            raise EmbeddingError("TOKEN_BUDGET_EXCEEDED")
        try:
            resp = self._client.embeddings.create(model=self.model, input=texts, dimensions=DIMENSIONS)
        except Exception as exc:  # noqa: BLE001 - class name only; messages may echo request data
            raise EmbeddingError(f"EMBEDDING_CALL_FAILED:{type(exc).__name__}") from exc
        vectors = [list(d.embedding) for d in resp.data]
        if len(vectors) != len(texts) or any(len(v) != DIMENSIONS for v in vectors):
            raise EmbeddingError("UNEXPECTED_EMBEDDING_SHAPE")
        used = getattr(getattr(resp, "usage", None), "prompt_tokens", 0) or getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
        self._calls += 1
        self._tokens += used
        if self._log:
            self._log.parent.mkdir(parents=True, exist_ok=True)
            with self._log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": round(time.time()), "request_id": self._rid, "purpose": "embedding", "model": self.model,
                                     "input_tokens": used, "output_tokens": 0, "items": len(texts)}) + "\n")
        return vectors
