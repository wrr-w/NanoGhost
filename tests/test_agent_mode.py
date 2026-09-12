"""AGENT_MODE 的单一真相：实例的 channel_directory.json。

这块以前是靠实例 .env 里的 AGENT_MODE 决定的，而 run.py 加载 .env 用的是
load_dotenv(override=True) —— 网关孵 worker 时传下去的环境变量会被文件顶掉。
症状：控制台里"启用飞书"点成功了，worker 起来却在跑 CLI、几秒就 EOFError 退出，
30 秒后体检再孵一遍，无限循环，而且一路都不报错。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

from agent_core.config import resolve_agent_mode  # noqa: E402


def _write_channel(inst: Path, enabled: bool) -> None:
    (inst / "channel_directory.json").write_text(
        json.dumps({"updated_at": None, "channels": {"feishu": {"enabled": enabled}}}),
        encoding="utf-8",
    )


# ── resolve_agent_mode ──


def test_channel_on_means_feishu(tmp_path):
    _write_channel(tmp_path, True)
    assert resolve_agent_mode(tmp_path) == ("feishu", "channel")


def test_channel_off_means_cli(tmp_path):
    _write_channel(tmp_path, False)
    assert resolve_agent_mode(tmp_path) == ("cli", "default")


def test_explicit_beats_channel(tmp_path):
    """网关孵 worker 时传的就是这个 explicit —— 它得赢。"""
    _write_channel(tmp_path, True)
    assert resolve_agent_mode(tmp_path, explicit="cli") == ("cli", "env")


def test_explicit_is_cleaned(tmp_path):
    _write_channel(tmp_path, False)
    assert resolve_agent_mode(tmp_path, explicit='  "FEISHU" ') == ("feishu", "env")


@pytest.mark.parametrize("content", ['{oops', '[]', '{"channels": []}', '{"channels": {"feishu": 1}}'])
def test_broken_channel_file_falls_back_to_cli(tmp_path, content):
    (tmp_path / "channel_directory.json").write_text(content, encoding="utf-8")
    assert resolve_agent_mode(tmp_path) == ("cli", "default")


def test_missing_instance_dir(tmp_path):
    assert resolve_agent_mode("") == ("cli", "default")
    assert resolve_agent_mode(None) == ("cli", "default")
    assert resolve_agent_mode(tmp_path / "nope") == ("cli", "default")


# ── setup_wizard 写的就是上面读的那份 ──


def test_wizard_writes_channel_file(tmp_path):
    from agent_core.setup_wizard import _write_channel_config

    path = _write_channel_config(str(tmp_path), feishu_enabled=True)
    assert Path(path) == tmp_path / "channel_directory.json"
    assert resolve_agent_mode(tmp_path) == ("feishu", "channel")
    # 临时文件不能留下 —— 留一个 .tmp 在实例目录里会被人当成配置读
    assert sorted(p.name for p in tmp_path.iterdir()) == ["channel_directory.json"]


def test_wizard_preserves_other_channels(tmp_path):
    """控制台可能已经往里写过别的通道，别一把抹掉。"""
    from agent_core.setup_wizard import _write_channel_config

    _write_channel(tmp_path, True)
    raw = json.loads((tmp_path / "channel_directory.json").read_text(encoding="utf-8"))
    raw["channels"]["cli"] = {"enabled": True}
    raw["keep_me"] = 1
    (tmp_path / "channel_directory.json").write_text(json.dumps(raw), encoding="utf-8")

    _write_channel_config(str(tmp_path), feishu_enabled=False)
    after = json.loads((tmp_path / "channel_directory.json").read_text(encoding="utf-8"))
    assert after["channels"]["feishu"]["enabled"] is False
    assert after["channels"]["cli"] == {"enabled": True}
    assert after["keep_me"] == 1


def test_wizard_rewrites_broken_file(tmp_path):
    from agent_core.setup_wizard import _write_channel_config

    (tmp_path / "channel_directory.json").write_text("{oops", encoding="utf-8")
    _write_channel_config(str(tmp_path), feishu_enabled=True)
    assert resolve_agent_mode(tmp_path) == ("feishu", "channel")


# ── 端到端：run.py 真的按这套规则分流 ──
#
# 起子进程而不是 import run：run.py 在导入期就动 os.environ、开日志文件，在这个
# 进程里 import 会把整个测试会话的环境改掉。子进程里 USERPROFILE 指向临时目录，
# 所以连 ~/.nanoghost/.env 也读不到 —— 不碰真机。

_SNIPPET = (
    "import run, os;"
    "print('MODE=%s SOURCE=%s' % (os.environ.get('AGENT_MODE'),"
    " os.environ.get('AGENT_MODE_SOURCE')))"
)


def _spawn(tmp_path: Path, env_txt: str, channel: bool | None, spawn_env: dict) -> str:
    home = tmp_path / "home"
    (home / ".nanoghost").mkdir(parents=True)
    inst = tmp_path / "inst"
    inst.mkdir()
    (inst / ".env").write_text(env_txt, encoding="utf-8")
    if channel is not None:
        _write_channel(inst, channel)

    env = dict(os.environ)
    env.pop("AGENT_MODE", None)
    env["USERPROFILE"] = str(home)  # Windows 的 expanduser("~") 认这个
    env["HOME"] = str(home)
    env["INSTANCE_DIR"] = str(inst)
    env["PYTHONPATH"] = str(REPO / "src")
    env.update(spawn_env)
    r = subprocess.run(
        [sys.executable, "-c", _SNIPPET], cwd=str(REPO), env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip().splitlines()[-1]


def test_env_file_cannot_override_channel(tmp_path):
    """回归用例：这就是当初那个 bug 的现场 —— .env 写 cli，通道开着。"""
    got = _spawn(tmp_path, "AGENT_MODE=cli\nLLM_API_KEY=x\n", True, {})
    assert got == "MODE=feishu SOURCE=channel"


def test_stale_env_mode_is_ignored(tmp_path):
    """.env 里写 feishu、通道关着 → 老老实实跑 CLI，不绕开通道开关。"""
    got = _spawn(tmp_path, "AGENT_MODE=feishu\nLLM_API_KEY=x\n", False, {})
    assert got == "MODE=cli SOURCE=default"


def test_spawner_intent_still_wins(tmp_path):
    """网关传的 AGENT_MODE 得压得住 .env —— 否则真正的 worker 会被文件带跑偏。"""
    got = _spawn(tmp_path, "AGENT_MODE=cli\nLLM_API_KEY=x\n", True, {"AGENT_MODE": "cli"})
    assert got == "MODE=cli SOURCE=env"
