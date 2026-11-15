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
    assert a["memory_id"] == b["memory_id"] == "state-近况" and b["action"] == "revised"
    assert mem.db.get("state-近况")["text"] == "宿舍搬完了，在准备期末"
    assert "搬宿舍" in rg.provenance(mem, "state-近况")["revisions"][0]


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
    pack = rg.memory_pack(mem, "她喜欢什么口味的冰淇淋", budget=6, max_chars=60)
    assert pack["skipped"] is False
    assert pack["items"] and all(it["kind"] == "preference" for it in pack["items"])
    assert pack["items"][0]["source"].startswith("chat:")
    assert pack["chars"] <= 60 and pack["truncated"] >= 1
