import json
import logging
import numpy as np
from pathlib import Path
from tools.index import build_index, DB_FILE, NOTES_TEXT_DIR
from tools.embeddings import get_embeddings
import tools.vector_db as vector_db
from mcp import types

log = logging.getLogger(__name__)


def _resolve_note_slug(page_id: str) -> str | None:
    """Find the notesAppText subdirectory slug for a given page_id."""
    if not NOTES_TEXT_DIR.exists():
        return None
    for page_dir in NOTES_TEXT_DIR.iterdir():
        if not page_dir.is_dir():
            continue
        meta_file = page_dir / ".page_meta.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                if meta.get("page_id") == page_id:
                    return page_dir.name
            except (json.JSONDecodeError, OSError):
                continue
    return None


def search_semantic(query: str, k: int = 5, mode: str = "code") -> list[dict]:
    """
    Perform semantic search against the vector DB.
    Lazy-triggers build_index() on each call (idempotent / mtime-gated).
    Returns merged results from file_chunks and note_blobs, sorted by score.
    """
    build_index()

    # Embed query
    q_embeddings = get_embeddings([query])
    if not q_embeddings:
        log.error("Failed to embed query.")
        return []

    q_vec = np.array(q_embeddings[0], dtype=np.float32)
    q_norm = np.linalg.norm(q_vec)
    if q_norm > 0:
        q_vec = q_vec / q_norm

    # Search both tables, merge, re-sort
    file_results = vector_db.search_file_chunks(q_vec, k=k)
    blob_results = vector_db.search_note_blobs(q_vec, k=k)

    merged = file_results + blob_results
    merged.sort(key=lambda r: r["score"], reverse=True)
    return merged[:k]


async def _search_semantic(query: str, k: int = 5) -> list[types.TextContent]:
    """MCP wrapper for semantic search."""
    results = search_semantic(query, k=k)
    if not results:
        return [types.TextContent(type="text", text="No semantic matches found.")]

    lines = [f"Conceptual matches for '{query}':\n"]
    for r in results:
        if "page_id" in r:
            # note_blob result — include the readable path for the agent
            slug = _resolve_note_slug(r['page_id'])
            if slug:
                readable_path = f"notesAppText/{slug}/{r['blob_idx']}.txt"
                lines.append(
                    f"- [note: {r['page_id']} / blob {r['blob_idx']}] → "
                    f"read with path: \"{readable_path}\" (score: {r['score']:.3f})"
                )
            else:
                lines.append(f"- [note: {r['page_id']} / blob {r['blob_idx']}] (score: {r['score']:.3f})")
        else:
            # file_chunk result
            lines.append(f"- {r['file']}:{r['start_line']} (score: {r['score']:.3f})")

    return [types.TextContent(type="text", text="\n".join(lines))]


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        query = sys.argv[1]
        results = search_semantic(query)
        print(f"{'score':<8} | {'location':<40} | {'id'}")
        print("-" * 70)
        for r in results:
            if "page_id" in r:
                loc = f"[note: {r['page_id']} / blob {r['blob_idx']}]"
            else:
                loc = f"{r['file']}:{r['start_line']}"
            print(f"{r['score']:.4f} | {loc:<40} | {r['id']}")
    else:
        print("Usage: python -m tools.search \"query\"")
