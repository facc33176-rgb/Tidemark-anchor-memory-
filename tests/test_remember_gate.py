"""把关写入、出处、近况覆盖、先判断再回忆、回忆包预算。"""
from __future__ import annotations

import remember_gate as rg
from anchor_memory import AnchorMemory


def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("ANCHOR_DISABLE_CHROMA", "1")
    monkeypatch.setenv("ANCHOR_EMBED_PROVIDER", "local")
    return AnchorMemory(str(tmp_path))


def test_gate_rules():
    assert rg.should_recall("嗯嗯")["recall"] is False
    assert rg.should_recall("好呀～")["recall"] is False
    assert rg.should_recall("哈哈哈哈")["recall"] is False
    assert rg.should_recall("你还记得上次我们去哪吃饭吗")["reason"] == "asks_past"
    assert rg.should_recall("我下周三要考《宏观经济学》")["recall"] is True
    assert rg.should_recall("今天食堂的饭好难吃，你觉得我该点外卖吗")["recall"] is True


def test_store_with_kind_and_provenance(tmp_path, monkeypatch):
    mem = fresh(tmp_path, monkeypatch)
    r = rg.remember(mem, "她不吃香菜，点外卖要备注", kind="preference",
                    source="chat:2026-11-15#12", quote="我不吃香菜！！")
    assert r["action"] == "stored"
    p = rg.provenance(mem, r["memory_id"])
    assert p["source"] == "chat:2026-11-15#12" and p["quote"] == "我不吃香菜！！"
    assert rg.kind_of(mem.db.get(r["memory_id"])["tag"]) == "preference"
    assert mem.db.get(r["memory_id"])["tag"].startswith("rkind:")


def test_duplicate_is_not_stored_twice(tmp_path, monkeypatch):
    mem = fresh(tmp_path, monkeypatch)
    a = rg.remember(mem, "她养了一只橘猫，叫团子", kind="fact")
    b = rg.remember(mem, "她养了一只橘猫，叫团子", kind="fact", source="chat:x")
    assert b["action"] == "duplicate" and b["memory_id"] == a["memory_id"]
    assert mem.count() == 1
    assert rg.provenance(mem, a["memory_id"])["seen_again"] == 1


def test_similar_is_reported_not_overwritten(tmp_path, monkeypatch):
    mem = fresh(tmp_path, monkeypatch)
    a = rg.remember(mem, "她下周三考宏观经济学", kind="event")
    b = rg.remember(mem, "她下周三考微观经济学", kind="event")
    assert b["action"] == "stored" and b["memory_id"] != a["memory_id"]
    assert mem.count() == 2
    assert b["similar"] and b["similar"][0]["memory_id"] == a["memory_id"]


def test_explicit_revise_keeps_history(tmp_path, monkeypatch):
    mem = fresh(tmp_path, monkeypatch)
    a = rg.remember(mem, "她养了一只橘猫，叫团子", kind="fact")
    b = rg.remember(mem, "她养了一只橘猫，叫团团", kind="fact", source="chat:y",
                    quote="其实叫团团啦", revise_of=a["memory_id"])
    assert b["action"] == "revised" and b["memory_id"] == a["memory_id"] and mem.count() == 1
    assert mem.db.get(a["memory_id"])["text"] == "她养了一只橘猫，叫团团"
    p = rg.provenance(mem, a["memory_id"])
    assert "团子" in p["revisions"][0] and p["quote"] == "其实叫团团啦"


def test_state_keeps_one_record(tmp_path, monkeypatch):
    mem = fresh(tmp_path, monkeypatch)
    a = rg.remember(mem, "这周在搬宿舍，压力大", kind="state", key="近况")
    b = rg.remember(mem, "宿舍搬完了，在准备期末", kind="state", key="近况")
    sid = a["memory_id"]
    assert sid.startswith("state-近况-") and b["memory_id"] == sid and b["action"] == "revised"
    assert mem.db.get(sid)["text"] == "宿舍搬完了，在准备期末"
    assert "搬宿舍" in rg.provenance(mem, sid)["revisions"][0]


