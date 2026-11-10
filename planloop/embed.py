"""Embeddings without third-party dependencies.

The default embedder is deterministic signed feature hashing over word
unigrams/bigrams and character trigrams.  It is not a semantic model -- it is a
*lexical* similarity signal that happens to be stable and free.  When a real
embedding endpoint is configured it is used instead, and the storage format
does not change (same float32 blob).

This matters for honesty: with the hashing embedder, "vector search" is a
fuzzy lexical match.  Calling it semantic search would be a lie.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.request
from typing import Protocol, runtime_checkable

TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}|[\u4e00-\u9fff]{1,}")


@runtime_checkable
class Embedder(Protocol):
    dim: int
    name: str

    def embed(self, text: str) -> list[float]: ...


def _features(text: str) -> list[str]:
    lowered = text.lower()
    tokens = TOKEN_RE.findall(lowered)
    feats: list[str] = list(tokens)
    feats += [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
    compact = re.sub(r"\s+", "", lowered)
    feats += [compact[i : i + 3] for i in range(0, max(0, len(compact) - 2))]
    return feats


class HashingEmbedder:
    """Signed feature hashing; no dependencies, deterministic across machines."""

    def __init__(self, dim: int = 384):
        self.dim = dim
        self.name = f"hashing-{dim}"

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for feat in _features(text):
            digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0:
            return vec
        return [v / norm for v in vec]


class RemoteEmbedder:
    """OpenAI-compatible /embeddings endpoint."""

    def __init__(self, model: str, *, base_url: str | None = None, api_key: str | None = None, timeout: float = 30.0):
        self.model = model
        self.name = f"remote:{model}"
        self.base_url = (base_url or os.environ.get("PLANLOOP_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("PLANLOOP_API_KEY", "")
        self.timeout = timeout
        self.dim = 0

    def embed(self, text: str) -> list[float]:
        payload = json.dumps({"model": self.model, "input": text}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        vec = [float(x) for x in data["data"][0]["embedding"]]
        self.dim = len(vec)
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def default_embedder() -> Embedder:
    model = os.environ.get("PLANLOOP_EMBED_MODEL")
    if model:
        return RemoteEmbedder(model)
    return HashingEmbedder()
