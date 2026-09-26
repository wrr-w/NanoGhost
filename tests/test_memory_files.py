from datetime import date
from pathlib import Path

from agent_core.memory.files import (
    append_daily_line,
    daily_memory_path,
    ensure_memory_layout,
    long_term_memory_path,
    read_daily_memory_block,
    read_long_term_memory_block,
)
from agent_core.tool.builtins.memory import memory_read, memory_write


def test_ensure_memory_layout_creates_daily_dir(tmp_path: Path):
    ensure_memory_layout(str(tmp_path))
    assert (tmp_path / "memory.daily").is_dir()


def test_long_term_memory_path_points_to_memory_md(tmp_path: Path):
    assert long_term_memory_path(str(tmp_path)) == tmp_path / "memory.md"


def test_daily_memory_path_uses_iso_date(tmp_path: Path):
    assert daily_memory_path(str(tmp_path), "2026-06-09") == tmp_path / "memory.daily" / "2026-06-09.md"


def test_read_long_term_memory_block_returns_none_for_missing_file(tmp_path: Path):
    assert read_long_term_memory_block(str(tmp_path)) is None


def test_read_daily_memory_block_returns_none_for_missing_day(tmp_path: Path):
    assert read_daily_memory_block(str(tmp_path), "2026-06-09") is None


def test_memory_write_append_daily_target(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path))

    result = memory_write(
        {
            "action": "append",
            "section": "daily_log",
            "content": "跟进 capture 状态",
            "target": "daily",
        },
        {},
    )

    assert result.ok
    daily_dir = tmp_path / "memory.daily"
    assert daily_dir.is_dir()

    today_path = daily_dir / f"{date.today().isoformat()}.md"
    assert today_path.is_file()
    assert "## daily_log" in today_path.read_text(encoding="utf-8")
    assert "- 跟进 capture 状态" in today_path.read_text(encoding="utf-8")


def test_memory_write_append_long_term_target(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path))

    result = memory_write(
        {
            "action": "append",
            "section": "project_context",
            "content": "保留长期背景",
            "target": "long_term",
        },
        {},
    )

    assert result.ok
    memory_path = tmp_path / "memory.md"
    assert memory_path.is_file()
    assert "## project_context" in memory_path.read_text(encoding="utf-8")
    assert "- 保留长期背景" in memory_path.read_text(encoding="utf-8")


def test_memory_read_section_from_daily_target(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path))
    daily_dir = tmp_path / "memory.daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / f"{date.today().isoformat()}.md").write_text(
        "# NanoGhost Memory\n\n## daily_log\n- 今日事项\n",
        encoding="utf-8",
    )

    result = memory_read(
        {
            "action": "section",
            "section": "daily_log",
            "target": "daily",
        },
        {},
    )

    assert result.ok
    assert result.data == {"section": "daily_log", "content": ["- 今日事项"]}


# ---- daily 写入侧（B 线：此前 daily 目录从未被创建） -------------------------

def test_append_daily_line_creates_dir_and_file(tmp_path: Path):
    path = append_daily_line(str(tmp_path), "- 10:30 修好写管线", day_str="2026-09-23")
    assert path == tmp_path / "memory.daily" / "2026-09-23.md"
    assert "- 10:30 修好写管线" in path.read_text(encoding="utf-8")


def test_append_daily_line_is_append_only(tmp_path: Path):
    append_daily_line(str(tmp_path), "- a", day_str="2026-09-23")
    append_daily_line(str(tmp_path), "- b", day_str="2026-09-23")
    text = (tmp_path / "memory.daily" / "2026-09-23.md").read_text(encoding="utf-8")
    assert text.count("- a") == 1
    assert text.count("- b") == 1
    assert read_daily_memory_block(str(tmp_path), "2026-09-23") is not None


def test_append_daily_line_ignores_blank(tmp_path: Path):
    path = append_daily_line(str(tmp_path), "   ", day_str="2026-09-24")
    assert path.is_file()


def test_append_daily_line_separates_days(tmp_path: Path):
    append_daily_line(str(tmp_path), "- day1", day_str="2026-09-23")
    append_daily_line(str(tmp_path), "- day2", day_str="2026-09-24")
    day1 = (tmp_path / "memory.daily" / "2026-09-23.md").read_text(encoding="utf-8")
    day2 = (tmp_path / "memory.daily" / "2026-09-24.md").read_text(encoding="utf-8")
    assert "day2" not in day1
    assert "day1" not in day2
