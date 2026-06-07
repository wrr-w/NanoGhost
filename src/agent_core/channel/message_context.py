"""
通用消息上下文层。

定义平台无关的消息归一化格式和给 LLM 的文本构建器。

契约：
  各 adapter 把平台原始消息翻译成 MessageSource + MessageContext，
  ContextBuilder 负责将翻译结果格式化为 LLM 可读的文本。

消息流：
  平台原始消息 → adapter 解析 → MessageSource + MessageContext
    → ContextBuilder.build_user_message() → 给 LLM 的 user 文本
    → ContextBuilder.build_session_context() → 注入 system prompt（session 级，只一次）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


# ══════════════════════════════════════════════
# 类型枚举
# ══════════════════════════════════════════════


class MessageType(str, Enum):
    """归一化后的消息类型。各 adapter 自行映射。"""
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    AUDIO = "audio"
    VIDEO = "video"
    POST = "post"               # 富文本/卡片
    STICKER = "sticker"
    COMMAND = "command"         # 斜杠命令
    SYSTEM = "system"           # 系统消息


class ChatType(str, Enum):
    PRIVATE = "p2p"
    GROUP = "group"
    CHANNEL = "channel"
    THREAD = "thread"


# ══════════════════════════════════════════════
# 数据类 — adapter 输出
# ══════════════════════════════════════════════


@dataclass
class MentionRef:
    """@提及引用。各 adapter 负责把平台原始 mention 映射到此格式。"""
    key: str = ""               # 消息中的占位符（@_user_1 / <@!123>）
    name: str = ""              # 显示名
    user_id: str = ""           # 平台用户 ID（路由用）
    is_all: bool = False
    is_bot: bool = False


@dataclass
class MessageSource:
    """消息来源——谁在什么场景下发了消息。

    adapter 必须填充的字段：
      platform, chat_id, chat_type, sender_id, sender_name
    """
    platform: str = "unknown"
    chat_id: str = ""
    chat_name: str = ""
    chat_type: str = "p2p"              # ChatType 枚举值

    sender_id: str = ""
    sender_name: str = ""
    user_id_alt: str = ""               # 跨 session 关联用的稳定 ID
    is_bot: bool = False

    thread_id: str = ""
    guild_id: str = ""
    chat_topic: str = ""                # 群公告/描述（扩展）

    @property
    def is_group(self) -> bool:
        return self.chat_type in ("group", "channel")

    @property
    def chat_label(self) -> str:
        return {"p2p": "私聊", "group": "群聊", "channel": "频道", "thread": "线程"}.get(self.chat_type, "会话")

    @property
    def display_name(self) -> str:
        if self.sender_name:
            return self.sender_name
        # 避免返回 raw open_id / union_id 等无意义 ID
        if self.sender_id and not self.sender_id.startswith("ou_") and not self.sender_id.startswith("on_"):
            return self.sender_id
        return "用户"


@dataclass
class MessageContext:
    """归一化后的消息内容。adapter 必须填充 text 和 message_type。"""
    text: str = ""                       # 原始文本（@mention 替换前）
    message_type: str = "text"
    message_id: str = ""

    # 回复引用
    parent_id: str = ""
    reply_to_text: str = ""
    root_id: str = ""

    # 提及
    mentions: List[MentionRef] = field(default_factory=list)

    # 语音 STT
    transcribed_text: str = ""

    # 平台原始数据（调试/透传）
    raw: Any = None

    @property
    def is_command(self) -> bool:
        return self.message_type == "command" or self.text.startswith("/")

    @property
    def bot_mentions(self) -> List[MentionRef]:
        return [m for m in self.mentions if m.is_bot]

    @property
    def user_mentions(self) -> List[MentionRef]:
        return [m for m in self.mentions if not m.is_bot and not m.is_all]


# ══════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════

_MENTION_PLACEHOLDER_RE = re.compile(r"@_user_\d+|@_all")
_MENTION_BOUNDARY_CHARS = {" ", "\t", "\n", "\r", "，", "。", "！", "？", "、", "）", "】", ">"}


# ══════════════════════════════════════════════
# ContextBuilder — adapter 调用的格式化入口
# ══════════════════════════════════════════════


class ContextBuilder:
    """通用上下文构建器。

    Adapter 把平台消息翻译成 MessageSource + MessageContext 后，
    用此 builder 生成给 LLM 的文本。

    用法：
        source = MessageSource(platform="feishu", sender_name="张三", ...)
        ctx = MessageContext(text="你好", ...)
        builder = ContextBuilder(bot_name="Assistant", bot_id="app_bot_123")

        # 注入 system prompt（session 开始调一次）
        sys_block = builder.build_session_context(source)

        # 生成 user 消息（每轮调一次）
        user_text = builder.build_user_message(source, ctx)
    """

    def __init__(self, bot_name: str = "", bot_id: str = ""):
        self._bot_name = bot_name.strip()
        self._bot_id = bot_id.strip()

    # ══════════════════════════════════════════════
    # 公开接口
    # ══════════════════════════════════════════════

    def build_session_context(self, source: MessageSource, *, shared_session: bool = False) -> str:
        """Build session context block injected into system prompt.

        When shared_session=True (group chat without per-user isolation),
        do NOT pin a single user name - instead note it's multi-user.
        Individual messages carry [sender_name] prefix for identification.
        """
        lines = [
            "## 当前会话上下文",
            "",
            f"**来源:** {source.platform.title()} ({source.chat_label}: {source.chat_name or source.chat_id})",
        ]
        if self._bot_name:
            lines.append(f"**当前身份:** {self._bot_name}")
        if source.chat_topic:
            lines.append(f"**群公告/描述:** {source.chat_topic}")
        if shared_session:
            lines.append("**会话类型:** 多人会话——每条消息前面会标注发送者姓名。")
        else:
            lines.append(f"**用户:** {source.display_name}")
        lines.append(f"**连接的平台:** local, {source.platform}: 已连接 \u2713")
        return "\n".join(lines)
    def build_user_message(self, source: MessageSource, ctx: MessageContext) -> str:
        """构建最终发给 LLM 的 user message 文本。

        处理链（顺序固定）：
          1. @_user_N 占位符 → @姓名
          2. 剥离 bot 自 @mention（开头/结尾）
          3. 非 bot 的 @提及 → [提及了: 姓名, ...]
          4. 回复引用 → [回复给: "原文"]
          5. [发送者名] 消息正文
          6. 语音转文字 → [语音: xxx]
        """
        text = ctx.text

        # 1. @mention 占位符替换
        text, non_bot_names = self._normalize_mentions(text, ctx.mentions)

        # 2. 剥离 bot 自 @mention
        text = self._strip_self_mentions(text)

        # 3. 非 bot 的 @提及 hint
        if non_bot_names:
            text = f"[提及了: {', '.join(non_bot_names)}]\n\n{text}"

        # 4. 回复引用
        if ctx.parent_id and ctx.reply_to_text:
            text = f'[回复给: "{ctx.reply_to_text[:200]}"]\n\n{text}'

        # 5. 发送者前缀
        prefix = source.display_name
        if prefix:
            text = f"[{prefix}] {text}"

        # 6. 语音转文字
        if ctx.transcribed_text:
            text = f"[语音: {ctx.transcribed_text}]\n\n{text}"

        return text.strip()

    # ══════════════════════════════════════════════
    # @mention 处理
    # ══════════════════════════════════════════════

    def _normalize_mentions(self, text: str, mentions: List[MentionRef]) -> Tuple[str, List[str]]:
        """替换 @_user_N 占位符为 @姓名。"""
        if not mentions or not text:
            return text, []

        mention_map: Dict[str, str] = {}
        non_bot_names: List[str] = []
        bot_keys: List[str] = []

        for ref in mentions:
            if not ref.key:
                continue
            display = ref.name or ref.user_id or "用户"
            mention_map[ref.key] = display
            if ref.is_bot:
                bot_keys.append(ref.key)
            elif not ref.is_all:
                non_bot_names.append(display)

        self._state_bot_keys = bot_keys
        self._state_mentions = mentions

        def _replace(m: re.Match) -> str:
            key = m.group(0)
            name = mention_map.get(key)
            if name:
                return f"@{name}"
            return "@all" if key in ("@_all", "@all") else " "

        return _MENTION_PLACEHOLDER_RE.sub(_replace, text).strip(), non_bot_names

    def _strip_self_mentions(self, text: str) -> str:
        """剥离开头和结尾的 bot 自身 @mention。"""
        bot_keys = getattr(self, "_state_bot_keys", [])
        if not bot_keys or not text:
            return text

        self_names = list({
            f"@{ref.name}" for ref in getattr(self, "_state_mentions", [])
            if ref.key in bot_keys and ref.name
        })

        if not self_names:
            return text

        remaining = text.lstrip()
        while True:
            changed = False
            for nm in self_names:
                if not remaining.startswith(nm):
                    continue
                after = remaining[len(nm):]
                if after and after[0] not in _MENTION_BOUNDARY_CHARS:
                    continue
                remaining = after.lstrip()
                changed = True
            if not changed:
                break

        while True:
            changed = False
            for nm in self_names:
                if remaining.endswith(nm):
                    remaining = remaining[:-len(nm)].rstrip()
                    changed = True
            if not changed:
                break

        return remaining

    # 每次 build_user_message 调用时临时设置的状态
    _state_bot_keys: List[str] = []
    _state_mentions: List[MentionRef] = []
