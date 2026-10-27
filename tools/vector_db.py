"""
tools/vector_db.py — SQLite-backed vector store.

One table:
  file_chunks  — indexed workspace file chunks (code, text, and recognized note
                 text under notesAppText/<slug>/0.txt — all treated uniformly)

Vectors stored as raw float32 BLOBs (numpy .tobytes() / np.frombuffer).
IDs are sha1-based.

Concurrency: one shared connection with WAL journal mode.
Writes are serialised under a threading.Lock; reads run lock-free.
"""

import hashlib
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# ── Module-level shared state ─────────────────────────────────────────────────

_conn: Optional[sqlite3.Connection] = None
_write_lock = threading.Lock()
_manifest_path: Optional[Path] = None

# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS file_chunks (
    id          TEXT PRIMARY KEY,
    file        TEXT NOT NULL,
    chunk_type  TEXT,
    start_line  INTEGER,
    chunk_count INTEGER,
    text        TEXT NOT NULL,
    vector      BLOB NOT NULL,
    mtime       REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_file_chunks_file ON file_chunks (file);
"""


# ── Init ──────────────────────────────────────────────────────────────────────

def init_vector_db(db_path: Path) -> None:
    """
    Open (or reuse) the shared SQLite connection, enable WAL, and create tables.
    Idempotent — safe to call on every build_index() run.
    """
    global _conn, _manifest_path

    db_path = Path(db_path)
    # Always (re-)set manifest path so it's never None after the first real call,
    # even if _conn was already opened by a previous init_vector_db call.
    _manifest_path = db_path.parent / "manifest.json"

    if _conn is not None:
        return  # connection already open, nothing else to do

    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_DDL)
    conn.commit()
    _conn = conn
    log.info(f"vector_db: opened {db_path}")


def _require_conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("vector_db not initialised — call init_vector_db() first")
    return _conn


# ── Manifest ──────────────────────────────────────────────────────────────────

def _write_manifest() -> None:
    """
    Write .index/manifest.json — a human-readable snapshot of everything
    currently in the DB.  Called inside _write_lock after every mutation.

    Format:
    {
      "generated_at": "<iso8601>",
      "file_chunks": {
        "<rel/path>": {"chunks": N, "last_indexed": "<iso8601>"}
        ...
      },
      "totals": {"files": N, "file_chunks": N}
    }
    """
    if _manifest_path is None or _conn is None:
        return
    try:
        # ── file_chunks: one entry per distinct file ──────────────────────────
        fc_rows = _conn.execute(
            "SELECT file, COUNT(*) as n, MAX(mtime) as latest FROM file_chunks GROUP BY file"
        ).fetchall()

        file_chunks_section: dict = {}
        for file, count, latest_mtime in fc_rows:
            # mtime is a Unix timestamp float — convert to ISO string
            try:
                iso = datetime.fromtimestamp(latest_mtime, tz=timezone.utc).isoformat()
            except Exception:
                iso = str(latest_mtime)
            file_chunks_section[file] = {"chunks": count, "last_indexed": iso}

        total_fc = sum(v["chunks"] for v in file_chunks_section.values())

        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "file_chunks": dict(sorted(file_chunks_section.items())),
            "totals": {
                "files": len(file_chunks_section),
                "file_chunks": total_fc,
            },
        }

        # Write atomically via a temp file so readers never see a partial write
        tmp = _manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        tmp.replace(_manifest_path)

    except Exception as exc:
        log.warning("vector_db: failed to write manifest: %s", exc)


# ── ID helpers ────────────────────────────────────────────────────────────────

def _chunk_id(file_path: str, chunk_name: str) -> str:
    return hashlib.sha1(f"{file_path}::{chunk_name}".encode()).hexdigest()


# ── file_chunks API ───────────────────────────────────────────────────────────

def get_file_chunk_mtimes() -> dict[str, float]:
    """
    Return {file_path: mtime} for every distinct file currently in file_chunks.
    Used by build_index() to detect changed / deleted files.
    """
    conn = _require_conn()
    rows = conn.execute("SELECT file, mtime FROM file_chunks").fetchall()
    # If a file has multiple chunks, all share the same mtime — take any.
    result: dict[str, float] = {}
    for file, mtime in rows:
        result[file] = mtime
    return result


def upsert_file_chunk(chunk: dict, vector: np.ndarray) -> None:
    """
    Insert or replace a single file chunk row.

    chunk keys: id, file, type, start_line, count, code, mtime
    """
    conn = _require_conn()
    row_id = _chunk_id(chunk["file"], chunk["id"])
    vec_blob = vector.astype(np.float32).tobytes()

    with _write_lock:
        conn.execute(
            """
            INSERT OR REPLACE INTO file_chunks
                (id, file, chunk_type, start_line, chunk_count, text, vector, mtime)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                chunk["file"],
                chunk.get("type"),
                chunk.get("start_line"),
                chunk.get("count"),
                chunk.get("code", ""),
                vec_blob,
                chunk["mtime"],
            ),
        )
        conn.commit()
        _write_manifest()


def delete_file_chunks_for_file(file_path: str) -> None:
    """Remove all chunks belonging to a given file."""
    conn = _require_conn()
    with _write_lock:
        conn.execute("DELETE FROM file_chunks WHERE file = ?", (file_path,))
        conn.commit()
        _write_manifest()


def search_file_chunks(query_vec: np.ndarray, k: int = 5) -> list[dict]:
    """
    Brute-force cosine similarity over all file_chunk vectors.
    Returns top-k dicts with keys: id, file, start_line, count, score.
    query_vec must already be L2-normalised.
    """
    conn = _require_conn()
    rows = conn.execute(
        "SELECT id, file, start_line, chunk_count, vector FROM file_chunks"
    ).fetchall()

    if not rows:
        return []

    ids, files, starts, counts, blobs = zip(*rows)
    mat = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(rows), -1)
    scores = mat @ query_vec.astype(np.float32)

    top_idx = np.argsort(scores)[::-1][:k]
    return [
        {
            "id": ids[i],
            "file": files[i],
            "start_line": starts[i],
            "count": counts[i],
            "score": float(scores[i]),
        }
        for i in top_idx
    ]


# ── note_blobs API ────────────────────────────────────────────────────────────

def drop_note_blobs_table() -> None:
    """
    Drop the retired note_blobs table, if present.

    Recognized note text is now stored in notesAppText/<slug>/0.txt and indexed
    as ordinary file_chunks, so the separate note_blobs table is obsolete. This
    is a one-time cleanup invoked by the notes-layout migration; it is a no-op
    once the table is gone.
    """
    conn = _require_conn()
    with _write_lock:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='note_blobs'"
        ).fetchone()
        if not exists:
            return
        conn.execute("DROP TABLE IF EXISTS note_blobs")
        conn.commit()
        _write_manifest()


# ── CLI smoke-test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        init_vector_db(db)
        print("Tables created:")
        conn = _require_conn()
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ):
            print(f"  {row[0]}")
        print("OK")