def test_bad_kind_rejected(tmp_path, monkeypatch):
    mem = fresh(tmp_path, monkeypatch)
    try:
        rg.remember(mem, "x", kind="gossip")
    except ValueError:
        return
    raise AssertionError("bad kind accepted")


def test_memory_pack_gate_and_budget(tmp_path, monkeypatch):
    mem = fresh(tmp_path, monkeypatch)
    assert rg.memory_pack(mem, "嗯嗯")["skipped"] is True
    for flavor in ["香草", "巧克力", "抹茶", "芒果", "草莓", "海盐"]:
        rg.remember(mem, f"她喜欢{flavor}口味的冰淇淋，买的时候会挑{flavor}的牌子", kind="preference", source=f"chat:{flavor}")
    assert mem.count() == 6
    pack = rg.memory_pack(mem, "她喜欢什么口味的冰淇淋", budget=6, max_chars=100)
    assert pack["skipped"] is False
    assert pack["items"] and all(it["kind"] == "preference" for it in pack["items"])
    assert pack["items"][0]["source"].startswith("chat:")
    assert pack["chars"] <= 100 and pack["truncated"] >= 1


def test_state_survives_short_cleanup_and_revise_keeps_context(tmp_path, monkeypatch):
    mem = fresh(tmp_path, monkeypatch)
    sid = rg.remember(mem, "这周在搬宿舍", kind="state", key="近况")["memory_id"]
    assert mem.db.get(sid)["tier"] == "long"
    a = rg.remember(mem, "她养了一只橘猫，叫团子", kind="fact")
    with mem.db._conn() as conn:
        conn.execute("UPDATE memories SET context = ? WHERE memory_id = ?", ("完整原文", a["memory_id"]))
        conn.commit()
    rg.remember(mem, "她养了一只橘猫，叫团团", kind="fact", revise_of=a["memory_id"])
    assert mem.db.get(a["memory_id"])["context"] == "完整原文"


def test_revise_rejects_unknown_and_caps_seen_again(tmp_path, monkeypatch):
    mem = fresh(tmp_path, monkeypatch)
    try:
        rg.remember(mem, "x", kind="fact", revise_of="nope")
        raise AssertionError("accepted unknown revise_of")
    except ValueError:
        pass
    a = rg.remember(mem, "她喜欢下雨天", kind="preference")
    for _ in range(30):
        rg.remember(mem, "她喜欢下雨天", kind="preference")
    assert rg.provenance(mem, a["memory_id"])["seen_again"] == 20


