import logging
import numpy as np
from tools.index import build_index, DB_FILE
from tools.ollama import get_embeddings
import tools.vector_db as vector_db
from mcp import types

log = logging.getLogger(__name__)


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
            # note_blob result
            label = f"[note: {r['page_id']} / blob {r['blob_idx']}]"
            lines.append(f"- {label} (score: {r['score']:.3f})")
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
