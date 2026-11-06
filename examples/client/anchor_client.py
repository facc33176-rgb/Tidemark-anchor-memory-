"""A tiny HTTP client for the Anchor Memory REST API, with two habits learned in production.

1. Check before writing: recall a few similar memories first and skip the write when the
   same thing was already stored (scheduled summarisers otherwise write one event many times).
2. Never come back empty-handed: if recall fails (for example the embedding provider is down
   or out of credit), fall back to a plain keyword scan over recent memories and say so.

Only the standard library is used. Run `python anchor_client.py` against a local server.

中文说明：写之前先查重；向量召回挂了就退回关键字扫描，并在结果里标明是保底。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("ANCHOR_URL", "http://127.0.0.1:8765")
TIMEOUT = float(os.environ.get("ANCHOR_TIMEOUT", "15"))


def _get(path: str, **params) -> dict:
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _norm(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?；;：:“”\"'（）()]+", "", text or "").lower()


def _bigrams(text: str) -> set[str]:
    t = _norm(text)
    return {t[i : i + 2] for i in range(len(t) - 1)}


def similar(a: str, b: str) -> float:
    """Character-bigram overlap (Dice). Good enough to catch the same sentence written twice."""
    x, y = _bigrams(a), _bigrams(b)
    return 0.0 if not x or not y else 2 * len(x & y) / (len(x) + len(y))


def store(text: str, tag: str = "general", *, dedupe: float = 0.8, **extra) -> dict:
    """Store a memory unless a near-identical one already exists."""
    try:
        hits = recall(text, budget=5).get("results", [])
    except Exception:
        hits = []
    for hit in hits:
        if similar(text, hit.get("text", "")) >= dedupe:
            return {"skipped": True, "duplicate_of": hit.get("memory_id")}
    return _post("/api/store", {"text": text, "tag": tag, **extra})


def keyword_fallback(query: str, n: int = 5, pool: int = 500) -> list[dict]:
    """Score recent memories by keywords: a whole query term hit = 3, each CJK bigram hit = 1."""
    items = _get("/api/recent", n=pool)
    items = items.get("memories", items) if isinstance(items, dict) else items
    terms = [t for t in re.split(r"[\s，。！？、,.!?]+", query) if t]
    grams = _bigrams(query)
    scored = []
    for it in items or []:
        text = it.get("text", "")
        score = sum(3 for t in terms if t and t in text) + sum(1 for g in grams if g in _norm(text))
        if score:
            scored.append((score, it.get("timestamp", ""), it))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [it for _, _, it in scored[:n]]


def recall(query: str, budget: int = 5, policy: str = "search") -> dict:
    """Recall; on any failure fall back to keyword_fallback and mark the result."""
    try:
        return _get("/api/recall", query=query, budget=budget, policy=policy)
    except (urllib.error.URLError, TimeoutError, ValueError) as err:
        try:
            return {"results": keyword_fallback(query, budget), "fallback": "keyword", "error": str(err)}
        except Exception:
            raise err


if __name__ == "__main__":
    print(store("The fictional observatory recalibrated its blue sensor.", "event"))
    print(store("The fictional observatory recalibrated its blue sensor.", "event"))  # skipped
    print(json.dumps(recall("blue sensor"), ensure_ascii=False)[:300])
    print(keyword_fallback("observatory sensor")[:1])
