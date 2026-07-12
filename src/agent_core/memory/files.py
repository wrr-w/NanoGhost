from __future__ import annotations

from pathlib import Path


def _instance_root(instance_dir: str) -> Path:
    return Path(instance_dir).expanduser().resolve()


def ensure_memory_layout(instance_dir: str) -> None:
    root = _instance_root(instance_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "memory.daily").mkdir(parents=True, exist_ok=True)


def long_term_memory_path(instance_dir: str) -> Path:
    return _instance_root(instance_dir) / "memory.md"


def daily_memory_path(instance_dir: str, day_str: str) -> Path:
    return _instance_root(instance_dir) / "memory.daily" / f"{day_str}.md"


def _read_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def read_long_term_memory_block(instance_dir: str) -> str | None:
    return _read_text(long_term_memory_path(instance_dir))


def read_daily_memory_block(instance_dir: str, day_str: str) -> str | None:
    return _read_text(daily_memory_path(instance_dir, day_str))
