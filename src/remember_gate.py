"""Gated writes, provenance and recall gating for Tidemark.

借鉴「原话当证据、写入要把关、先判断要不要回忆」三条做法，全部是加法：
  - remember():   写入前查重：去掉空格标点后一字不差才算重复（只加热）；看着像的照存，并把像的报出来，
                  由调用方（模型）判断是不是同一件事的新说法，是就带 revise_of 再调一次改旧的（旧文留在改动记录）。
                  每条带分类 rkind（fact/preference/event/understanding/state）和出处（source + quote）。
                  state 类带 key 时只保留一条（「近况」），每次覆盖。
  - should_recall(): 不花模型钱的规则判断：只跳过整句都是承接语的（嗯嗯、好呀、哈哈），其余都查。
  - memory_pack(): 判断 → 召回 → 附上分类和出处 → 按字数预算截断，并报告截掉了几条。
原有 /api/store、/api/recall 行为不变。

分类用 rkind: 前缀，不用 kind:，因为夜里的 taxonomy_tagger 会重写 kind:（它的取值不同）；
taxonomy_tagger.tag_one 已改为保留 rkind: 部分。
"""
from __future__ import annotations

import difflib
import hashlib
import re
import threading
import uuid
from datetime import datetime, timezone

KINDS = ("fact", "preference", "event", "understanding", "state")
KIND_PREFIX = "rkind:"
NEAR_SIM = 0.75        # 看着像 → 照存，并报给调用方
MAX_REVISIONS = 30     # 一条记忆最多留这么多条改动记录
MAX_SEEN = 20
POLICIES = ("search", "conversation")
_LOCK = threading.Lock()
_PUNCT = re.compile(r"[\s　，。！？、；：,.!?;:~～…·\"'“”‘’（）()【】\[\]《》<>—\-_]+")
_SHORT_ACK = re.compile(r"^[\s，。！？!?~～…\.、]*(嗯+|好+|好的|好呀|好哒|行|ok|okay|哈+|嘿+|呜+|啊+|噢+|哦+|对+|是的|收到|晚安|早安|午安|么么哒?|亲亲|抱抱|在吗|在|知道了|明白了?|谢谢|嗷+|嘻+|hh+|hhh+)[\s，。！？!?~～…\.、啦呀呢吧哦嘛]*$", re.I)
_PAST = re.compile(r"上次|之前|以前|那天|那次|还记得|记不记得|记得吗|你说过|我说过|我们说过|当时|去年|上个月|上周|前几天|昨天|前天|小时候|第一次|原来|曾经|那会儿|忘了吗|忘记")
_ASK = re.compile(r"[?？]|什么|为什么|怎么|哪|谁|几|多少|是不是|有没有|吗$|呢$")
_ENTITY = re.compile(r"[A-Z][a-zA-Z]{2,}|「[^」]{1,20}」|《[^》]{1,30}》|\d{1,4}[年月号日]")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(text: str) -> str:
    return _PUNCT.sub("", (text or "")).lower()


def kind_of(tag: str) -> str:
    for part in (tag or "").split(","):
        part = part.strip()
        if part.startswith(KIND_PREFIX):
            return part[len(KIND_PREFIX):]
    return ""


def _with_kind(tag: str, kind: str, extra: str = "") -> str:
    parts = []
    for p in ((tag or "") + "," + (extra or "")).split(","):
        p = p.strip()
        if p and not p.startswith(KIND_PREFIX) and p not in parts:
            parts.append(p)
    return ",".join([f"{KIND_PREFIX}{kind}"] + parts)


def _state_id(key: str) -> str:
    raw = (key or "").strip() or "default"
    slug = re.sub(r"[^0-9A-Za-z一-鿿]+", "-", raw)[:24].strip("-") or "k"
    return f"state-{slug}-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:6]}"


# ---------- 先判断要不要回忆 ----------
def should_recall(text: str, last_query: str = "") -> dict:
    """Cheap rule gate. Returns {recall: bool, reason: str}.
    宁可多查一次，也不能漏掉「外婆走了」「我怀孕了」这种短而重要的话，所以只跳过整句承接语。"""
    t = (text or "").strip()
    if not t:
        return {"recall": False, "reason": "empty"}
    if _SHORT_ACK.match(t):
        return {"recall": False, "reason": "short_ack"}
    if _PAST.search(t):
        return {"recall": True, "reason": "asks_past"}
    if _ENTITY.search(t):
        return {"recall": True, "reason": "entity"}
    if last_query and _norm(t) == _norm(last_query):
        return {"recall": False, "reason": "same_as_last"}
    return {"recall": True, "reason": "question" if _ASK.search(t) else "new_topic"}


