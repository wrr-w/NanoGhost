# -*- coding: utf-8 -*-
"""file 消息块：信封归一化 + 通道分发 + 降级。"""
from __future__ import annotations

from agent_core.channel.route import make_file_block, normalize_blocks, normalize_files
from agent_core.router import Router


# ── 块模型 ──────────────────────────────────────────────

def test_make_file_block_str_and_dict():
    blk = make_file_block(["C:\\a\\报告.pdf", {"path": "D:/x/y.docx", "name": "文档.docx"}])
    assert blk["type"] == "file"
    assert blk["files"][0] == {"path": "C:\\a\\报告.pdf", "name": "报告.pdf"}
    assert blk["files"][1] == {"path": "D:/x/y.docx", "name": "文档.docx"}


def test_normalize_files_drops_invalid_and_caps_at_10():
    items = ["", {"path": ""}, "a.txt"] + [f"f{i}.txt" for i in range(20)]
    out = normalize_files(items)
    assert len(out) == 10
    assert out[0]["name"] == "a.txt"


def test_normalize_blocks_handles_file():
    blocks = normalize_blocks([
        {"type": "text", "text": "hi"},
        {"type": "file", "files": ["C:\\a\\b.pdf"]},
        {"type": "file", "files": []},   # 空 → 丢弃
        {"type": "file"},                # 无 files → 丢弃
    ])
    assert [b["type"] for b in blocks] == ["text", "file"]
    assert blocks[1]["files"][0] == {"path": "C:\\a\\b.pdf", "name": "b.pdf"}


def test_guess_file_type():
    from agent_core.channel.feishu.api import guess_file_type
    assert guess_file_type("a.pdf") == "pdf"
    assert guess_file_type("a.DOCX") == "doc"
    assert guess_file_type("a.xlsx") == "xls"
    assert guess_file_type("a.bin") == "stream"
    assert guess_file_type("") == "stream"


# ── 通道分发 ────────────────────────────────────────────

class _FakeIO:
    def __init__(self, res):
        self.res = res
        self.file_calls = []
        self.texts = []

    def send_files(self, chat_id, files):
        self.file_calls.append((chat_id, files))
        return self.res

    def send_text(self, chat_id, text):
        self.texts.append((chat_id, text))
        return True

    def send_markdown(self, chat_id, text):
        return True

    def reply(self, message_id, text):
        return True

    def reply_markdown(self, message_id, text):
        return True

    def send_images(self, chat_id, imgs):
        return {"ok": True}


def test_feishu_channel_send_blocks_file_ok():
    from agent_core.channel.feishu.channel import FeishuChannel
    io = _FakeIO({"ok": True, "sent": 1, "failed": 0, "errors": []})
    ch = FeishuChannel(io=io)
    ok = ch.send_blocks("oc_x", [{"type": "file", "files": [{"path": "C:\\a\\b.pdf", "name": "b.pdf"}]}])
    assert ok is True
    assert io.file_calls and io.file_calls[0][0] == "oc_x"
    assert io.file_calls[0][1][0]["name"] == "b.pdf"


def test_feishu_channel_file_falls_back_to_text():
    from agent_core.channel.feishu.channel import FeishuChannel
    io = _FakeIO({"ok": False, "sent": 0, "failed": 1, "errors": ["not_found"]})
    ch = FeishuChannel(io=io)
    ok = ch.send_blocks("oc_x", [{"type": "file", "files": [{"path": "C:\\no\\x.pdf", "name": "x.pdf"}]}])
    assert ok is True                      # 降级成文本，仍算处理成功
    assert io.texts and "x.pdf" in io.texts[0][1]


def test_feishu_channel_capability_declares_file():
    from agent_core.channel.feishu.channel import FeishuChannel
    ch = FeishuChannel(io=_FakeIO({"ok": True, "sent": 1}))
    prof = ch.message_capability_profile()
    assert prof["supports_file"] is True
    assert "file" in prof["block_types"]
    assert prof["limits"]["max_files_per_block"] >= 10
    assert "file" in ch.capabilities()


# ── 路由器：预览 + 无 send_files 的通道降级 ─────────────

def test_router_block_preview_file():
    assert Router._block_preview([{"type": "file", "files": [{"path": "a"}, {"path": "b"}]}]) == "[files:2]"


class _NoFileChannel:
    def __init__(self):
        self.sent = []

    def send(self, target, text):
        self.sent.append((target, text))
        return True

    def reply(self, message_id, text):
        return True


def test_router_channel_without_send_files_falls_back():
    r = Router.__new__(Router)  # 只测该方法，不跑 __init__
    ch = _NoFileChannel()
    ok = r._send_blocks_via_channel(
        ch,
        target="feishu:oc_x",
        blocks=[{"type": "file", "files": [{"path": "C:\\a\\b.pdf", "name": "b.pdf"}]}],
        delivery="send",
        reply_to=None,
    )
    assert ok is True
    assert ch.sent and "b.pdf" in ch.sent[0][1]
