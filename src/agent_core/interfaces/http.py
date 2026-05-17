from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple


class HttpPort(ABC):
    """HTTP 端口：执行内部 API 调用。"""

    @abstractmethod
    def request(
        self, method: str, url: str,
        body: Optional[Dict] = None,
        timeout: int = 120,
    ) -> Tuple[int, Dict[str, Any]]: ...