# ---------- 出处 ----------
def _ann_text(a) -> str:
    return a.get("text", "") if isinstance(a, dict) else str(a)


def provenance(mem, memory_id: str) -> dict:
    """出处跟着最近一次改动走：遇到 revised_from 就清空之前的 source/quote。"""
    out = {"source": "", "quote": "", "revisions": [], "seen_again": 0}
    for ann in mem.db.get_annotations(memory_id=memory_id) or []:
        text = _ann_text(ann)
        if text.startswith("revised_from:"):
            out["revisions"].append(text[13:])
            out["source"], out["quote"] = "", ""
        elif text.startswith("source:"):
            out["source"] = text[7:]
        elif text.startswith("quote:"):
            out["quote"] = text[6:]
        elif text.startswith("seen_again:"):
            out["seen_again"] += 1
    return out


def _count_ann(mem, memory_id: str, prefix: str) -> int:
    return sum(1 for a in (mem.db.get_annotations(memory_id=memory_id) or []) if _ann_text(a).startswith(prefix))


def _seen_again(mem, memory_id: str, source: str) -> None:
    if _count_ann(mem, memory_id, "seen_again:") < MAX_SEEN:
        mem.db.annotate(memory_id, f"seen_again:{(source or _now())[:200]}")


def _mark_revised(mem, memory_id: str, old_text: str) -> None:
    if _count_ann(mem, memory_id, "revised_from:") < MAX_REVISIONS:
        mem.db.annotate(memory_id, f"revised_from:{(old_text or '')[:300]} @{_now()}")


def _overwrite(mem, memory_id: str, text: str, tag: str, tier: str, emotion_score: float,
               old: dict, bump_time: bool = False) -> None:
    """覆盖正文；保留原来的层级、合集和完整上下文，近况类把时间改成现在。"""
    old = old or {}
    mem.store(memory_id=memory_id, text=text, tag=tag, tier=tier, emotion_score=emotion_score,
              level=old.get("level") or "raw", collection=old.get("collection") or "")
    sets, args = [], []
    if old.get("context"):
        sets.append("context = ?"); args.append(old["context"])
    if bump_time:
        sets.append("timestamp = ?"); args.append(datetime.utcnow().isoformat())
    if sets:
        with mem.db._conn() as conn:
            conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE memory_id = ?", (*args, memory_id))
            conn.commit()


def _annotate_source(mem, memory_id: str, source: str, quote: str) -> None:
    if source:
        mem.db.annotate(memory_id, f"source:{source[:200]}")
    if quote:
        mem.db.annotate(memory_id, f"quote:{quote[:400]}")


def _heat(mem, memory_id: str, amount: float, reason: str, spread: bool) -> None:
    try:
        mem.db.apply_heat([memory_id], amount, f"{reason}:{memory_id}", spread=spread, source=reason)
    except Exception:
        pass


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
        other = kind_of(row.get("tag", r.get("tag", "")))
        if kind and other and other != kind:
            continue
        dist = r.get("distance")
        vec = (1.0 - float(dist)) if isinstance(dist, (int, float)) else 0.0
        txt = difflib.SequenceMatcher(None, _norm(text), _norm(old)).ratio()
        out.append({"memory_id": mid, "text": old, "sim": round(max(vec, txt), 3), "vec": round(vec, 3),
                    "txt": round(txt, 3), "same": _norm(text) == _norm(old), "tag": row.get("tag", "")})
    out.sort(key=lambda x: (not x["same"], -x["sim"]))
    return out


def _new_id(mem) -> str:
    for _ in range(5):
        mid = uuid.uuid4().hex[:12]
        if not mem.db.get(mid):
            return mid
    raise RuntimeError("could not allocate memory_id")


