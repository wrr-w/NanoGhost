# -*- coding: utf-8 -*-
"""P4 单测：Watcher（事件源）+ Sink（结果出口）+ 通道管理接口 + list_endpoints 工具。"""

from __future__ import annotations

import json
import os
import tempfile

from agent_core.channel.base import Channel
from agent_core.channel.registry import get_registry
from agent_core.runtime.inbox import get_hub
from agent_core.runtime.watcher import Watcher
from agent_core.sink import Sink


class FakeChannel(Channel):
    def __init__(self, name="fake"):
        self.name = name
        self.calls = []

    def send(self, target, text):
        self.calls.append((target, text))
        return True

    def reply(self, message_id, text):
        return True


# ── Watcher ──────────────────────────────────────────────
def test_watcher_first_run_no_emit_then_change_emits():
    hub = get_hub()
    hub.drain("feishu:ow")
    state = {"v": 1}
    w = Watcher()
    w.watch("probe1", probe=lambda: {"n": state["v"]}, target="feishu:ow", interval=0)

    assert w.check_once() == []          # 首轮：装填，不产
    assert w.check_once() == []          # 无变化：不产

    state["v"] = 2
    out = w.check_once()
    assert len(out) == 1 and out[0]["watch"] == "probe1"

    evs = hub.drain("feishu:ow")
    assert any(e.kind == "event" and e.payload["watch"] == "probe1" for e in evs)


def test_watcher_interval_gating():
    w = Watcher()
    w.watch("p", probe=lambda: 1, target="t:x", interval=1000)
    w.check_once(now=100.0)               # 首轮总是探测（装填）
    assert w.list_watches()[0]["armed"] is True
    assert w.check_once(now=101.0) == []  # 未到间隔 → 跳过


# ── Sink ─────────────────────────────────────────────────
def test_sink_emit_recent_subscriber_and_persist():
    tmp = os.path.join(tempfile.gettempdir(), "ng_p4_sink.jsonl")
    try:
        os.remove(tmp)
    except OSError:
        pass
    s = Sink(path=tmp, capacity=10)
    got = []
    s.subscribe(lambda rec: got.append(rec.kind))

    s.emit("subagent_done", {"x": 1}, target="feishu:oc_a", summary="完成")
    s.emit("watch_change", {"y": 2})
    assert s.count() == 2
    assert got == ["subagent_done", "watch_change"]
    rec = s.recent(1)[0]
    assert rec["kind"] == "watch_change"

    with open(tmp, encoding="utf-8") as f:
        lines = [json.loads(ln) for ln in f if ln.strip()]
    assert len(lines) == 2 and lines[0]["kind"] == "subagent_done"
    try:
        os.remove(tmp)
    except OSError:
        pass


# ── 通道管理接口 ─────────────────────────────────────────
def test_channel_admin_overview_and_snapshot():
    from agent_core.channel.admin import channel_overview, endpoints_snapshot
    from agent_core.channel.directory import get_directory

    get_registry().register(FakeChannel("admch"))
    d = get_directory()
    d.register("admch:a", channel="admch", kind="group", meta={"chat_name": "A"})
    d.register("admch:b", channel="admch", kind="user", meta={"name": "B"})

    ov = [c for c in channel_overview() if c["channel"] == "admch"]
    assert ov and ov[0]["endpoints"] >= 2 and "text" in ov[0]["capabilities"]

    snap = endpoints_snapshot(channel="admch")
    assert snap["count"] >= 2
    snap2 = endpoints_snapshot(channel="admch", kind="user")
    assert all(e["kind"] == "user" for e in snap2["endpoints"])


def test_channel_metrics_counts_sent():
    from agent_core.channel.admin import channel_metrics
    from agent_core.channel.route import RouteEnvelope
    from agent_core.router import get_router

    get_registry().register(FakeChannel("admch"))   # 自足：确保通道在
    before = channel_metrics()["sent"]
    get_router().submit(RouteEnvelope(
        direction="outbound", kind="text", delivery="send",
        to=["admch:x"], blocks=[{"type": "text", "text": "hi"}],
    ))
    after = channel_metrics()
    assert after["sent"] >= before + 1


# ── list_endpoints 工具 ──────────────────────────────────
def test_list_endpoints_tool():
    from agent_core.tool.builtins.endpoints import list_endpoints

    res = list_endpoints({"channel": "admch"}, {})
    assert res.ok and res.data["count"] >= 1
    assert any(c["channel"] == "admch" for c in res.data["channels"])


def test_list_endpoints_registered():
    from agent_core.tool.builtins import register_builtins
    from agent_core.tool.registry import ToolRegistry

    reg = ToolRegistry()
    register_builtins(reg)
    assert reg.get_definition("list_endpoints") is not None
    assert reg.get_definition("send_message") is not None
