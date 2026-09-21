# -*- coding: utf-8 -*-
"""P2 单测：出站路由（模型自由 + 护栏）+ send_message 工具 + 出站镜像。"""

from __future__ import annotations

from agent_core.channel.base import Channel
from agent_core.channel.registry import ChannelRegistry
from agent_core.router import MAX_HOP, RateLimiter, Router


class FakeChannel(Channel):
    def __init__(self, name="fake"):
        self.name = name
        self.calls = []
        self.fail = False

    def send(self, target, text):
        self.calls.append((target, text))
        return not self.fail

    def reply(self, message_id, text):
        self.calls.append((message_id, text))
        return not self.fail


def _mk_router(name="fake"):
    reg = ChannelRegistry()
    ch = FakeChannel(name)
    reg.register(ch)
    return Router(registry=reg), ch


# ── resolve ──────────────────────────────────────────────
def test_resolve_defaults_to_source():
    r = Router()
    assert r.resolve(None, {}) == []
    assert r.resolve(None, {"source_addr": "feishu:oc_x"}) == ["feishu:oc_x"]
    assert r.resolve("reply", {"source_addr": "feishu:oc_x"}) == ["feishu:oc_x"]
    assert r.resolve([], {"source_addr": "feishu:oc_x"}) == ["feishu:oc_x"]
    assert r.resolve("feishu:ou_a", {}) == ["feishu:ou_a"]
    assert r.resolve(["feishu:ou_a", "feishu:ou_a", "feishu:oc_b"], {}) == ["feishu:ou_a", "feishu:oc_b"]


# ── deliver / fan-out ────────────────────────────────────
def test_deliver_fanout():
    r, ch = _mk_router()
    rep = r.deliver(["fake:a", "fake:b"], "hi")
    assert rep["ok"] and rep["sent"] == ["fake:a", "fake:b"]
    assert ch.calls == [("a", "hi"), ("b", "hi")]


def test_deliver_empty_text_and_no_target():
    r, ch = _mk_router()
    assert r.deliver(["fake:a"], "   ")["ok"] is False
    assert r.deliver([], "hi")["ok"] is False


def test_deliver_no_channel():
    r = Router(registry=ChannelRegistry())
    rep = r.deliver(["nope:a"], "hi")
    assert rep["failed"] == ["nope:a"]
    assert rep["reasons"]["nope:a"] == "no_channel"


# ── 出站镜像 ─────────────────────────────────────────────
def test_mirror_skips_source():
    r, ch = _mk_router()
    mirrored = []
    r.set_mirror(lambda addr, text: mirrored.append((addr, text)))
    rep = r.deliver(["fake:a", "fake:src"], "hi", source_addr="fake:src")
    assert rep["ok"]
    assert mirrored == [("fake:a", "hi")]        # 来源会话不镜像


def test_mirror_not_called_on_failure():
    r, ch = _mk_router()
    ch.fail = True
    mirrored = []
    r.set_mirror(lambda addr, text: mirrored.append((addr, text)))
    rep = r.deliver(["fake:a"], "hi")
    assert rep["failed"] == ["fake:a"]
    assert mirrored == []


# ── 护栏：ACL（按 agent）──────────────────────────────────
def test_acl_denies_by_agent():
    r, ch = _mk_router()
    r.acl.set("ns", ["fake:allowed"])            # ns 只能发 fake:allowed
    rep = r.deliver(["fake:allowed", "fake:denied"], "hi", agent_key="ns")
    assert rep["sent"] == ["fake:allowed"]
    assert rep["skipped"] == ["fake:denied"]
    assert rep["reasons"]["fake:denied"] == "acl_denied"
    # 未配置的 agent → 放开
    rep2 = r.deliver(["fake:denied"], "hi", agent_key="other")
    assert rep2["sent"] == ["fake:denied"]


# ── 护栏：限速 ───────────────────────────────────────────
def test_rate_limit():
    r, ch = _mk_router()
    r.rate = RateLimiter(per_window=1, window=60)
    assert r.deliver(["fake:a"], "1")["sent"] == ["fake:a"]
    rep2 = r.deliver(["fake:a"], "2")
    assert rep2["skipped"] == ["fake:a"]
    assert rep2["reasons"]["fake:a"] == "rate_limited"


# ── 护栏：防环（hop）─────────────────────────────────────
def test_hop_guard():
    r, ch = _mk_router()
    rep = r.deliver(["fake:a"], "x", hop=MAX_HOP + 1)
    assert rep["ok"] is False and "防环" in rep["error"]


# ── 出站：默认回来源 + 审计 ───────────────────────────────
def test_outbound_defaults_to_source_and_audits():
    from agent_core.channel.route import RouteEnvelope

    r, ch = _mk_router()
    env = RouteEnvelope(
        direction="outbound",
        kind="text",
        delivery="send",
        to=[],                              # 空 → 缺省回来源
        source_addr="fake:src",
        blocks=[{"type": "text", "text": "hi"}],
    )
    rep = r.submit(env)
    assert rep["sent"] == ["fake:src"]
    assert r.auditor.recent(1)[0]["action"] == "sent"


# ── send_message 工具 ────────────────────────────────────
def test_send_message_tool_default_and_fanout():
    from agent_core.channel.registry import get_registry
    from agent_core.tool.builtins.send import send_message

    fc = FakeChannel("feishu")
    get_registry().register(fc)                  # 全局注册一个假飞书通道
    ctx = {"channel_ctx": {"chat_id": "oc_x", "platform": "feishu"}, "namespace": "ns"}

    # 默认 → 回来源
    res = send_message({"blocks": [{"type": "text", "text": "hello"}]}, ctx)
    assert res.ok and res.data["sent"] == ["feishu:oc_x"]
    assert fc.calls == [("oc_x", "hello")]

    # 空 blocks → 失败
    assert send_message({"blocks": []}, ctx).ok is False

    # 一对多
    fc.calls.clear()
    res3 = send_message(
        {
            "to": ["feishu:ou_a", "feishu:oc_b"],
            "delivery": "send",
            "blocks": [{"type": "markdown", "text": "hi all"}],
        },
        ctx,
    )
    assert res3.ok and fc.calls == [("ou_a", "hi all"), ("oc_b", "hi all")]


def test_send_message_registered():
    from agent_core.tool.builtins import register_builtins
    from agent_core.tool.registry import ToolRegistry

    reg = ToolRegistry()
    register_builtins(reg)
    assert reg.get_definition("send_message") is not None
