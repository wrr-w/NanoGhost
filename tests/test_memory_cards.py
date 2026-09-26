# -*- coding: utf-8 -*-
"""Card / Graph 落数据测试（A 线）。

这三件事此前从未被验证过（库是空的）：卡片签名 / 冷启动剪枝 / 图落边。
"""

from __future__ import annotations

from agent_core.memory.cards import (
    _flow_signature,
    _prune_cards_by_tail_elimination,
    record_successful_flow,
)
from agent_core.memory.graph import update_graph_ml


class FakeDB:
    def __init__(self):
        self.cards = {}
        self.edges = []

    def load_all_memory_cards(self, namespace=None):
        return list(self.cards.values())

    def save_memory_card(self, card):
        self.cards[card["flow_hash"]] = card

    def save_ml_edge(self, edge):
        self.edges.append(edge)


def _steps():
    return [
        {"step": 1, "method": "CAPTURE_ABOUT", "path": "",
         "tool_name": "capture_about", "ok": True, "status_code": 0},
        {"step": 2, "method": "CAPTURE_AGENT_STATUS", "path": "",
         "tool_name": "capture_agent_status", "ok": True, "status_code": 0},
    ]


# ---- 签名 -----------------------------------------------------------------

def test_flow_signature_includes_tool_name():
    sig = _flow_signature([
        {"method": "GET", "path": "/a", "tool_name": "t1"},
        {"method": "GET", "path": "/a", "tool_name": "t2"},
    ])
    assert sig["length"] == 2
    assert sig["steps"][0] != sig["steps"][1]


# ---- Card 落库 -------------------------------------------------------------

def test_record_flow_creates_card():
    db = FakeDB()
    h = record_successful_flow("看看采集状态", _steps(), 2, db=db)
    assert h
    assert len(db.cards) == 1
    card = db.cards[h]
    assert card["success_count"] == 1
    assert card["steps"][0]["tool_name"] == "capture_about"


def test_record_flow_hits_existing_card():
    db = FakeDB()
    h1 = record_successful_flow("看看采集状态", _steps(), 2, db=db)
    h2 = record_successful_flow("再查一次采集", _steps(), 3, db=db)
    assert h1 == h2
    assert len(db.cards) == 1
    assert db.cards[h1]["success_count"] == 2
    assert db.cards[h1]["total_rounds"] == 5


def test_all_failed_steps_not_recorded():
    db = FakeDB()
    steps = [dict(s, ok=False) for s in _steps()]
    assert record_successful_flow("全失败", steps, 1, db=db) is None
    assert db.cards == {}


def test_no_intent_not_recorded():
    db = FakeDB()
    assert record_successful_flow("   ", _steps(), 1, db=db) is None
    assert db.cards == {}


# ---- 剪枝 -----------------------------------------------------------------

def test_cold_start_cards_not_pruned():
    items = [
        {"flow_hash": "a", "success_count": 1, "approved_count": 0, "intent_examples": []},
        {"flow_hash": "b", "success_count": 1, "approved_count": 0, "intent_examples": []},
    ]
    assert len(_prune_cards_by_tail_elimination(items)) == 2


def test_prune_removes_tail_when_warm():
    items = [
        {"flow_hash": f"c{i}", "success_count": 10, "approved_count": 0, "intent_examples": []}
        for i in range(5)
    ]
    items.append({"flow_hash": "tail", "success_count": 0, "approved_count": 0, "intent_examples": []})
    kept = [i["flow_hash"] for i in _prune_cards_by_tail_elimination(items)]
    assert "tail" not in kept
    assert len(kept) == 5


# ---- Graph 落边 ------------------------------------------------------------

def test_update_graph_creates_edges():
    db = FakeDB()
    update_graph_ml(_steps(), db=db)
    assert len(db.edges) == 4
    assert {e["level"] for e in db.edges} == {1, 2, 3, 4}
    assert all(e["total_count"] == 1 for e in db.edges)


def test_graph_skips_identical_adjacent_steps():
    db = FakeDB()
    s = _steps()[0]
    update_graph_ml([dict(s, step=1), dict(s, step=2)], db=db)
    assert db.edges == []


def test_graph_distinguishes_tools_with_same_method_and_path():
    db = FakeDB()
    a = {"step": 1, "method": "GET", "path": "", "tool_name": "t1", "ok": True}
    b = {"step": 2, "method": "GET", "path": "", "tool_name": "t2", "ok": True}
    update_graph_ml([a, b], db=db)
    assert len(db.edges) == 4


def test_graph_empty_steps_noop():
    db = FakeDB()
    update_graph_ml([], db=db)
    assert db.edges == []
