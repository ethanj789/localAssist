import os
import json
import time
import logging
import numpy as np
from pathlib import Path
from tools.files import get_file_chunks, WORKSPACE_ROOT, SKIP_DIRS
from tools.embeddings import get_embeddings
import tools.vector_db as vector_db

# Recognized note text lives here. It is NOT in SKIP_DIRS: each <slug>/0.txt is
# indexed as an ordinary workspace file (mtime-gated), not via a separate table.
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


# ── One-time notes-layout migration ───────────────────────────────────────────

def migrate_notes_layout() -> None:
    """
    Migrate the notesAppText tree from the old blob layout to the clean one.

    Old layout per page dir: 0.txt + 0.hash + .page_meta.json, indexed into a
    separate note_blobs table. New layout: 0.txt only, indexed like any other
    workspace file; the page_id/slug/hash bookkeeping lives in the notes app's
    private recognition map (_recognition.json).

    This pass is idempotent and does three things:
      1. Backfill the recognition map from each dir's legacy sidecars (so the
         next recognition run doesn't needlessly re-OCR every page), but only
         for pages whose canonical note data still exists.
      2. Garbage-collect orphaned text dirs — any notesAppText/<slug>/ whose
         page_id no longer has a live note (e.g. a page deleted in the UI under
         the old buggy delete that left this folder behind).
      3. Strip legacy .hash / .page_meta.json sidecars from surviving dirs so
         the agent-visible tree holds nothing but 0.txt.

    Also drops the retired note_blobs table rows once, since those blobs are now
    represented as ordinary file_chunks.
    """
    if not NOTES_TEXT_DIR.exists():
        _drop_legacy_note_blobs()
        return

    import shutil
    from tool_apps.notes import recognition_state

    # page_id -> page_type for every live note on disk.
    live_pages: dict[str, str] = {}
    notes_data_root = Path(__file__).resolve().parent.parent / "tool_apps" / "notes" / "data"
    for page_type in ("notes", "art"):
        type_dir = notes_data_root / page_type
        if not type_dir.is_dir():
            continue
        for page_dir in type_dir.iterdir():
            if page_dir.is_dir() and (page_dir / "meta.json").exists():
                live_pages[page_dir.name] = page_type

    # slug -> (page_id, page_type) from the recognition maps. This is the
    # authoritative owner lookup AFTER migration (once sidecars are stripped),
    # so a clean dir is never mistaken for an orphan on a later pass.
    slug_to_page: dict[str, tuple[str, str]] = {}
    for page_type in ("notes", "art"):
        for pid, entry in recognition_state.load_map(page_type).items():
            slug = entry.get("slug")
            if slug:
                slug_to_page[slug] = (pid, page_type)

    removed_orphans = 0
    stripped = 0
    backfilled = 0

    for slug_dir in sorted(NOTES_TEXT_DIR.iterdir()):
        if not slug_dir.is_dir():
            continue

        # Resolve the owning page_id from the legacy sidecar first (old layout),
        # then fall back to the recognition map keyed by this dir's slug (new
        # layout). Only when NEITHER resolves to a live note is it an orphan.
        page_id = None
        page_type = None
        sidecar = slug_dir / ".page_meta.json"
        if sidecar.exists():
            try:
                page_id = json.loads(sidecar.read_text(encoding="utf-8")).get("page_id")
            except Exception:
                page_id = None
            if page_id in live_pages:
                page_type = live_pages[page_id]
            else:
                page_id = None

        if page_id is None:
            mapped = slug_to_page.get(slug_dir.name)
            if mapped and mapped[0] in live_pages:
                page_id, page_type = mapped

        # Genuine orphan — its note no longer exists. Remove the stale text dir.
        if page_id is None or page_type is None:
            try:
                shutil.rmtree(slug_dir, ignore_errors=True)
                removed_orphans += 1
                log.info("migrate_notes_layout: removed orphan text dir %s", slug_dir.name)
            except Exception as exc:
                log.warning("migrate_notes_layout: failed removing %s: %s", slug_dir.name, exc)
            continue

        # Surviving page: backfill map (slug + legacy source hash) if absent.
        if recognition_state.get_entry(page_type, page_id) is None:
            legacy_hash = ""
            hash_file = slug_dir / "0.hash"
            if hash_file.exists():
                try:
                    legacy_hash = hash_file.read_text(encoding="utf-8").strip()
                except OSError:
                    legacy_hash = ""
            recognition_state.set_entry(page_type, page_id, slug_dir.name, legacy_hash)
            backfilled += 1

        # Strip legacy sidecars so only 0.txt remains in the clean tree.
        for legacy in (*slug_dir.glob("*.hash"), sidecar):
            try:
                legacy.unlink()
                stripped += 1
            except OSError:
                pass

    _drop_legacy_note_blobs()

    if removed_orphans or stripped or backfilled:
        log.info(
            "migrate_notes_layout: %d orphan dir(s) removed, %d sidecar(s) stripped, %d map entr(ies) backfilled.",
            removed_orphans, stripped, backfilled,
        )


def _drop_legacy_note_blobs() -> None:
    """Drop the retired note_blobs table (its rows are now file_chunks)."""
    try:
        vector_db.drop_note_blobs_table()
    except Exception as exc:
        log.debug("migrate_notes_layout: note_blobs drop skipped: %s", exc)


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
    # Clean the notes text tree (orphan GC + sidecar strip) BEFORE the walk so
    # orphaned note dirs are never indexed as files. Idempotent.
    migrate_notes_layout()

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


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        build_index()
    else:
        print("Usage: python -m tools.index build")
