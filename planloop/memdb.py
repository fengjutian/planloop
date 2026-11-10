"""Persistent memory: SQLite + FTS5 + vectors, split by visibility.

Layout:

    <repo>/.planloop/memory/public.db    committed, shareable
    <repo>/.planloop/memory/private.db   git-ignored, local only

Two physical files rather than a visibility column, because a convention can be
violated by a missing WHERE clause while a file cannot be committed by accident.

Three design choices that come from the market scan (docs/market-scan.md):

* **write decisions** -- ADD / UPDATE / NOOP / DELETE, each recorded in
  ``memory_history`` so "why did this change" stays answerable;
* **bi-temporal validity** -- ``valid_from`` / ``valid_to`` instead of a naked
  TTL: a fact stops being true at a point in time, it does not merely expire;
* **executable procedures** -- a procedure carries ``exec_spec``, a command that
  can actually be run, instead of prose describing what to run.

Retrieval is hybrid and explainable:

    score = 0.45*cosine + 0.35*bm25 + 0.12*confidence + 0.08*usage + key bonus

Every row carries ``evidence``.  Without evidence a row is a hypothesis capped at
low confidence, so it cannot crowd out verified knowledge.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from planloop.embed import Embedder, cosine, default_embedder

KINDS = ("fact", "procedure", "decision", "episode", "lesson")
STATUSES = ("active", "stale", "refuted", "archived")
OPS = ("ADD", "UPDATE", "DELETE", "NOOP")

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    scope       TEXT NOT NULL,
    visibility  TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    evidence    TEXT,
    confidence  REAL NOT NULL DEFAULT 0.5,
    status      TEXT NOT NULL DEFAULT 'active',
    source      TEXT NOT NULL DEFAULT 'observation',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    expires_at  REAL,
    valid_from  REAL,
    valid_to    REAL,
    exec_spec   TEXT,
    hits        INTEGER NOT NULL DEFAULT 0,
    misses      INTEGER NOT NULL DEFAULT 0,
    vec         BLOB
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_identity ON memory(scope, kind, key);
CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory(kind, status);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(id UNINDEXED, key, value, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS memory_history (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id  TEXT NOT NULL,
    op         TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    confidence REAL,
    reason     TEXT,
    at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_memory ON memory_history(memory_id, at);
"""

COLUMNS = {
    "valid_from": "REAL",
    "valid_to": "REAL",
    "exec_spec": "TEXT",
}


@dataclass
class Hit:
    id: str
    kind: str
    key: str
    value: str
    score: float
    confidence: float
    visibility: str
    evidence: dict[str, Any] | None = None
    exec_spec: str = ""
    breakdown: dict[str, float] = field(default_factory=dict)

    def render(self) -> str:
        badge = "ok" if self.evidence else "??"
        suffix = f" [exec: {self.exec_spec}]" if self.exec_spec else ""
        return f"[{badge} {self.kind}:{self.key}] {self.value}{suffix}  (score={self.score:.2f})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "key": self.key,
            "value": self.value,
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 3),
            "visibility": self.visibility,
            "evidence": self.evidence,
            "exec_spec": self.exec_spec,
        }


@dataclass
class WriteResult:
    op: str
    id: str
    previous: str | None = None

    def render(self) -> str:
        if self.op == "NOOP":
            return f"NOOP {self.id} (unchanged)"
        if self.op == "UPDATE":
            return f"UPDATE {self.id}: {self.previous!r} -> new value"
        return f"{self.op} {self.id}"


def repo_scope(root: Path | str) -> str:
    digest = hashlib.sha1(str(Path(root).resolve()).lower().encode("utf-8")).hexdigest()
    return f"repo:{digest[:12]}"


