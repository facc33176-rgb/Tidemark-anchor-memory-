"""Gated writes, provenance and recall gating for Tidemark.

借鉴「原话当证据、写入要把关、先判断要不要回忆」三条做法，全部是加法：
  - remember():   写入前查重 → 一样就加热，不重复存；同一件事说法变了就改旧的并留下改动记录；
                  每条带 kind（fact/preference/event/understanding/state）和出处（source + quote）。
                  state 类带 key 时只保留一条（「近况」），每次覆盖。
  - should_recall(): 不花模型钱的规则判断：短承接不查，问到以前的事/新话题才查。
  - memory_pack(): 判断 → 召回 → 附上 kind 和出处 → 按字数预算截断，并报告截掉了几条。
原有 /api/store、/api/recall 行为不变。
"""
from __future__ import annotations

import difflib
import re
from datetime import datetime, timezone

KINDS = ("fact", "preference", "event", "understanding", "state")
KIND_PREFIX = "kind:"
DUP_TEXT = 0.95       # 文字几乎一样 → 同一条，只加热不重复存
DUP_VEC = 0.97        # 或者向量极像且文字也很像（≥0.85）
NEAR_SIM = 0.75       # 看着像但不确定是不是同一件事 → 照存，并把相似的报给调用方，由模型决定要不要 revise_of
_SHORT_ACK = re.compile(r"^[\s，。！？!?~～…\.、]*(嗯+|嗯嗯|好+|好的|好呀|好哒|行|ok|okay|哈+|哈哈+|嘿+|呜+|啊+|噢+|哦+|对+|是的|收到|晚安|早安|么么|亲亲|抱抱|在吗|在|知道了|明白|谢谢|嗷)[\s，。！？!?~～…\.、啦呀呢吧哦嘛]*$", re.I)
_PAST = re.compile(r"上次|之前|以前|那天|那次|还记得|记不记得|记得吗|你说过|我说过|我们说过|当时|去年|上个月|上周|前几天|昨天|前天|小时候|第一次|原来|曾经|那会儿|忘了吗|忘记")
_ASK = re.compile(r"[?？]|什么|为什么|怎么|哪|谁|几|多少|是不是|有没有|吗$|呢$")
_ENTITY = re.compile(r"[A-Z][a-zA-Z]{2,}|「[^」]{1,20}」|《[^》]{1,30}》|\d{1,4}[年月号日]")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def kind_of(tag: str) -> str:
    for part in (tag or "").split(","):
        part = part.strip()
        if part.startswith(KIND_PREFIX):
            return part[len(KIND_PREFIX):]
    return ""


def _with_kind(tag: str, kind: str) -> str:
    parts = [p.strip() for p in (tag or "").split(",") if p.strip() and not p.strip().startswith(KIND_PREFIX)]
    return ",".join([f"{KIND_PREFIX}{kind}"] + parts)


def _slug(key: str) -> str:
    s = re.sub(r"[^0-9A-Za-z一-鿿_-]+", "-", (key or "").strip())[:40].strip("-")
    return s or "default"


# ---------- 先判断要不要回忆 ----------
def should_recall(text: str, last_query: str = "") -> dict:
    """Cheap rule gate. Returns {recall: bool, reason: str}."""
    t = (text or "").strip()
    if not t:
        return {"recall": False, "reason": "empty"}
    if _SHORT_ACK.match(t):
        return {"recall": False, "reason": "short_ack"}
    if _PAST.search(t):
        return {"recall": True, "reason": "asks_past"}
    if _ENTITY.search(t):
        return {"recall": True, "reason": "entity"}
    if last_query and difflib.SequenceMatcher(None, t, last_query).ratio() > 0.6 and len(t) < 30:
        return {"recall": False, "reason": "same_topic"}
    if len(t) <= 4 and not _ASK.search(t):
        return {"recall": False, "reason": "too_short"}
    return {"recall": True, "reason": "question" if _ASK.search(t) else "new_topic"}


# ---------- 出处 ----------
def provenance(mem, memory_id: str) -> dict:
    out = {"source": "", "quote": "", "revisions": [], "seen_again": 0}
    for ann in mem.db.get_annotations(memory_id=memory_id) or []:
        text = ann.get("text", "") if isinstance(ann, dict) else str(ann)
        if text.startswith("source:"):
            out["source"] = text[7:]
        elif text.startswith("quote:"):
            out["quote"] = text[6:]
        elif text.startswith("revised_from:"):
            out["revisions"].append(text[13:])
        elif text.startswith("seen_again:"):
            out["seen_again"] += 1
    return out


def _annotate_source(mem, memory_id: str, source: str, quote: str) -> None:
    if source:
        mem.db.annotate(memory_id, f"source:{source[:200]}")
    if quote:
        mem.db.annotate(memory_id, f"quote:{quote[:400]}")


