"""Toroidal-Indexer: embedding helper via NVIDIA NIM API (nv-embed-v1, 4096-dim)."""

import json
import os
import sys

_NIM_URL = "https://integrate.api.nvidia.com/v1/embeddings"
_EMBEDDING_MODEL = "nvidia/nv-embed-v1"
EMBEDDING_DIM = 4096
_BATCH_SIZE = 50


def _get_nim_key():
    config_path = os.path.join(os.path.expanduser("~"), ".claude", "config.json")
    try:
        with open(config_path) as f:
            return json.load(f).get("nim_api_key", "")
    except Exception:
        return ""


def embed_texts(texts):
    """Embed a list of texts via NVIDIA NIM API. Returns list of 4096-dim vectors.

    Falls back to None on any error (caller should handle gracefully).
    """
    import requests

    key = _get_nim_key()
    if not key:
        return None

    safe_texts = [t if t and t.strip() else "[empty]" for t in texts]
    all_vecs = []
    for i in range(0, len(safe_texts), _BATCH_SIZE):
        batch = safe_texts[i : i + _BATCH_SIZE]
        try:
            resp = requests.post(
                _NIM_URL,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": _EMBEDDING_MODEL,
                    "input": batch,
                    "input_type": "passage",
                    "encoding_format": "float",
                },
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            all_vecs.extend(d["embedding"] for d in data["data"])
        except Exception as e:
            print(f"[indexer] NIM embed error: {e}", file=sys.stderr)
            return None
    return all_vecs


def embed_text(text):
    """Embed a single text. Returns 4096-dim vector or None on error."""
    result = embed_texts([text])
    return result[0] if result else None


def embed_query(text):
    """Embed a query string (uses 'query' input_type for asymmetric retrieval)."""
    import requests

    key = _get_nim_key()
    if not key:
        return None
    try:
        resp = requests.post(
            _NIM_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": _EMBEDDING_MODEL,
                "input": [text],
                "input_type": "query",
                "encoding_format": "float",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["data"][0]["embedding"]
    except Exception as e:
        print(f"[indexer] NIM query embed error: {e}", file=sys.stderr)
        return None