def _pack(vec: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


class MemoryDB:
    def __init__(
        self,
        path: Path | str,
        *,
        scope: str,
        root: Path | str,
        visibility: str = "private",
        embedder: Embedder | None = None,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.scope = scope
        self.root = Path(root).resolve()
        self.visibility = visibility
        self.embedder = embedder or default_embedder()
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()
        self._reindex_if_needed()

    # -- internals --------------------------------------------------------
    def _migrate(self) -> None:
        """Add columns introduced after the first database was created."""
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(memory)")}
        for name, sql_type in COLUMNS.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE memory ADD COLUMN {name} {sql_type}")
        self.conn.commit()

    def _reindex_if_needed(self) -> None:
        rows = self.conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        fts = self.conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
        if rows != fts:
            self.conn.execute("DELETE FROM memory_fts")
            self.conn.executemany(
                "INSERT INTO memory_fts(id, key, value) VALUES (?, ?, ?)",
                [(r["id"], r["key"], r["value"]) for r in self.conn.execute("SELECT id, key, value FROM memory")],
            )
            self.conn.commit()

    def _id(self, kind: str, key: str) -> str:
        raw = f"{self.scope}|{kind}|{key}".encode("utf-8")
        return "m_" + hashlib.sha1(raw).hexdigest()[:12]

    def _log(self, memory_id: str, op: str, *, old: str | None, new: str | None, confidence: float, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO memory_history(memory_id, op, old_value, new_value, confidence, reason, at) "
            "VALUES (?,?,?,?,?,?,?)",
            (memory_id, op, old, new, confidence, reason, time.time()),
        )

    # -- writes -----------------------------------------------------------
    def write(
        self,
        kind: str,
        key: str,
        value: str,
        *,
        evidence: dict[str, Any] | None = None,
        confidence: float = 0.5,
        ttl_days: float | None = None,
        source: str = "observation",
        exec_spec: str | None = None,
        reason: str = "",
    ) -> WriteResult:
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
        key, value = key.strip(), value.strip()
        if not key or not value:
            raise ValueError("memory key and value must be non-empty")

        # no evidence -> hypothesis: capped, cannot outrank verified facts
        if evidence is None:
            confidence = min(confidence, 0.35)
        confidence = max(0.0, min(1.0, confidence))

        now = time.time()
        mid = self._id(kind, key)
        existing = self.conn.execute("SELECT * FROM memory WHERE id = ?", (mid,)).fetchone()
        previous = existing["value"] if existing else None
        same = (
            existing is not None
            and existing["status"] != "archived"
            and existing["value"] == value
            and (existing["exec_spec"] or "") == (exec_spec or "")
        )

        if same:
            effective = max(existing["confidence"], confidence)
            self.conn.execute(
                "UPDATE memory SET updated_at = ?, evidence = COALESCE(?, evidence), "
                "confidence = MAX(confidence, ?) WHERE id = ?",
                (now, json.dumps(evidence, ensure_ascii=False) if evidence else None, confidence, mid),
            )
            self._log(mid, "NOOP", old=previous, new=value, confidence=effective, reason=reason or "unchanged")
            self.conn.commit()
            return WriteResult("NOOP", mid, previous)

        op = "UPDATE" if existing else "ADD"
        vec = _pack(self.embedder.embed(f"{key} {value}"))
        valid_to = now + ttl_days * 86400 if ttl_days else None

        self.conn.execute(
            """
            INSERT INTO memory (id, kind, scope, visibility, key, value, evidence, confidence,
                                status, source, created_at, updated_at, expires_at,
                                valid_from, valid_to, exec_spec, hits, misses, vec)
            VALUES (?,?,?,?,?,?,?,?, 'active', ?,?,?,?,?,?,?,0,0,?)
            ON CONFLICT(id) DO UPDATE SET
                value=excluded.value,
                evidence=excluded.evidence,
                confidence=excluded.confidence,
                updated_at=excluded.updated_at,
                expires_at=excluded.expires_at,
                valid_to=excluded.valid_to,
                exec_spec=excluded.exec_spec,
                vec=excluded.vec,
                status='active'
            """,
            (
                mid, kind, self.scope, self.visibility, key, value,
                json.dumps(evidence, ensure_ascii=False) if evidence else None,
                confidence, source, now, now, valid_to, now, valid_to, exec_spec or None, vec,
            ),
        )
        self.conn.execute("DELETE FROM memory_fts WHERE id = ?", (mid,))
        self.conn.execute("INSERT INTO memory_fts(id, key, value) VALUES (?,?,?)", (mid, key, value))
        self._log(mid, op, old=previous, new=value, confidence=confidence, reason=reason)
        self.conn.commit()
        return WriteResult(op, mid, previous)

    def upsert(self, kind: str, key: str, value: str, **kw: Any) -> str:
        """Backwards-compatible wrapper: returns the id."""
        return self.write(kind, key, value, **kw).id

    def forget(self, mid: str, *, reason: str = "forgotten") -> bool:
        row = self.conn.execute("SELECT * FROM memory WHERE id = ?", (mid,)).fetchone()
        if row is None:
            return False
        now = time.time()
        self.conn.execute(
            "UPDATE memory SET status='archived', valid_to=?, updated_at=? WHERE id = ?", (now, now, mid)
        )
        self._log(mid, "DELETE", old=row["value"], new=None, confidence=row["confidence"], reason=reason)
        self.conn.commit()
        return True

    def set_status(self, mid: str, status: str, *, confidence: float | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        sets = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, time.time()]
        if confidence is not None:
            sets.append("confidence = ?")
            params.append(max(0.0, min(1.0, confidence)))
        params.append(mid)
        self.conn.execute(f"UPDATE memory SET {', '.join(sets)} WHERE id = ?", params)
        self.conn.commit()

    def record_use(self, ids: Iterable[str], *, miss: bool = False) -> None:
        col = "misses" if miss else "hits"
        for mid in ids:
            self.conn.execute(f"UPDATE memory SET {col} = {col} + 1 WHERE id = ?", (mid,))
        self.conn.commit()

    # -- reads ------------------------------------------------------------
    def get(self, kind: str, key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM memory WHERE scope = ? AND kind = ? AND key = ?", (self.scope, kind, key)
        ).fetchone()

    def history(self, mid: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT op, old_value, new_value, confidence, reason, at FROM memory_history "
                "WHERE memory_id = ? ORDER BY at",
                (mid,),
            )
        ]

    def rows(
        self,
        *,
        kinds: Iterable[str] | None = None,
        status: str | None = "active",
        now: float | None = None,
    ) -> list[sqlite3.Row]:
        now = now or time.time()
        sql = "SELECT * FROM memory WHERE scope IN (?, 'global') AND (valid_to IS NULL OR valid_to > ?)"
        params: list[Any] = [self.scope, now]
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            sql += f" AND kind IN ({placeholders})"
            params.extend(kinds)
        if status:
            sql += " AND status = ?"
            params.append(status)
        return list(self.conn.execute(sql, params))

    def expire(self, *, now: float | None = None) -> int:
        now = now or time.time()
        cur = self.conn.execute(
            "UPDATE memory SET status='stale' WHERE status='active' AND valid_to IS NOT NULL AND valid_to <= ?",
            (now,),
        )
        self.conn.commit()
        return cur.rowcount

    def _bm25(self, query: str) -> dict[str, float]:
        terms = [t for t in query.replace('"', " ").split() if t]
        if not terms:
            return {}
        match = " OR ".join(f'"{t}"' for t in terms)
        try:
            rows = self.conn.execute(
                "SELECT id, bm25(memory_fts) AS rank FROM memory_fts WHERE memory_fts MATCH ? LIMIT 200",
                (match,),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        if not rows:
            return {}
        best = min(r["rank"] for r in rows)
        worst = max(r["rank"] for r in rows)
        span = (worst - best) or 1.0
        return {r["id"]: 1.0 - (r["rank"] - best) / span for r in rows}

    def search(
        self,
        query: str,
        *,
        kinds: Iterable[str] | None = None,
        limit: int = 6,
        max_chars: int = 900,
        now: float | None = None,
    ) -> list[Hit]:
        self.expire(now=now)
        rows = self.rows(kinds=kinds, now=now)
        if not rows:
            return []
        bm25 = self._bm25(query)
        qvec = self.embedder.embed(query)
        qlower = query.lower()
        hits: list[Hit] = []
        for row in rows:
            vec = _unpack(row["vec"])
            cos = max(0.0, cosine(qvec, vec)) if len(vec) == len(qvec) else 0.0
            lex = bm25.get(row["id"], 0.0)
            usage = min(1.0, math.log1p(row["hits"]) / 3.0)
            key_bonus = 0.0
            if qlower and (qlower in row["key"].lower() or row["key"].lower() in qlower):
                key_bonus = 0.12
            score = 0.45 * cos + 0.35 * lex + 0.12 * row["confidence"] + 0.08 * usage + key_bonus
            if score <= 0.02 and not lex:
                continue
            hits.append(
                Hit(
                    id=row["id"],
                    kind=row["kind"],
                    key=row["key"],
                    value=row["value"],
                    score=score,
                    confidence=row["confidence"],
                    visibility=row["visibility"],
                    evidence=json.loads(row["evidence"]) if row["evidence"] else None,
                    exec_spec=row["exec_spec"] or "",
                    breakdown={"cosine": round(cos, 3), "bm25": round(lex, 3)},
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)

        selected: list[Hit] = []
        used = 0
        for hit in hits[:limit]:
            cost = len(hit.key) + len(hit.value) + 12
            if used + cost > max_chars:
                break
            selected.append(hit)
            used += cost
        return selected

    def context_block(self, query: str, *, kinds: Iterable[str] | None = None, limit: int = 3, max_chars: int = 700) -> tuple[str, list[str]]:
        hits = self.search(query, kinds=kinds, limit=limit, max_chars=max_chars)
        if not hits:
            return "", []
        lines = [f"- ({h.kind}) {h.key}: {h.value}" for h in hits]
        return "\n".join(lines), [h.id for h in hits]

    # -- executable procedures -------------------------------------------
    def run_procedure(self, key: str, *, timeout: float = 120.0) -> tuple[bool, str]:
        row = self.get("procedure", key)
        if row is None or not row["exec_spec"]:
            return False, f"no executable procedure named {key!r}"
        try:
            proc = subprocess.run(
                row["exec_spec"], shell=True, cwd=str(self.root), capture_output=True,
                text=True, timeout=timeout, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return False, f"procedure {key!r} timed out after {timeout}s"
        ok = proc.returncode == 0
        self.conn.execute(
            "UPDATE memory SET confidence = MAX(0.0, MIN(1.0, confidence + ?)), updated_at = ? WHERE id = ?",
            (0.05 if ok else -0.2, time.time(), row["id"]),
        )
        self.conn.commit()
        output = (proc.stdout + proc.stderr).strip()
        return ok, output[-1500:]

    # -- evidence ---------------------------------------------------------
    def verify(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Re-run stored evidence; anything that no longer holds becomes stale."""
        results: list[dict[str, Any]] = []
        for row in self.rows(status="active", now=now):
            evidence = json.loads(row["evidence"]) if row["evidence"] else None
            if not evidence:
                continue
            ok, detail = check_evidence(evidence, self.root)
            results.append({"id": row["id"], "key": row["key"], "ok": ok, "detail": detail})
            if not ok:
                self.set_status(row["id"], "stale", confidence=max(0.0, row["confidence"] - 0.4))
        return results

    # -- reporting --------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        by_kind = {
            r["kind"]: r["n"]
            for r in self.conn.execute(
                "SELECT kind, COUNT(*) AS n FROM memory WHERE status='active' GROUP BY kind"
            )
        }
        by_status = {
            r["status"]: r["n"]
            for r in self.conn.execute("SELECT status, COUNT(*) AS n FROM memory GROUP BY status")
        }
        ops = {
            r["op"]: r["n"]
            for r in self.conn.execute("SELECT op, COUNT(*) AS n FROM memory_history GROUP BY op")
        }
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "path": str(self.path),
            "visibility": self.visibility,
            "scope": self.scope,
            "by_kind": by_kind,
            "by_status": by_status,
            "write_ops": ops,
            "total": sum(by_kind.values()),
            "bytes": size,
            "embedder": self.embedder.name,
        }

    def export_markdown(
        self,
        *,
        title: str = "Agent memory",
        tiers: Sequence[str] = ("A", "B"),
    ) -> str:
        """Render this database as an AGENTS.md-compatible section.

        `tiers` is the export gate.  Memory that is exported becomes *instructions*
        for the next agent, so by default only machine-verifiable rows (A: command,
        B: file) leave the database.  Human-attested (C) and unverified (D) rows
        must be requested explicitly.
        """
        allowed = {t.upper() for t in tiers}
        rows = [r for r in self.rows() if _tier(r) in allowed]
        facts = [r for r in rows if r["kind"] == "fact"]
        procedures = [r for r in rows if r["kind"] == "procedure"]
        decisions = [r for r in rows if r["kind"] == "decision"]

        header = (
            f"<!-- generated by planloop from {self.path.name}: "
            f"{len(rows)} entries, tiers {','.join(sorted(allowed))}; edits will be overwritten -->"
        )
        lines = [
            header,
            f"## {title}",
            "",
            "> Generated from verified project memory. Treat as reference, re-check before acting.",
            "",
        ]
        if facts:
            lines.append("### Facts")
            for row in sorted(facts, key=lambda r: r["key"]):
                lines.append(f"- **{row['key']}**: {row['value']}")
            lines.append("")
        if procedures:
            lines.append("### Procedures")
            for row in sorted(procedures, key=lambda r: r["key"]):
                cmd = f" - `{row['exec_spec']}`" if row["exec_spec"] else ""
                lines.append(f"- **{row['key']}**: {row['value']}{cmd}")
            lines.append("")
        if decisions:
            lines.append("### Decisions")
            for row in sorted(decisions, key=lambda r: r["key"]):
                lines.append(f"- **{row['key']}**: {row['value']}")
            lines.append("")
        if not rows:
            lines.append("_No verified entries yet._")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def close(self) -> None:
        self.conn.close()


def _tier(row: sqlite3.Row) -> str:
    """Evidence tier: A command, B file, C human, D none."""
    if not row["evidence"]:
        return "D"
    try:
        evidence = json.loads(row["evidence"])
    except json.JSONDecodeError:
        return "D"
    return {"command": "A", "file": "B", "manual": "C"}.get(str(evidence.get("type")), "D")


def check_evidence(evidence: dict[str, Any], root: Path) -> tuple[bool, str]:
    """Evidence must be executable, otherwise it is not evidence."""
    kind = evidence.get("type")
    if kind == "command":
        cmd = str(evidence.get("spec") or "")
        if not cmd:
            return False, "command evidence without spec"
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=str(root), capture_output=True, text=True,
                timeout=float(evidence.get("timeout", 60)), encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return False, "evidence command timed out"
        want = int(evidence.get("exit", 0))
        ok = proc.returncode == want
        return ok, f"exit={proc.returncode} (want {want})"
    if kind == "file":
        path = (root / str(evidence.get("path") or "")).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            return False, "evidence path escapes repository"
        return path.exists(), f"{path.name} exists={path.exists()}"
    if kind == "manual":
        return True, "human attested"
    return False, f"unsupported evidence type {kind!r}"


class MemorySystem:
    """Facade over the public and private databases of one repository."""

    def __init__(self, root: Path | str, *, embedder: Embedder | None = None, base: Path | str | None = None):
        self.root = Path(root).resolve()
        self.scope = repo_scope(self.root)
        base_dir = Path(base) if base else self.root / ".planloop" / "memory"
        self.embedder = embedder or default_embedder()
        self.public = MemoryDB(base_dir / "public.db", scope=self.scope, root=self.root,
                               visibility="public", embedder=self.embedder)
        self.private = MemoryDB(base_dir / "private.db", scope=self.scope, root=self.root,
                                visibility="private", embedder=self.embedder)

    def db(self, visibility: str) -> MemoryDB:
        if visibility == "public":
            return self.public
        if visibility == "private":
            return self.private
        raise ValueError(f"unknown visibility {visibility!r}")

    def remember(self, kind: str, key: str, value: str, *, public: bool = False, **kw: Any) -> str:
        return self.db("public" if public else "private").write(kind, key, value, **kw).id

    def search(self, query: str, **kw: Any) -> list[Hit]:
        merged = self.public.search(query, **kw) + self.private.search(query, **kw)
        merged.sort(key=lambda h: h.score, reverse=True)
        limit = int(kw.get("limit", 6))
        return merged[:limit]

    def context_block(self, query: str, **kw: Any) -> tuple[str, list[str]]:
        hits = self.search(query, **kw)
        if not hits:
            return "", []
        lines = [f"- ({h.kind}) {h.key}: {h.value}" for h in hits]
        return "\n".join(lines), [h.id for h in hits]

    def stats(self) -> dict[str, Any]:
        return {"public": self.public.stats(), "private": self.private.stats(), "scope": self.scope}

    def verify(self) -> dict[str, Any]:
        return {"public": self.public.verify(), "private": self.private.verify()}

    def export_markdown(self, *, include_private: bool = False, tiers: Sequence[str] = ("A", "B")) -> str:
        text = self.public.export_markdown(title=f"Project memory ({self.scope})", tiers=tiers)
        if include_private:
            text += "\n" + self.private.export_markdown(title="Private memory (do not share)", tiers=tiers)
        return text

    def exportable_count(self, *, tiers: Sequence[str] = ("A", "B")) -> dict[str, int]:
        def count(db: MemoryDB) -> int:
            allowed = {t.upper() for t in tiers}
            return sum(1 for r in db.rows() if _tier(r) in allowed)

        return {"public": count(self.public), "private": count(self.private)}

    def close(self) -> None:
        self.public.close()
        self.private.close()