# ---------- 把关写入 ----------
def _nearest(mem, text: str, kind: str, n: int = 5) -> list:
    try:
        rows = mem.search(query=text, n_results=n, hebbian=False, activate_on_hit=False,
                          cite_on_hit=False, corpus="exclude", max_distance=None)
    except Exception:
        rows = []
    out = []
    for r in rows or []:
        mid = r.get("memory_id")
        if not mid:
            continue
        row = mem.db.get(mid) or {}
        old = row.get("text") or r.get("text") or r.get("snippet") or ""
        if kind and kind_of(row.get("tag", r.get("tag", ""))) not in ("", kind):
            continue
        dist = r.get("distance")
        vec = (1.0 - float(dist)) if isinstance(dist, (int, float)) else 0.0
        txt = difflib.SequenceMatcher(None, text, old).ratio()
        out.append({"memory_id": mid, "text": old, "sim": round(max(vec, txt), 3), "vec": round(vec, 3),
                    "txt": round(txt, 3), "tag": row.get("tag", "")})
    out.sort(key=lambda x: -x["sim"])
    return out


def remember(mem, text: str, kind: str = "fact", source: str = "", quote: str = "",
             key: str = "", revise_of: str = "", tier: str = "long",
             emotion_score: float = 0.5) -> dict:
    """revise_of: 调用方（模型）确认「这是那条旧记忆的更新」时传旧 memory_id，才会改旧的。
    不传时：几乎一样 → duplicate；像但不确定 → 照存为新的，并在 similar 里报出来。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("text is required")
    kind = (kind or "fact").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    tag = _with_kind("", kind)

    # 近况：一个 key 只留一条，每次覆盖，旧内容记进改动记录
    if kind == "state":
        mid = f"state-{_slug(key)}"
        old = mem.db.get(mid)
        if old and (old.get("text") or "").strip() == text:
            mem.db.annotate(mid, f"seen_again:{source or _now()}")
            return {"action": "duplicate", "memory_id": mid}
        mem.store(memory_id=mid, text=text, tag=tag, tier="short", emotion_score=emotion_score)
        if old:
            mem.db.annotate(mid, f"revised_from:{(old.get('text') or '')[:300]} @{_now()}")
        _annotate_source(mem, mid, source, quote)
        return {"action": "revised" if old else "stored", "memory_id": mid}

    if revise_of:
        old = mem.db.get(revise_of)
        if not old:
            raise ValueError("revise_of not found")
        mem.store(memory_id=revise_of, text=text, tag=_with_kind(old.get("tag", ""), kind), tier=tier, emotion_score=emotion_score)
        mem.db.annotate(revise_of, f"revised_from:{(old.get('text') or '')[:300]} @{_now()}")
        _annotate_source(mem, revise_of, source, quote)
        return {"action": "revised", "memory_id": revise_of, "previous": old.get("text", "")}

    near = _nearest(mem, text, kind)
    top = near[0] if near else None
    if top and (top["txt"] >= DUP_TEXT or (top["vec"] >= DUP_VEC and top["txt"] >= 0.85)):
        mem.db.annotate(top["memory_id"], f"seen_again:{source or _now()}")
        try:
            mem.db.apply_heat([top["memory_id"]], 0.3, f"remember-dup:{top['memory_id']}", spread=False, source="remember_dup")
        except Exception:
            pass
        return {"action": "duplicate", "memory_id": top["memory_id"], "similar": [top]}
    similar = [x for x in near if x["sim"] >= NEAR_SIM][:3]

    import uuid
    mid = str(uuid.uuid4())[:8]
    mem.store(memory_id=mid, text=text, tag=tag, tier=tier, emotion_score=emotion_score)
    _annotate_source(mem, mid, source, quote)
    return {"action": "stored", "memory_id": mid, "similar": similar}


# ---------- 回忆包：判断 → 召回 → 附出处 → 预算 ----------
def memory_pack(mem, query: str, budget: int = 6, max_chars: int = 1200, gate: bool = True,
                last_query: str = "", policy: str = "search") -> dict:
    decision = should_recall(query, last_query) if gate else {"recall": True, "reason": "forced"}
    if not decision["recall"]:
        return {"skipped": True, "reason": decision["reason"], "items": [], "truncated": 0}
    try:
        res = mem.recall(query, budget=budget, policy=policy, allow_empty=True)
        rows = res.get("results", []) if isinstance(res, dict) else (res or [])
    except Exception:
        rows = []
    if not rows:
        try:
            rows = mem.search(query=query, n_results=budget, hebbian=False, activate_on_hit=False, cite_on_hit=False)
        except Exception:
            rows = []
    items, used, cut = [], 0, 0
    for r in rows:
        mid = r.get("memory_id")
        if not mid:
            continue
        row = mem.db.get(mid) or {}
        text = row.get("text") or r.get("text") or r.get("snippet") or ""
        if used + len(text) > max_chars and items:
            cut += 1
            continue
        used += len(text)
        p = provenance(mem, mid)
        items.append({"memory_id": mid, "text": text, "kind": kind_of(row.get("tag", "")) or "",
                      "source": p["source"], "quote": p["quote"], "revised": bool(p["revisions"])})
    return {"skipped": False, "reason": decision["reason"], "items": items, "truncated": cut, "chars": used}
