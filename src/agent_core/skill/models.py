from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SkillDefinition:
    """解析后的 SKILL.md 技能定义。

    与 opencode/claude-code/hermes 生态完全兼容。
    """
    name: str
    description: str
    content: str
    filepath: str
    license: Optional[str] = None
    compatibility: Optional[str] = None
    version: Optional[str] = None
    platforms: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    related_skills: List[str] = field(default_factory=list)
    metadata: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "license": self.license,
            "compatibility": self.compatibility,
            "version": self.version,
            "platforms": list(self.platforms),
            "tags": list(self.tags),
            "related_skills": list(self.related_skills),
            "metadata": dict(self.metadata),
        }
