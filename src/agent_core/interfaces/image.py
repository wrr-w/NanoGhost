from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class ImagePort(ABC):
    """图片存储端口：引用计数管理。"""

    @abstractmethod
    def add_image(self, base64: str) -> str: ...

    @abstractmethod
    def get_image(self, image_id: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def get_images_batch(self, ids: List[str]) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def increment_references(self, ids: List[str]) -> None: ...

    @abstractmethod
    def decrement_references(self, ids: List[str]) -> List[str]: ...
