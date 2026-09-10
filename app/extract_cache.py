"""Chunk-level extract cache: identical chunks skip the LLM entirely.

Key covers (file SHA, doc name, entity hint, page, chunk text, model,
cache version), so a cleared DB, a rotated API key, or a server restart
re-extracts from cache instead of re-paying every Bedrock call. Parse-cache
hits pair with extract-cache hits: re-uploading unchanged PDFs after e.g. a
key rotation costs ~zero LLM calls.

Entries are content-addressed and tiny; stale files are harmless (a key miss
just re-extracts). Not wiped by delete_document/clear_all on purpose.
"""
import hashlib
import json
import os

CACHE_VERSION = 2  # v2: whitespace-collapsed numeric scan + per-cent unit alias

# Re-exported lazily by callers to avoid import cycles at module load.
_Fact = None


def _dir() -> str:
    from .config import settings
    d = os.path.join(settings.data_dir, "extract_cache")
    os.makedirs(d, exist_ok=True)
    return d


def make_key(doc_sha: str, doc: str, entity: str, page_index: int,
             chunk_text: str) -> str:
    from .config import settings
    blob = "\x00".join([str(CACHE_VERSION), settings.bedrock_model,
                        doc_sha, doc, entity or "", str(page_index),
                        chunk_text or ""])
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def _path(key: str) -> str:
    return os.path.join(_dir(), key + ".json")


def get(key: str):
    """Return (facts, questions) on hit, None on miss/corruption."""
    global _Fact
    try:
        with open(_path(key)) as f:
            raw = json.load(f)
        if _Fact is None:
            from .models import Fact as _F
            _Fact = _F
        facts = [_Fact.model_validate(d) for d in raw.get("facts", [])]
        return facts, raw.get("questions", [])
    except (OSError, ValueError):
        return None


def put(key: str, facts, questions) -> None:
    try:
        with open(_path(key), "w") as f:
            json.dump({"facts": [x.model_dump() for x in facts],
                       "questions": questions or []}, f)
    except OSError:
        pass  # cache is best-effort; extraction already succeeded
