from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class DatabasePort(ABC):
    """Agent 持久化端口：会话、消息、图片、记忆卡片、流程图边。"""

    # --- 会话 ---
    @abstractmethod
    def create_agent_session(self, title: str = "新对话") -> str: ...

    @abstractmethod
    def get_agent_session(self, session_id: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def update_agent_session_title(self, session_id: str, title: str) -> None: ...

    @abstractmethod
    def list_agent_sessions(self, limit: int = 50) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def delete_agent_session(self, session_id: str) -> bool: ...

    # --- 消息 ---
    @abstractmethod
    def add_agent_message(
        self, session_id: str, role: str, content: str,
        type: str = "text", steps_json: Optional[str] = None,
    ) -> str: ...

    @abstractmethod
    def get_agent_messages(self, session_id: str) -> List[Dict[str, Any]]: ...

    # --- 图片 ---
    @abstractmethod
    def get_agent_images_batch(self, image_ids: List[str]) -> List[Dict[str, Any]]: ...

    # --- 记忆卡片（支持 namespace 隔离） ---
    @abstractmethod
    def load_all_memory_cards(
        self, namespace: Optional[str] = None,
    ) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def save_memory_card(self, card: Dict[str, Any]) -> None: ...

    @abstractmethod
    def delete_memory_card(self, card_id: str) -> bool: ...

    # --- 流程图边 ---
    @abstractmethod
    def load_all_memory_edges(self) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def save_memory_edge(self, edge: Dict[str, Any]) -> None: ...
