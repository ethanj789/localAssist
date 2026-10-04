"""
tools/embeddings.py — Text embedding via sentence-transformers (Hugging Face).
Loads a SentenceTransformer model once at module level and batches inputs.
(Formerly ollama.py — embeddings no longer come from Ollama.)
"""
import logging
from sentence_transformers import SentenceTransformer

log = logging.getLogger(__name__)

DEFAULT_MODEL = "mixedbread-ai/mxbai-embed-large-v1"

# Load once at module level — same pattern as your WhisperModel
_model: SentenceTransformer | None = None

def _get_model(model_name: str) -> SentenceTransformer:
    global _model
    if _model is None:
        log.info(f"Loading embedding model: {model_name}")
        _model = SentenceTransformer(model_name)
    return _model


def get_embeddings(inputs: list[str], model: str = DEFAULT_MODEL, retry: bool = True) -> list[list[float]] | None:
    """
    Fetch embeddings using sentence-transformers.
    Always batches inputs.
    """
    if not inputs:
        return []

    try:
        m = _get_model(model)
        vectors = m.encode(inputs, batch_size=32, show_progress_bar=False)
        embeddings = [v.tolist() for v in vectors]

        # Same validation as before
        dim = len(embeddings[0])
        if dim == 0:
            log.error("Embedding model returned empty vectors.")
            return None

        for i, v in enumerate(embeddings):
            if len(v) != dim:
                log.error(f"Inconsistent embedding dimensions at index {i}.")
                return None

        return embeddings

    except Exception as e:
        log.error(f"Embedding failed: {e}")
        if retry:
            log.info("Retrying embedding...")
            return get_embeddings(inputs, model, retry=False)
        return None