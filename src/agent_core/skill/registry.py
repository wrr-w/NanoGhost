import logging
import os
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from agent_core.skill.base import Skill
from agent_core.skill.discovery import discover_skills
from agent_core.skill.models import SkillDefinition

logger = logging.getLogger("agent_core")


class SkillRegistry:
    """混合 Skill 注册中心。

    同时支持两种技能体系：
    1. 旧的 Python ABC Skill（execute/can_handle，用于代码级拦截）
    2. 新的 SKILL.md 指令集（兼容 opencode/claude-code/hermes 生态）
    """

    def __init__(self):
        # 旧体系：Python ABC Skill
        self._skills: Dict[str, Skill] = {}
        # 新体系：SKILL.md 指令集
        self._skill_defs: Dict[str, SkillDefinition] = {}

    # ======== 旧体系 API（保持向后兼容） ========

    def register(self, skill: Skill) -> None:
        name = skill.name or skill.__class__.__name__
        self._skills[name] = skill

    def unregister(self, name: str) -> None:
        self._skills.pop(name, None)

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def list(self) -> List[Skill]:
        return list(self._skills.values())

    def find_handler(
        self, parsed: Dict[str, Any], messages: List[Dict],
    ) -> Optional[Skill]:
        for skill in self._skills.values():
            if skill.can_handle(parsed, messages):
                return skill
        return None

    def execute(
        self,
        parsed: Dict[str, Any],
        messages: List[Dict],
        context: Dict[str, Any],
    ) -> Iterator[Tuple[str, Any]]:
        handler = self.find_handler(parsed, messages)
        if handler:
            yield from handler.execute(parsed, messages, context)

    # ======== 新体系 API（SKILL.md 生态兼容） ========

    def discover(self, extra_dirs: Optional[List[str]] = None) -> int:
        """从标准生态目录自动发现并注册 SKILL.md 技能。

        Args:
            extra_dirs: 额外的扫描目录。

        Returns:
            新发现的技能数量。
        """
        discovered = discover_skills(extra_dirs=extra_dirs)
        count = 0
        for sd in discovered:
            if sd.name not in self._skill_defs:
                self._skill_defs[sd.name] = sd
                count += 1
        if count > 0:
            logger.info(f"[SkillRegistry] 共发现 {count} 个 SKILL.md 技能")
        return count

    def get_skill_def(self, name: str) -> Optional[SkillDefinition]:
        """按名称获取 SKILL.md 技能定义。"""
        return self._skill_defs.get(name)

    def add_skill_def(self, sd: SkillDefinition) -> None:
        """直接注册一个 SkillDefinition 实例。"""
        self._skill_defs[sd.name] = sd

    def list_skill_defs(self) -> List[SkillDefinition]:
        """列出所有已发现的 SKILL.md 技能。"""
        return list(self._skill_defs.values())

    def list_skill_defs_dict(self) -> List[Dict[str, Any]]:
        """以字典列表形式返回技能定义（用于序列化/展示）。"""
        return [sd.to_dict() for sd in self._skill_defs.values()]

    def remove_skill_def(self, name: str) -> None:
        self._skill_defs.pop(name, None)

    def match_skills(
        self, query: str, top_k: int = 3,
    ) -> List[SkillDefinition]:
        """根据用户意图关键词匹配最相关的技能。

        使用简单的关键词匹配（不依赖 embedding），
        适合在 Agent 上下文窗口中列出相关技能。

        Args:
            query: 用户输入文本。
            top_k: 返回 TOP K 个结果。

        Returns:
            匹配到的技能列表（按相关度降序）。
        """
        if not self._skill_defs or not query:
            return []

        query_lower = query.lower()
        query_words = set(query_lower.split())

        scored: List[tuple[float, SkillDefinition]] = []
        for sd in self._skill_defs.values():
            score = 0.0
            text = (sd.name + " " + sd.description).lower()

            # 精确子串匹配权重高
            for word in query_words:
                if word in text:
                    score += 1.0
                if word in sd.name.lower():
                    score += 2.0  # 名字匹配权重翻倍

            # 完整子串匹配
            if query_lower in text:
                score += 3.0

            if score > 0:
                scored.append((score, sd))

        scored.sort(key=lambda x: -x[0])
        return [sd for _, sd in scored[:top_k]]

    def load_skill_content(self, name: str) -> Optional[str]:
        """加载指定技能的完整内容（用于按需注入）。

        Args:
            name: 技能名称。

        Returns:
            格式化的技能内容文本，或 None（技能不存在时）。
        """
        sd = self._skill_defs.get(name)
        if sd is None:
            return None
        return (
            f"## 技能: {sd.name}\n"
            f"{sd.description}\n\n"
            f"{sd.content}\n"
        )

    def build_skill_context(self) -> Optional[str]:
        """构建轻量技能索引，供注入 system prompt 使用。

        只列出技能名称和描述，模型按需通过
        `{{"use_skill": "skill-name"}}` 加载完整内容。

        与 Hermes/opencode 的 <available_skills> 模式兼容。

        Returns:
            格式化的索引文本块，或 None（无可用技能时）。
        """
        all_defs = self.list_skill_defs()
        if not all_defs:
            return None

        parts: List[str] = [
            "## 可用技能 (Skills)",
            "如需使用某个技能，调用 use_skill 工具加载其完整指令。",
            "",
            "<available_skills>",
        ]
        for sd in all_defs:
            skill_dir = os.path.dirname(sd.filepath)
            parts.append(f"  - **{sd.name}**: {sd.description} ({skill_dir}/)")
        parts.append("</available_skills>")
        parts.append("")

        return "\n".join(parts)

    # ======== 通用查询 ========

    def all_skill_names(self) -> Set[str]:
        """返回所有技能（新旧体系）的名称集合。"""
        names: Set[str] = set(self._skills.keys())
        names.update(self._skill_defs.keys())
        return names
