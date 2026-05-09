import os
import json
import time
import logging
import numpy as np
from pathlib import Path
from tools.files import get_file_chunks, WORKSPACE_ROOT, SKIP_DIRS
from tools.ollama import get_embeddings

log = logging.getLogger(__name__)

INDEX_DIR = WORKSPACE_ROOT / ".index"
CHUNKS_FILE = INDEX_DIR / "chunks.json"
VECTORS_FILE = INDEX_DIR / "vectors.npy"
BUSY_LOCK_FILE = INDEX_DIR / "indexing.lock"

def build_index(force=False):
    """
    Builds the semantic index.
    Checks mtimes and rebuilds if any file changed.
    """
    if not INDEX_DIR.exists():
        INDEX_DIR.mkdir(parents=True, exist_ok=True)

    # TODO: add dirty flag to skip walk if recently checked
    current_files = {}
    all_rel_paths = []
    
    # Deterministic file discovery (excluding .index itself which is in SKIP_DIRS)
    for root, dirs, files in os.walk(WORKSPACE_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        # We discover files to check mtimes
        # but we need to discover files here to check mtimes.
        for f in files:
            abs_path = Path(root) / f
            rel_path = str(abs_path.relative_to(WORKSPACE_ROOT)).replace("\\", "/")
            all_rel_paths.append(rel_path)

    all_rel_paths.sort()
    
    for rel_path in all_rel_paths:
        abs_path = WORKSPACE_ROOT / rel_path
        try:
            mtime = abs_path.stat().st_mtime
            current_files[rel_path] = {"mtime": mtime}
        except FileNotFoundError:
            continue

    # Check what needs to be updated
    stored_data = {}
    stored_files = {}
    if not force and CHUNKS_FILE.exists():
        try:
            with open(CHUNKS_FILE, "r") as f:
                stored_data = json.load(f)
                stored_files = stored_data.get("meta", {}).get("files", {})
        except Exception as e:
            log.warning(f"Failed to load existing index: {e}. Rebuilding.")

    unmodified_files = set()
    if not force:
        for p in current_files:
            if p in stored_files and stored_files[p]["mtime"] == current_files[p]["mtime"]:
                unmodified_files.add(p)

    needs_update = [p for p in all_rel_paths if p not in unmodified_files]
    deleted_files = [p for p in stored_files if p not in current_files]

    if not force and not needs_update and not deleted_files:
        log.info("Index is up to date.")
        return stored_data

    # Mark as busy
    with open(BUSY_LOCK_FILE, "w") as f:
        f.write(str(time.time()))
        
    try:
        log.info("Rebuilding index incrementally...")
        all_chunks = []
        all_vectors = []
        start_time = time.time()

        # Retain chunks and vectors for unmodified files
        if unmodified_files and CHUNKS_FILE.exists() and VECTORS_FILE.exists():
            try:
                stored_chunks = stored_data.get("chunks", [])
                stored_vectors = np.load(VECTORS_FILE)
                if len(stored_chunks) == stored_vectors.shape[0]:
                    for chunk, vec in zip(stored_chunks, stored_vectors):
                        if chunk.get("file") in unmodified_files:
                            all_chunks.append(chunk)
                            all_vectors.append(vec)
                else:
                    log.warning("Shape mismatch in stored chunks vs vectors. Rebuilding all.")
                    needs_update = all_rel_paths
                    all_chunks = []
                    all_vectors = []
            except Exception as e:
                log.warning(f"Failed to load existing vectors/chunks for incremental build: {e}. Rebuilding all.")
                needs_update = all_rel_paths
                all_chunks = []
                all_vectors = []

        for rel_path in needs_update:
            chunks = get_file_chunks(rel_path)
            if not chunks:
                continue
                
            log.info(f"Processing {rel_path} ({len(chunks)} chunks)")
            
            # Batch embedding for the file
            texts = [c["code"] for c in chunks]
            embeddings = get_embeddings(texts)
            
            if embeddings is None:
                log.error(f"Failed to get embeddings for {rel_path}. Skipping file.")
                continue
                
            if len(embeddings) != len(chunks):
                log.error(f"Embedding count mismatch for {rel_path}. Skipping file.")
                continue
                
            # Add to collection
            for i, chunk in enumerate(chunks):
                all_chunks.append(chunk)
                # Normalize vector now for fast dot product similarity later
                v = np.array(embeddings[i], dtype=np.float32)
                norm = np.linalg.norm(v)
                if norm > 0:
                    v = v / norm
                all_vectors.append(v)

        if not all_chunks:
            log.warning("No chunks found to index.")
            return None

        # Save
        data = {
            "meta": {
                "built_at": int(time.time()),
                "files": current_files
            },
            "chunks": all_chunks
        }
        
        with open(CHUNKS_FILE, "w") as f:
            json.dump(data, f, indent=2)
            
        np.save(VECTORS_FILE, np.array(all_vectors, dtype=np.float32))
        
        duration = time.time() - start_time
        print(f"Index built:")
        print(f"  Files: {len(current_files)}")
        print(f"  Chunks: {len(all_chunks)}")
        print(f"  Time: {duration:.2f}s")
        
        return data
    finally:
        if BUSY_LOCK_FILE.exists():
            BUSY_LOCK_FILE.unlink()

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        build_index()
    else:
        print("Usage: python -m tools.index build")