def test_reviewer_cases(tmp_path, monkeypatch):
    mem = fresh(tmp_path, monkeypatch)
    # 1. 长句里改一个字不能被当成重复吞掉
    long_a = "她最喜欢的是那家开在学校后门、老板很凶但是很好吃的牛肉面，每周都要去吃两三次"
    a = rg.remember(mem, long_a, kind="preference")
    b = rg.remember(mem, long_a.replace("最喜欢", "最不喜欢"), kind="preference")
    assert b["action"] == "stored" and b["similar"] and b["similar"][0]["memory_id"] == a["memory_id"]
    # 标点空格不同仍算重复
    assert rg.remember(mem, long_a.replace("，", " "), kind="preference")["action"] == "duplicate"
    # 3. 改 understanding 不能把层级打回 raw
    mem.store(memory_id="u1", text="她累的时候不想被讲道理", tag="rkind:understanding", tier="long", level="understanding")
    rg.remember(mem, "她累的时候不想被讲道理，只想被抱抱", kind="understanding", revise_of="u1")
    assert mem.db.get("u1")["level"] == "understanding"
    # 4. 改了以后旧出处不再当成新文的出处
    c = rg.remember(mem, "她住三号楼", kind="fact", source="chat:1", quote="我住三号楼")
    rg.remember(mem, "她搬到了七号楼", kind="fact", revise_of=c["memory_id"])
    p = rg.provenance(mem, c["memory_id"])
    assert p["quote"] == "" and p["source"] == "" and p["revisions"]
    # 5. 短而重要的话要查
    for t in ["我怀孕了", "外婆走了", "团子死了", "分手吧", "好难过", "生日快乐"]:
        assert rg.should_recall(t)["recall"] is True, t
    assert rg.should_recall("外婆今天住院了", last_query="外婆今天来看我了")["recall"] is True
    # 6. 不同 key 不撞
    x = rg.remember(mem, "a", kind="state", key="😀")["memory_id"]
    y = rg.remember(mem, "b", kind="state", key="🎵")["memory_id"]
    assert x != y
    # 8. 第一条也不超预算；坏 policy 报错
    mem.store(memory_id="big", text="长" * 2000, tag="rkind:fact", tier="long")
    pk = rg.memory_pack(mem, "长长长长长长", max_chars=100)
    assert pk["chars"] <= 100
    try:
        rg.memory_pack(mem, "她喜欢什么", policy="nope")
        raise AssertionError("bad policy accepted")
    except ValueError:
        pass
    # 10. 类型不对报 ValueError
    try:
        rg.remember(mem, 123, kind="fact")
        raise AssertionError("non-string accepted")
    except ValueError:
        pass


def test_tagger_keeps_rkind(tmp_path, monkeypatch):
    import json, taxonomy_tagger
    from types import SimpleNamespace
    mem = fresh(tmp_path, monkeypatch)
    monkeypatch.setattr(taxonomy_tagger, "_CONFIG", SimpleNamespace(
        taxonomy_url="https://taxonomy.example.invalid/v1", taxonomy_api_key="t", taxonomy_model="m"))
    r = rg.remember(mem, "她最近在学吉他", kind="state", key="近况")
    monkeypatch.setattr(taxonomy_tagger, "DB", str(mem.db.db_path))
    monkeypatch.setattr(taxonomy_tagger, "_call", lambda messages, max_tokens=800: json.dumps(
        {"id": r["memory_id"], "tags": ["state:past", "domain:system", "kind:event"]}))
    assert taxonomy_tagger.tag_one(r["memory_id"], "她最近在学吉他") is True
    tag = mem.db.get(r["memory_id"])["tag"]
    assert rg.kind_of(tag) == "state" and "kind:event" in tag


def test_caller_tags_and_tier(tmp_path, monkeypatch):
    mem = fresh(tmp_path, monkeypatch)
    a = rg.remember(mem, "她说考完试想去看海", kind="event", tier="short",
                    tag="日常相处,tone:tender,rkind:fact,date:20261115", emotion_score=0.6)
    row = mem.db.get(a["memory_id"])
    assert row["tier"] == "short"
    assert row["tag"] == "rkind:event,日常相处,tone:tender,date:20261115"
    b = rg.remember(mem, "她说考完试想去青岛看海", kind="event", tag="date:20261116,heat:high",
                    revise_of=a["memory_id"])
    tag = mem.db.get(b["memory_id"])["tag"]
    assert tag.startswith("rkind:event,") and "日常相处" in tag and "date:20261116" in tag and "heat:high" in tag
    s = rg.remember(mem, "她这周在考试周", kind="state", key="近况", tag="学习")
    assert "学习" in mem.db.get(s["memory_id"])["tag"] and mem.db.get(s["memory_id"])["tier"] == "long"
    for bad in (dict(tier="forever"), dict(tag=["x"]), dict(tag="x" * 1001)):
        try:
            rg.remember(mem, "随便一句", **bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    for v in (True, "0.5", 2, -1):
        try:
            rg.score(v)
            raise AssertionError(v)
        except ValueError:
            pass
    assert rg.score(0) == 0.0 and rg.score(1) == 1.0
