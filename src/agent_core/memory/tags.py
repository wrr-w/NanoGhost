"""开放标签索引（内核零预设）。

三条约定（对应「标签 × 日期、开放标签、内核零预设」）：

- **内核零预设**：内核不预置任何标签类目/分类树；标签是**开放字符串**，
  由调用方（agent / LLM）从内容里生成，抽不出就只挂日期。
- **开放标签**：标签可新增、可合并、可消亡；内核只做**存储 / 计数 / 查询**，
  不评判标签好坏、不排序推荐。
- **标签 × 日期**：索引按 (标签, 日期) 双键组织 —— 既能看「某标签的时间线」，
  也能看「某天发生了什么」。

存储：`<instance_dir>/memory.tags.json`
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_lock = threading.Lock()
TAGS_FILE = "memory.tags.json"


def tags_path(instance_dir: str) -> Path:
    return Path(instance_dir).expanduser().resolve() / TAGS_FILE


def load_tags(instance_dir: str) -> Dict[str, Any]:
    p = tags_path(instance_dir)
    if not p.is_file():
        return {"tags": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"tags": {}}
    if not isinstance(data, dict) or not isinstance(data.get("tags"), dict):
        return {"tags": {}}
    return data


def _save(instance_dir: str, data: Dict[str, Any]) -> None:
    p = tags_path(instance_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def register_tags(
    instance_dir: str,
    tags: List[str],
    *,
    day_str: Optional[str] = None,
    section: str = "",
) -> List[str]:
    """登记一次写入的开放标签（不存在则新建，存在则计数 +1）。

    返回实际登记的标签（去重、去空、保序）。空标签直接忽略 —— 内核不替内容
    造标签。
    """
    cleaned: List[str] = []
    seen = set()
    for t in tags or []:
        t = (t or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        cleaned.append(t)
    if not cleaned:
        return []

    day = day_str or _dt.date.today().isoformat()
    with _lock:
        data = load_tags(instance_dir)
        table = data.setdefault("tags", {})
        for t in cleaned:
            rec = table.get(t)
            if not isinstance(rec, dict):
                rec = {"first_seen": day, "last_seen": day, "count": 0, "dates": []}
            rec["last_seen"] = day
            rec["count"] = int(rec.get("count") or 0) + 1
            dates = [d for d in (rec.get("dates") or []) if d != day]
            dates.append(day)
            rec["dates"] = dates[-60:]
            if section:
                secs = [s for s in (rec.get("sections") or []) if s != section]
                secs.append(section)
                rec["sections"] = secs[-20:]
            table[t] = rec
        data["updated_at"] = time.time()
        _save(instance_dir, data)
    return cleaned


def list_tags(instance_dir: str, limit: int = 50) -> List[Dict[str, Any]]:
    """标签索引：按「最后出现日期倒序、其次计数」排列。"""
    data = load_tags(instance_dir)
    rows: List[Dict[str, Any]] = []
    for tag, rec in (data.get("tags") or {}).items():
        if not isinstance(rec, dict):
            continue
        rows.append({
            "tag": tag,
            "count": int(rec.get("count") or 0),
            "first_seen": rec.get("first_seen"),
            "last_seen": rec.get("last_seen"),
            "dates": rec.get("dates") or [],
            "sections": rec.get("sections") or [],
        })
    rows.sort(key=lambda r: (r.get("last_seen") or "", r["count"]), reverse=True)
    return rows[:limit]


__all__ = ["tags_path", "load_tags", "register_tags", "list_tags"]
