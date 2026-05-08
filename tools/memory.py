import datetime
import logging
from pathlib import Path
import numpy as np
from config import get_raw_memory, save_memory
from tools.ollama import get_embeddings

log = logging.getLogger(__name__)

VECTORS_FILE = Path("aiWorkspace/aiNotes/memoryVectors.npy")

def _normalize_vector(v):
    v = np.array(v, dtype=np.float32)
    norm = np.linalg.norm(v)
    return v / norm if norm > 0 else v

def _get_best_match(query: str, agent_managed: list):
    if not agent_managed:
        return None
        
    # 1. Semantic search
    q_embeddings = get_embeddings([query])
    if q_embeddings and VECTORS_FILE.exists():
        q_vec = _normalize_vector(q_embeddings[0])
        try:
            vectors = np.load(VECTORS_FILE)
            if vectors.shape[0] == len(agent_managed):
                scores = np.dot(vectors, q_vec)
                best_idx = int(np.argmax(scores))
                if scores[best_idx] >= 0.75:
                    log.info(f"Semantic match found at idx {best_idx} with score {scores[best_idx]:.3f}")
                    return best_idx
        except Exception as e:
            log.error(f"Failed to load memory vectors: {e}")

    # 2. Substring fallback
    query_lower = query.lower()
    for i, slot in enumerate(agent_managed):
        if query_lower in slot["content"].lower():
            log.info(f"Substring match found at idx {i}")
            return i
            
    return None

def _sync_vectors(agent_managed, new_emb=None, target_idx=None, action="rebuild"):
    """
    Keep VECTORS_FILE in sync with agent_managed list.
    action can be: 'rebuild', 'add', 'delete', 'update'
    """
    if not agent_managed and action != "add":
        if VECTORS_FILE.exists(): VECTORS_FILE.unlink()
        return

    try:
        if action == "rebuild" or not VECTORS_FILE.exists():
            contents = [s["content"] for s in agent_managed]
            embs = get_embeddings(contents)
            if embs:
                vectors = np.array([_normalize_vector(e) for e in embs])
                np.save(VECTORS_FILE, vectors)
            return

        vectors = np.load(VECTORS_FILE)
        
        if action == "add" and new_emb and target_idx is not None:
            new_vec = _normalize_vector(new_emb)
            if vectors.shape[0] == len(agent_managed) - 1:
                vectors = np.insert(vectors, target_idx, new_vec, axis=0)
                np.save(VECTORS_FILE, vectors)
            else:
                _sync_vectors(agent_managed, action="rebuild")

        elif action == "delete" and target_idx is not None:
            if vectors.shape[0] == len(agent_managed) + 1:
                vectors = np.delete(vectors, target_idx, axis=0)
                np.save(VECTORS_FILE, vectors)
            else:
                _sync_vectors(agent_managed, action="rebuild")

        elif action == "update" and new_emb and target_idx is not None:
            if target_idx < vectors.shape[0]:
                vectors[target_idx] = _normalize_vector(new_emb)
                np.save(VECTORS_FILE, vectors)
            else:
                _sync_vectors(agent_managed, action="rebuild")

    except Exception as e:
        log.error(f"Vector sync failed: {e}")
        # Fallback to rebuild
        try:
            contents = [s["content"] for s in agent_managed]
            embs = get_embeddings(contents)
            if embs:
                vectors = np.array([_normalize_vector(e) for e in embs])
                np.save(VECTORS_FILE, vectors)
        except:
            pass

async def _manage_memory(action: str, content: str = None, new_content: str = None):
    data = get_raw_memory()
    agent_managed = data.get("agent_managed", [])

    if action == "write":
        if not content:
            return "Error: 'content' is required for 'write' action."
        if len(agent_managed) >= 10:
            return "Error: Memory is full (10/10). Please delete an existing memory first."
        
        existing_indices = {s["index"] for s in agent_managed}
        new_index = 0
        for i in range(10):
            if i not in existing_indices:
                new_index = i
                break
        
        agent_managed.append({
            "index": new_index,
            "content": content,
            "created": datetime.datetime.now().isoformat()
        })
        agent_managed.sort(key=lambda x: x["index"])
        data["agent_managed"] = agent_managed
        save_memory(data)
        
        # Find where it landed in the list
        target_idx = next(i for i, s in enumerate(agent_managed) if s["index"] == new_index)
        
        new_emb = get_embeddings([content])
        if new_emb:
            _sync_vectors(agent_managed, new_emb=new_emb[0], target_idx=target_idx, action="add")
        else:
            _sync_vectors(agent_managed, action="rebuild")
            
        return "Successfully saved memory."

    elif action == "delete":
        if not content:
            return "Error: 'content' (search target) is required for 'delete' action."
        
        match_idx = _get_best_match(content, agent_managed)
        if match_idx is None:
            return "Error: No matching memory found to delete."
        
        matched_content = agent_managed[match_idx]["content"]
        agent_managed.pop(match_idx)
        data["agent_managed"] = agent_managed
        save_memory(data)
        
        _sync_vectors(agent_managed, target_idx=match_idx, action="delete")
        return f"Successfully deleted memory: '{matched_content}'"

    elif action == "edit":
        if not content or not new_content:
            return "Error: both 'content' (target) and 'new_content' are required for 'edit' action."
        
        match_idx = _get_best_match(content, agent_managed)
        if match_idx is None:
            return "Error: No matching memory found to edit."
        
        old_content = agent_managed[match_idx]["content"]
        agent_managed[match_idx]["content"] = new_content
        agent_managed[match_idx]["created"] = datetime.datetime.now().isoformat()
        
        data["agent_managed"] = agent_managed
        save_memory(data)
        
        # Optimized update for edit
        new_emb = get_embeddings([new_content])
        if new_emb:
            _sync_vectors(agent_managed, new_emb=new_emb[0], target_idx=match_idx, action="update")
        else:
            _sync_vectors(agent_managed, action="rebuild")
            
        return f"Successfully updated memory from '{old_content}' to '{new_content}'"

    elif action == "clear_all":
        data["agent_managed"] = []
        save_memory(data)
        if VECTORS_FILE.exists():
            VECTORS_FILE.unlink()
        return "Successfully cleared all agent-managed memories."

    else:
        return f"Error: Unknown action '{action}'."
