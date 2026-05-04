import json
import logging
import numpy as np
from tools.index import CHUNKS_FILE, VECTORS_FILE, build_index
from tools.ollama import get_embeddings
from mcp import types

log = logging.getLogger(__name__)

def search_semantic(query: str, k: int = 5, mode: str = "code"):
    """
    Perform semantic search against the index.
    Lazy builds/checks index on call.
    """
    # Lazy check/build
    build_index()

    if not CHUNKS_FILE.exists() or not VECTORS_FILE.exists():
        log.error("Index files missing.")
        return []

    # Load fresh every time
    try:
        with open(CHUNKS_FILE, "r") as f:
            data = json.load(f)
        chunks = data.get("chunks", [])
        vectors = np.load(VECTORS_FILE)
    except Exception as e:
        log.error(f"Failed to load index: {e}")
        return []

    if not chunks or vectors.shape[0] == 0:
        return []

    # Get query embedding
    q_embeddings = get_embeddings([query])
    if not q_embeddings:
        log.error("Failed to embed query.")
        return []
    
    q_vec = np.array(q_embeddings[0], dtype=np.float32)
    q_norm = np.linalg.norm(q_vec)
    if q_norm > 0:
        q_vec = q_vec / q_norm

    # Dot product (since both are normalized)
    scores = np.dot(vectors, q_vec)

    # Get top K
    top_indices = np.argsort(scores)[::-1][:k]
    
    results = []
    for idx in top_indices:
        chunk = chunks[idx]
        results.append({
            "id": chunk["id"],
            "file": chunk["file"],
            "start_line": chunk["start_line"],
            "count": chunk["count"],
            "score": float(scores[idx])
        })
        
    return results
async def _search_semantic(query: str, k: int = 5) -> list[types.TextContent]:
    """MCP wrapper for semantic search."""
    results = search_semantic(query, k=k)
    if not results:
        return [types.TextContent(type="text", text="No semantic matches found.")]
    
    lines = [f"Conceptual matches for '{query}':\n"]
    for r in results:
        lines.append(f"- {r['file']}:{r['start_line']} (score: {r['score']:.3f})")
        
    return [types.TextContent(type="text", text="\n".join(lines))]

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        query = sys.argv[1]
        results = search_semantic(query)
        print(f"{'score':<8} | {'location':<30} | {'id'}")
        print("-" * 60)
        for r in results:
            loc = f"{r['file']}:{r['start_line']}"
            print(f"{r['score']:.4f} | {loc:<30} | {r['id']}")
    else:
        print("Usage: python -m tools.search \"query\"")
