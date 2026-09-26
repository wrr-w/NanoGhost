# -*- coding: utf-8 -*-
"""开放标签 × 日期 索引测试（C 线）。

内核零预设：只测「登记 / 计数 / 双键查询 / 空标签不造」，
不假设任何预置类目。
"""

from __future__ import annotations

from pathlib import Path

from agent_core.memory.tags import list_tags, load_tags, register_tags, tags_path
from agent_core.tool.builtins.memory import memory_read, memory_write


# ---- 索引本体 --------------------------------------------------------------

def test_register_tags_creates_index(tmp_path: Path):
    got = register_tags(str(tmp_path), ["记忆系统", "NanoGhost"], day_str="2026-09-23")
    assert got == ["记忆系统", "NanoGhost"]
    assert tags_path(str(tmp_path)).is_file()
    rows = {r["tag"]: r for r in list_tags(str(tmp_path))}
    assert rows["记忆系统"]["count"] == 1
    assert rows["记忆系统"]["dates"] == ["2026-09-23"]
    assert rows["记忆系统"]["first_seen"] == "2026-09-23"


def test_register_tags_incremental_across_days(tmp_path: Path):
    register_tags(str(tmp_path), ["甲"], day_str="2026-09-22")
    register_tags(str(tmp_path), ["甲"], day_str="2026-09-23")
    rows = {r["tag"]: r for r in list_tags(str(tmp_path))}
    assert rows["甲"]["count"] == 2
    assert rows["甲"]["first_seen"] == "2026-09-22"
    assert rows["甲"]["last_seen"] == "2026-09-23"
    assert rows["甲"]["dates"] == ["2026-09-22", "2026-09-23"]


def test_register_tags_same_day_twice_not_duplicated_in_dates(tmp_path: Path):
    register_tags(str(tmp_path), ["甲"], day_str="2026-09-23")
    register_tags(str(tmp_path), ["甲"], day_str="2026-09-23")
    rows = {r["tag"]: r for r in list_tags(str(tmp_path))}
    assert rows["甲"]["count"] == 2
    assert rows["甲"]["dates"] == ["2026-09-23"]


def test_register_tags_skips_blank(tmp_path: Path):
    assert register_tags(str(tmp_path), ["", "   ", None]) == []
    assert load_tags(str(tmp_path))["tags"] == {}


def test_register_tags_dedupes_input(tmp_path: Path):
    assert register_tags(str(tmp_path), ["x", "x", " x "], day_str="2026-09-23") == ["x"]


def test_list_tags_orders_by_last_seen(tmp_path: Path):
    register_tags(str(tmp_path), ["旧"], day_str="2026-09-01")
    register_tags(str(tmp_path), ["新"], day_str="2026-09-23")
    assert [r["tag"] for r in list_tags(str(tmp_path))][0] == "新"


# ---- 走 MEMORY 工具 --------------------------------------------------------

def test_memory_write_registers_tags(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path))
    res = memory_write(
        {
            "action": "append",
            "section": "proj",
            "content": "改造记忆系统",
            "tags": ["记忆系统", "计划"],
        },
        {},
    )
    assert res.ok
    assert "tags=" in res.data
    assert {r["tag"] for r in list_tags(str(tmp_path))} == {"记忆系统", "计划"}


def test_memory_write_without_tags_keeps_index_empty(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path))
    res = memory_write({"action": "append", "section": "proj", "content": "无标签"}, {})
    assert res.ok
    assert list_tags(str(tmp_path)) == []


def test_memory_read_tags_action(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path))
    register_tags(str(tmp_path), ["甲", "乙"], day_str="2026-09-23")
    res = memory_read({"action": "tags"}, {})
    assert res.ok
    assert res.data["count"] == 2
    assert {r["tag"] for r in res.data["tags"]} == {"甲", "乙"}


def test_memory_read_tags_action_empty(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path))
    res = memory_read({"action": "tags"}, {})
    assert res.ok
    assert res.data == {"tags": [], "count": 0}
