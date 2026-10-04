import os
import json
import time
import logging
import numpy as np
from datetime import datetime
from pathlib import Path
from tools.files import get_file_chunks, WORKSPACE_ROOT, SKIP_DIRS
from tools.ollama import get_embeddings
import tools.vector_db as vector_db

# OCR blob output lives here — included in indexing, not in SKIP_DIRS
NOTES_TEXT_DIR = WORKSPACE_ROOT / "notesAppText"

log = logging.getLogger(__name__)

INDEX_DIR = WORKSPACE_ROOT / ".index"
DB_FILE = INDEX_DIR / "vectors.db"
BUSY_LOCK_FILE = INDEX_DIR / "indexing.lock"

# Kept temporarily for one-time migration — removed once old files are gone
# TODO: remove after migration
CHUNKS_FILE = INDEX_DIR / "chunks.json"
VECTORS_FILE = INDEX_DIR / "vectors.npy"


def _migrate_legacy_index() -> None:
    """
    One-time migration: if the old chunks.json + vectors.npy exist and
    file_chunks table is empty, bulk-insert all rows into the DB, then
    delete both old files on success.
    """
    if not CHUNKS_FILE.exists() or not VECTORS_FILE.exists():
        return

    existing_mtimes = vector_db.get_file_chunk_mtimes()
    if existing_mtimes:
        # Table already has data — old files are stale leftovers, just remove them
        log.info("Legacy index files found but DB already populated — removing old files.")
        _delete_legacy_files()
        return

    log.info("Migrating legacy .npy/chunks.json index into vector DB...")
    try:
        with open(CHUNKS_FILE, "r") as f:
            stored_data = json.load(f)
        stored_chunks = stored_data.get("chunks", [])
        stored_vectors = np.load(VECTORS_FILE)

        if len(stored_chunks) != stored_vectors.shape[0]:
            log.warning("Legacy index shape mismatch — skipping migration, will rebuild.")
            return

        stored_files_meta = stored_data.get("meta", {}).get("files", {})

        migrated = 0
        for chunk, vec in zip(stored_chunks, stored_vectors):
            file_path = chunk.get("file", "")
            mtime = stored_files_meta.get(file_path, {}).get("mtime", 0.0)
            enriched = {**chunk, "mtime": mtime}
            vector_db.upsert_file_chunk(enriched, vec)
            migrated += 1

        log.info(f"Migration complete: {migrated} chunks inserted.")
        _delete_legacy_files()

    except Exception as e:
        log.warning(f"Legacy index migration failed: {e} — will rebuild from scratch.")


def _delete_legacy_files() -> None:
    for path in (CHUNKS_FILE, VECTORS_FILE):
        try:
            path.unlink()
            log.info(f"Deleted legacy index file: {path.name}")
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning(f"Could not delete {path.name}: {e}")


# ── Note blob scanning ────────────────────────────────────────────────────────

def _read_blob_file(txt_path: Path) -> dict | None:
    """
    Read a headerless blob .txt file and its .hash sidecar.

    The output directory contains a .page_meta.json sidecar with the real page_id.
    blob_idx is inferred from the filename stem (0.txt → 0).

    Returns a dict with keys: page_id, blob_idx, content_hash, text.
    Returns None if the file can't be read or parsed.
    """
    try:
        text = txt_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None

    # Read hash sidecar
    hash_path = txt_path.with_suffix(".hash")
    try:
        content_hash = hash_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None  # no hash = not a pipeline-produced file

    # Infer blob_idx from filename
    try:
        blob_idx = int(txt_path.stem)
    except ValueError:
        return None

    # Read page_id from .page_meta.json sidecar in same directory
    meta_sidecar = txt_path.parent / ".page_meta.json"
    try:
        page_id = json.loads(meta_sidecar.read_text(encoding="utf-8")).get("page_id", "")
    except Exception:
        page_id = txt_path.parent.name  # fallback: directory name

    if not page_id:
        return None

    return {
        "page_id": page_id,
        "blob_idx": blob_idx,
        "content_hash": content_hash,
        "text": text,
    }


