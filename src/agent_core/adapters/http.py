from typing import Any, Dict, Optional, Tuple

import requests as http_requests

from agent_core.interfaces import HttpPort


class RequestsHttp(HttpPort):

    def request(self, method, url, body=None, timeout=120):
        if method == "GET":
            r = http_requests.get(url, timeout=timeout)
        else:
            r = http_requests.request(method, url, json=body or {}, timeout=timeout,
                                       headers={"Content-Type": "application/json"})
        data = r.json() if r.text else {}
        return r.status_code, data