def score(v) -> float:
    """emotion_score：0～1 的数，其它一律当错。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("emotion_score must be a number")
    v = float(v)
    if not 0.0 <= v <= 1.0:
        raise ValueError("emotion_score must be between 0 and 1")
    return v


def remember(mem, text, kind="fact", source="", quote="", key="", revise_of="",
             tier: str = "long", emotion_score: float = 0.5, tag: str = "") -> dict:
    """revise_of: 调用方（模型）确认「这是那条旧记忆的更新」时传旧 memory_id，才会改旧的。
    不传时：去掉空格标点后一字不差 → duplicate；像 → 照存为新的，并在 similar 里报出来。"""
    for name, val in (("text", text), ("kind", kind), ("source", source), ("quote", quote),
                      ("key", key), ("revise_of", revise_of), ("tag", tag), ("tier", tier)):
        if val is not None and not isinstance(val, str):
            raise ValueError(f"{name} must be a string")
    text = (text or "").strip()
    if not text:
        raise ValueError("text is required")
    if len(text) > 4000:
        raise ValueError("text too long (max 4000)")
    kind = (kind or "fact").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    source, quote, key, revise_of = source or "", quote or "", key or "", (revise_of or "").strip()
    # tag：调用方自己的标签（分类、语气、日期…），原样并进去；rkind: 由这里管，传进来的会被忽略
    tag = (tag or "").strip()
    if len(tag) > 1000:
        raise ValueError("tag too long (max 1000)")
    tier = (tier or "long").strip()
    if tier not in ("short", "long"):
        raise ValueError("tier must be short or long")

    with _LOCK:
        # 近况：一个 key 只留一条，每次覆盖，旧内容记进改动记录
        if kind == "state":
            mid = _state_id(key)
            old = mem.db.get(mid)
            if old and _norm(old.get("text")) == _norm(text):
                _seen_again(mem, mid, source)
                return {"action": "duplicate", "memory_id": mid}
            # tier 一定要 long：short 会被定期清理，近况不能被清掉
            _overwrite(mem, mid, text, _with_kind(old.get("tag", "") if old else "", "state", tag), "long",
                       emotion_score, old, bump_time=True)
            if old:
                _mark_revised(mem, mid, old.get("text"))
            _annotate_source(mem, mid, source, quote)
            _heat(mem, mid, 0.4, "remember_state", False)
            return {"action": "revised" if old else "stored", "memory_id": mid}

        if revise_of:
            old = mem.db.get(revise_of)
            if not old:
                raise ValueError("revise_of not found")
            if (old.get("collection") or "") == "wenku" or (old.get("level") or "raw") not in ("raw", "understanding", "cognition"):
                raise ValueError("revise_of must be a daily memory")
            _overwrite(mem, revise_of, text, _with_kind(old.get("tag", ""), kind, tag), old.get("tier") or tier,
                       emotion_score, old)
            _mark_revised(mem, revise_of, old.get("text"))
            _annotate_source(mem, revise_of, source, quote)
            _heat(mem, revise_of, 0.4, "remember_revise", False)
            return {"action": "revised", "memory_id": revise_of, "previous": old.get("text", "")}

        near = _nearest(mem, text, kind)
        if near and near[0]["same"]:
            top = near[0]
            _seen_again(mem, top["memory_id"], source)
            _heat(mem, top["memory_id"], 0.3, "remember_dup", False)
            return {"action": "duplicate", "memory_id": top["memory_id"], "similar": [top]}
        similar = [x for x in near if x["sim"] >= NEAR_SIM][:3]

        mid = _new_id(mem)
        mem.store(memory_id=mid, text=text, tag=_with_kind("", kind, tag), tier=tier, emotion_score=emotion_score)
        _annotate_source(mem, mid, source, quote)
        _heat(mem, mid, 0.6, "remember_store", True)
        return {"action": "stored", "memory_id": mid, "similar": similar}


# ---------- 回忆包：判断 → 召回 → 附出处 → 预算 ----------
def memory_pack(mem, query, budget: int = 6, max_chars: int = 1200, gate: bool = True,
                last_query: str = "", policy: str = "search") -> dict:
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {', '.join(POLICIES)}")
    budget = max(1, min(int(budget), 20))
    max_chars = max(100, min(int(max_chars), 8000))
    decision = should_recall(query, last_query) if gate else {"recall": True, "reason": "forced"}
    if not decision["recall"]:
        return {"skipped": True, "reason": decision["reason"], "items": [], "truncated": 0, "chars": 0}
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
    for r in rows[:budget]:
        mid = r.get("memory_id")
        if not mid:
            continue
        row = mem.db.get(mid) or {}
        text = row.get("text") or r.get("text") or r.get("snippet") or ""
        room = max_chars - used
        if len(text) > room:
            if items or room < 20:
                cut += 1
                continue
            text = text[: room - 1] + "…"   # 第一条也不许超预算，截短
        used += len(text)
        p = provenance(mem, mid)
        items.append({"memory_id": mid, "text": text, "kind": kind_of(row.get("tag", "")) or "",
                      "source": p["source"], "quote": p["quote"], "revised": bool(p["revisions"])})
    return {"skipped": False, "reason": decision["reason"], "items": items, "truncated": cut, "chars": used}
