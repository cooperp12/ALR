import hashlib
import re


def semantic_query_fingerprint(query):
    q = re.sub(r"\s+", " ", (query or "").strip().lower())
    q = re.sub(r"\|\s*head\s+\d+", "", q)
    q = re.sub(r"\|\s*sort\s+[^|]+", "", q)
    q = re.sub(r"\s+", " ", q).strip()
    return hashlib.sha256(q.encode("utf-8")).hexdigest()
