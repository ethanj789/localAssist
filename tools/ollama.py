import httpx
import logging

log = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/embed"
DEFAULT_MODEL = "mxbai-embed-large"

def get_embeddings(inputs: list[str], model: str = DEFAULT_MODEL, retry: bool = True) -> list[list[float]] | None:
    """
    Fetch embeddings from Ollama using httpx.
    Always batches inputs.
    """
    if not inputs:
        return []

    payload = {
        "model": model,
        "input": inputs
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(OLLAMA_URL, json=payload)
            response.raise_for_status()
        data = response.json()
        embeddings = data.get("embeddings")
        
        if not embeddings:
            return None
            
        # Basic validation
        dim = len(embeddings[0])
        if dim == 0:
            log.error("Ollama returned empty embedding vectors.")
            return None
            
        for i, v in enumerate(embeddings):
            if len(v) != dim:
                log.error(f"Ollama returned inconsistent embedding dimensions at index {i}.")
                return None
                
        return embeddings
    except Exception as e:
        log.error(f"Ollama embedding failed: {e}")
        if retry:
            log.info("Retrying Ollama embedding...")
            return get_embeddings(inputs, model, retry=False)
        return None
