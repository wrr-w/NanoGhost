from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple


@dataclass
class SkillResult:
    """Skill 执行结果"""
    ok: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    events: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)


class Skill(ABC):
    """Agent Skill 基类。

    Skill 是 Agent 的可插拔能力单元,可以:
    - 处理特定的 action 类型（如 MEMORY 工具、need_slugs 等）
    - 注入额外消息到对话上下文
    - 产出事件（同主循环的事件体系）
    """

    name: str = ""
    description: str = ""

    @abstractmethod
    def can_handle(self, parsed: Dict[str, Any], messages: List[Dict]) -> bool:
        """判断此 Skill 是否能处理当前 LLM 输出。"""
        ...

    @abstractmethod
    def execute(
        self,
        parsed: Dict[str, Any],
        messages: List[Dict],
        context: Dict[str, Any],
    ) -> Iterator[Tuple[str, Any]]:
        """执行 Skill 逻辑,可 yield 事件。

        Args:
            parsed: LLM 输出的解析结果
            messages: 当前对话消息列表（可追加）
            context: 执行上下文,包含:
                - db: DatabasePort
                - llm: LLMPort
                - http: HttpPort
                - config: AgentConfig
                - agent: Agent 实例
                - session_id: Optional[str]
                - user_message: str
                - step_results: Dict[int, Dict]
                - all_steps_out: List[Dict]

        Yields:
            (event_type, event_data) 事件对,与主循环事件体系一致
        """
        ...
