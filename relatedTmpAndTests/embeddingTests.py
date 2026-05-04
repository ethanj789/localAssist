import time
import math
import requests
from typing import List, Dict, Tuple

OLLAMA_URL = "http://localhost:11434/api/embeddings"

# ---- configure models you want to compare ----
MODELS = [
    "nomic-embed-text-v2-moe",
    "mxbai-embed-large",
    # add more here
]

# ---- test corpus (mix of natural text + code) ----
CORPUS = [
    "How to cook a perfect medium rare steak with butter and garlic",
    "Best ways to optimize Python performance for large datasets",
    "The quick brown fox jumps over the lazy dog",
    "JavaScript async await example with error handling",
    "def binary_search(arr, target):\n    lo, hi = 0, len(arr)-1\n    while lo <= hi:\n        mid = (lo+hi)//2\n        if arr[mid] == target:\n            return mid\n        elif arr[mid] < target:\n            lo = mid+1\n        else:\n            hi = mid-1\n    return -1",
    "SELECT * FROM users WHERE email LIKE '%@gmail.com';",
    "Machine learning models can overfit when trained on small datasets",
    "How to center a div using CSS flexbox",
        # --- strong HIT (should rank very high) ---
    "def linear_search(arr, target):\n    for i, x in enumerate(arr):\n        if x == target:\n            return i\n    return -1",

    # --- near HIT (related but different algo) ---
    "def binary_search(arr, target):\n    lo, hi = 0, len(arr)-1\n    while lo <= hi:\n        mid = (lo+hi)//2\n        if arr[mid] == target:\n            return mid\n        elif arr[mid] < target:\n            lo = mid+1\n        else:\n            hi = mid-1\n    return -1",

    # --- partial match (search but different language) ---
    "function linearSearch(arr, target) {\n  for (let i = 0; i < arr.length; i++) {\n    if (arr[i] === target) return i;\n  }\n  return -1;\n}",

    # --- weak match (loop but not search) ---
    "for i in range(len(arr)):\n    print(arr[i])",

    # --- clear MISS (unrelated code) ---
    "def quicksort(arr):\n    if len(arr) <= 1:\n        return arr\n    pivot = arr[0]\n    left = [x for x in arr[1:] if x < pivot]\n    right = [x for x in arr[1:] if x >= pivot]\n    return quicksort(left) + [pivot] + quicksort(right)",

    # --- clear MISS (non-code) ---
    "How to bake a chocolate cake step by step",

]

# ---- search queries (mix of natural + code intent) ----
QUERIES = [
    "how do I cook steak",
    "python fast search algorithm",
    "async javascript error handling",
    "sql query gmail users",
    "css center element",
    "binary search implementation",
    "deep learning overfitting explanation",
    "python linear search function"

]

# ---- helpers ----
def embed(model: str, text: str) -> List[float]:
    r = requests.post(
        OLLAMA_URL,
        json={"model": model, "prompt": text},
        timeout=120
    )
    r.raise_for_status()
    return r.json()["embedding"]

def cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a))
    nb = math.sqrt(sum(x*x for x in b))
    return dot / (na * nb + 1e-9)

def batch_embed(model: str, texts: List[str]) -> Tuple[List[List[float]], float]:
    vectors = []
    start = time.perf_counter()

    for t in texts:
        vectors.append(embed(model, t))

    elapsed = time.perf_counter() - start
    return vectors, elapsed

def rank(query_vec, corpus_vecs, corpus_texts, top_k=3):
    scores = []
    for i, vec in enumerate(corpus_vecs):
        sim = cosine_similarity(query_vec, vec)
        scores.append((sim, corpus_texts[i]))

    scores.sort(reverse=True, key=lambda x: x[0])
    return scores[:top_k]

def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)

# ---- main benchmark ----
def run():
    for model in MODELS:
        print("\n" + "="*60)
        print(f"MODEL: {model}")

        # ---- embed corpus ----
        corpus_vecs, corpus_time = batch_embed(model, CORPUS)
        corpus_tokens = sum(estimate_tokens(t) for t in CORPUS)

        print(f"\nCorpus embedding time: {corpus_time:.4f}s")
        print(f"Approx throughput: {corpus_tokens / corpus_time:.2f} tok/s")

        # ---- embed queries ----
        query_vecs, query_time = batch_embed(model, QUERIES)
        query_tokens = sum(estimate_tokens(t) for t in QUERIES)

        print(f"Query embedding time: {query_time:.4f}s")
        print(f"Approx throughput: {query_tokens / query_time:.2f} tok/s")

        # ---- ranking ----
        print("\n--- Ranking Results ---")
        for q, q_vec in zip(QUERIES, query_vecs):
            print(f"\nQuery: {q}")

            results = rank(q_vec, corpus_vecs, CORPUS, top_k=3)
            for score, text in results:
                preview = text.replace("\n", " ")[:80]
                print(f"  {score:.4f} -> {preview}")

if __name__ == "__main__":
    run()