def scan_note_blobs() -> None:
    """
    Walk notesAppText/**/*.txt, read plain text + .hash sidecars, and upsert
    changed blobs into the note_blobs table.
    Removes DB rows for page_ids whose output directories are gone.
    """
    if not NOTES_TEXT_DIR.exists():
        return

    stored_hashes = vector_db.get_note_blob_hashes()
    seen_page_ids: set[str] = set()
    seen_blob_ids: set[str] = set()
    upserted = skipped = 0

    for page_dir in sorted(NOTES_TEXT_DIR.iterdir()):
        if not page_dir.is_dir():
            continue

        for txt_file in sorted(page_dir.glob("*.txt")):
            parsed = _read_blob_file(txt_file)
            if parsed is None:
                log.debug("scan_note_blobs: skipping unrecognised file %s", txt_file)
                continue

            seen_page_ids.add(parsed["page_id"])
            blob_id = f"{parsed['page_id']}__{parsed['blob_idx']}"
            seen_blob_ids.add(blob_id)

            if stored_hashes.get(blob_id) == parsed["content_hash"]:
                skipped += 1
                continue

            text = parsed["text"]
            if not text.strip():
                log.debug("scan_note_blobs: empty text in %s, skipping embed.", txt_file)
                continue

            embeddings = get_embeddings([text])
            if not embeddings:
                log.error("scan_note_blobs: failed to embed blob %s", blob_id)
                continue

            vec  = np.array(embeddings[0], dtype=np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm

            blob_row = {
                "id":           blob_id,
                "page_id":      parsed["page_id"],
                "blob_idx":     parsed["blob_idx"],
                "bbox":         "{}",   # no bbox in new format; placeholder
                "content_hash": parsed["content_hash"],
                "text":         text,
                "updated_at":   datetime.utcnow().isoformat(),
            }
            vector_db.upsert_note_blob(blob_row, vec)
            upserted += 1

    # Remove stale rows. Two cases:
    #   1. whole page gone  — delete every row for that page_id
    #   2. page present but a specific blob file gone (e.g. the old multi-blob
    #      pipeline left 1.txt behind and Ink now writes only 0.txt) — delete
    #      just that orphaned blob row.
    all_stored = vector_db.get_note_blob_hashes()
    stored_page_ids: set[str] = set()
    for blob_id in all_stored:
        if "__" in blob_id:
            stored_page_ids.add(blob_id.split("__")[0])

    removed_pages = 0
    removed_blobs = 0
    for gone_page_id in stored_page_ids - seen_page_ids:
        vector_db.delete_note_blobs_for_page(gone_page_id)
        removed_pages += 1

    for stale_blob_id in set(all_stored) - seen_blob_ids:
        # Skip blobs belonging to a page we already deleted wholesale above.
        page_id = stale_blob_id.split("__")[0] if "__" in stale_blob_id else stale_blob_id
        if page_id not in seen_page_ids:
            continue
        vector_db.delete_note_blob(stale_blob_id)
        removed_blobs += 1

    if upserted or skipped or removed_pages or removed_blobs:
        log.info(
            "scan_note_blobs: %d upserted, %d unchanged, %d page(s) removed, %d stale blob(s) removed.",
            upserted, skipped, removed_pages, removed_blobs,
        )


def build_index(force: bool = False) -> None:
    """
    Builds the semantic index incrementally.
    - init_vector_db (idempotent)
    - run one-time legacy migration if needed
    - walk workspace, compare mtimes
    - embed changed files, upsert into DB
    - delete DB rows for removed files
    """
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    vector_db.init_vector_db(DB_FILE)
    _migrate_legacy_index()

    # ── Discover current files ────────────────────────────────────────────────
    current_files: dict[str, float] = {}
    all_rel_paths: list[str] = []

    for root, dirs, files in os.walk(WORKSPACE_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            abs_path = Path(root) / f
            rel_path = str(abs_path.relative_to(WORKSPACE_ROOT)).replace("\\", "/")
            all_rel_paths.append(rel_path)

    all_rel_paths.sort()

    for rel_path in all_rel_paths:
        abs_path = WORKSPACE_ROOT / rel_path
        try:
            current_files[rel_path] = abs_path.stat().st_mtime
        except FileNotFoundError:
            continue

    # ── Compare against stored mtimes ─────────────────────────────────────────
    stored_mtimes = {} if force else vector_db.get_file_chunk_mtimes()

    needs_update: list[str] = []
    if force:
        needs_update = list(current_files.keys())
        # Wipe everything on force rebuild
        for file_path in stored_mtimes:
            vector_db.delete_file_chunks_for_file(file_path)
    else:
        for rel_path in all_rel_paths:
            stored = stored_mtimes.get(rel_path)
            if stored is None or stored != current_files.get(rel_path):
                needs_update.append(rel_path)

    deleted_files = [p for p in stored_mtimes if p not in current_files]

    if not needs_update and not deleted_files:
        log.info("Index is up to date.")
        return

    # ── Remove deleted files ──────────────────────────────────────────────────
    for file_path in deleted_files:
        log.info(f"Removing index entries for deleted file: {file_path}")
        vector_db.delete_file_chunks_for_file(file_path)

    # ── Mark as busy ──────────────────────────────────────────────────────────
    with open(BUSY_LOCK_FILE, "w") as f:
        f.write(str(time.time()))

    try:
        log.info(f"Indexing {len(needs_update)} changed file(s)...")
        start_time = time.time()
        indexed_files = 0
        indexed_chunks = 0

        for rel_path in needs_update:
            # Drop existing chunks for this file before reinserting
            vector_db.delete_file_chunks_for_file(rel_path)

            chunks = get_file_chunks(rel_path)
            if not chunks:
                continue

            log.info(f"Processing {rel_path} ({len(chunks)} chunks)")
            texts = [c["code"] for c in chunks]
            embeddings = get_embeddings(texts)

            if embeddings is None:
                log.error(f"Failed to get embeddings for {rel_path}. Skipping.")
                continue

            if len(embeddings) != len(chunks):
                log.error(f"Embedding count mismatch for {rel_path}. Skipping.")
                continue

            mtime = current_files.get(rel_path, 0.0)
            for chunk, raw_vec in zip(chunks, embeddings):
                v = np.array(raw_vec, dtype=np.float32)
                norm = np.linalg.norm(v)
                if norm > 0:
                    v = v / norm
                enriched = {**chunk, "mtime": mtime}
                vector_db.upsert_file_chunk(enriched, v)
                indexed_chunks += 1

            indexed_files += 1

        duration = time.time() - start_time
        log.info(
            f"Index updated: {indexed_files} files, {indexed_chunks} chunks in {duration:.2f}s"
        )
        print(f"Index updated:")
        print(f"  Files processed: {indexed_files}")
        print(f"  Chunks upserted: {indexed_chunks}")
        print(f"  Time: {duration:.2f}s")

    finally:
        if BUSY_LOCK_FILE.exists():
            BUSY_LOCK_FILE.unlink()

    # ── Scan OCR'd note blobs ─────────────────────────────────────────────────
    scan_note_blobs()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        build_index()
    else:
        print("Usage: python -m tools.index build")
