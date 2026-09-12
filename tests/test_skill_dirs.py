"""实例级技能目录的解析。

技能（像 prompts / memory.md 一样）属于**实例**，不属于某个渠道。历史上只有 CLI
那条路会去读 `config.yaml` 的 skills 段，飞书 worker 建 Agent 时没传 extra_dirs，
于是实例里放的技能对机器人凭空消失、只看得见全局 ~/.agents/skills。这里守住解析
规则本身，另外用源码断言守住两个入口都调了它。
"""

import os
from pathlib import Path

import pytest

from agent_core.skill.discovery import resolve_instance_skill_dirs

REPO = Path(__file__).resolve().parent.parent


def _mk_skill(d: Path, name: str = "") -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name or d.name}\ndescription: y\n---\nbody\n", encoding="utf-8",
    )


def _write_cfg(inst: Path, body: str) -> None:
    (inst / "config.yaml").write_text(body, encoding="utf-8")


def _norm(paths) -> list:
    """config.yaml 里写的是 ./vendor/sk 这种，拼出来会带正斜杠 —— 比路径别比分隔符。"""
    return [os.path.normpath(str(p)) for p in paths]


def test_no_instance_dir(monkeypatch):
    monkeypatch.delenv("INSTANCE_DIR", raising=False)
    assert resolve_instance_skill_dirs() == []


def test_falls_back_to_instance_skills_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("INSTANCE_DIR", raising=False)
    _mk_skill(tmp_path / "skills")
    assert resolve_instance_skill_dirs(str(tmp_path)) == [str(tmp_path / "skills")]


def test_missing_skills_dir_is_not_invented(tmp_path, monkeypatch):
    monkeypatch.delenv("INSTANCE_DIR", raising=False)
    assert resolve_instance_skill_dirs(str(tmp_path)) == []


def test_skills_dirs_wins_and_accepts_relative(tmp_path, monkeypatch):
    monkeypatch.delenv("INSTANCE_DIR", raising=False)
    _mk_skill(tmp_path / "skills")
    _mk_skill(tmp_path / "vendor" / "sk")
    _write_cfg(tmp_path, "skills:\n  dirs:\n    - ./vendor/sk\n    - .\n")
    assert _norm(resolve_instance_skill_dirs(str(tmp_path))) == _norm([
        tmp_path / "vendor" / "sk", tmp_path,
    ])


def test_skills_dirs_drops_agents_skills_dir_env(tmp_path, monkeypatch):
    """配了 dirs 就以它为准，不再让 discovery 去扫全局目录。"""
    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTS_SKILLS_DIR", str(tmp_path / "global"))
    _mk_skill(tmp_path / "vendor" / "sk")
    _write_cfg(tmp_path, "skills:\n  dirs:\n    - ./vendor/sk\n")
    resolve_instance_skill_dirs()
    assert "AGENTS_SKILLS_DIR" not in os.environ


def test_skills_dirs_keeps_env_var_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTS_SKILLS_DIR", str(tmp_path / "global"))
    _mk_skill(tmp_path / "skills")
    resolve_instance_skill_dirs()
    assert os.environ["AGENTS_SKILLS_DIR"] == str(tmp_path / "global")


def test_extra_dirs_is_the_legacy_name(tmp_path, monkeypatch):
    monkeypatch.delenv("INSTANCE_DIR", raising=False)
    _mk_skill(tmp_path / "a")
    _write_cfg(tmp_path, "skills:\n  extra_dirs:\n    - ./a\n")
    assert _norm(resolve_instance_skill_dirs(str(tmp_path))) == _norm([tmp_path / "a"])


def test_nonexistent_dirs_are_dropped(tmp_path, monkeypatch):
    monkeypatch.delenv("INSTANCE_DIR", raising=False)
    _write_cfg(tmp_path, "skills:\n  dirs:\n    - ./nope\n")
    assert resolve_instance_skill_dirs(str(tmp_path)) == []


def test_reads_instance_dir_from_env(tmp_path, monkeypatch):
    _mk_skill(tmp_path / "skills")
    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path))
    assert resolve_instance_skill_dirs() == [str(tmp_path / "skills")]


def test_allowlist_gates_what_directory_only_makes_discoverable(tmp_path, monkeypatch):
    """目录解析 ≠ 能用。白名单是另一道闸，两道都过才真的可用。

    "有实例但没配 enabled_only = 全禁"是有意的语义（和 MCP 的 enabled_only 一致），
    不是 bug —— 这条用例就是钉住它，免得以后有人看成漏了顺手改成"不限制"。
    """
    from agent_core.skill import discovery
    from agent_core.skill.registry import SkillRegistry

    # 把全局目录指到不存在的地方，免得真机上 ~/.agents/skills 里的技能混进来把计数搅浑
    monkeypatch.setattr(discovery, "AGENTS_SKILLS_DIR", str(tmp_path / "no-global"))
    _mk_skill(tmp_path / "skills" / "s1", name="s1")
    _mk_skill(tmp_path / "skills" / "s2", name="s2")
    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path))

    # 扫得到
    assert len(resolve_instance_skill_dirs()) == 1
    reg = SkillRegistry()
    assert reg.discover(extra_dirs=resolve_instance_skill_dirs()) == 2
    # 但没配白名单 → 一个都用不了
    assert reg.list_skill_defs() == []
    assert reg.get_skill_def("s1") is None
    assert reg.all_skill_names() == set()

    # 配了白名单 → 只有名字在里面那个能用
    _write_cfg(tmp_path, "skills:\n  enabled_only:\n    - s1\n")
    reg2 = SkillRegistry()
    reg2.discover(extra_dirs=resolve_instance_skill_dirs())
    assert [d.name for d in reg2.list_skill_defs()] == ["s1"]
    assert reg2.get_skill_def("s2") is None


def test_no_instance_dir_means_unrestricted(tmp_path, monkeypatch):
    """没有实例目录时是不限制 —— 和"有实例但没配"不是一回事。"""
    from agent_core.skill import discovery
    from agent_core.skill.registry import SkillRegistry

    monkeypatch.setattr(discovery, "AGENTS_SKILLS_DIR", str(tmp_path / "no-global"))
    monkeypatch.delenv("INSTANCE_DIR", raising=False)
    _mk_skill(tmp_path / "solo", name="solo")
    reg = SkillRegistry()
    assert reg.discover(extra_dirs=[str(tmp_path)]) == 1
    assert [d.name for d in reg.list_skill_defs()] == ["solo"]


def test_instance_dir_shadows_the_shared_dir(tmp_path, monkeypatch):
    """同名技能，实例目录里的那份赢 —— 本地覆盖全局。

    扫描顺序就是优先级（`discover_skills` 里 extra_dirs 排在 `~/.agents/skills`
    前面，先扫到的赢）。反过来的话，在实例里放个同名技能改半天也不生效，且没有报错。
    """
    from agent_core.skill import discovery

    shared = tmp_path / "shared"
    inst = tmp_path / "inst" / "skills"
    monkeypatch.setattr(discovery, "AGENTS_SKILLS_DIR", str(shared))
    _mk_skill(shared / "dup", name="dup")
    _mk_skill(inst / "dup", name="dup")
    # 两份内容不同才能看出来赢的是谁
    (shared / "dup" / "SKILL.md").write_text(
        "---\nname: dup\ndescription: 全局那份\n---\nbody\n", encoding="utf-8")
    (inst / "dup" / "SKILL.md").write_text(
        "---\nname: dup\ndescription: 实例那份\n---\nbody\n", encoding="utf-8")

    found = discovery.discover_skills(extra_dirs=[str(inst)], force=True)
    dups = [s for s in found if s.name == "dup"]
    assert len(dups) == 1, "同名技能应该只出来一份"
    assert dups[0].description == "实例那份"
    got = os.path.realpath(os.path.dirname(os.path.dirname(dups[0].filepath)))
    assert got == os.path.realpath(str(inst))


def test_agent_actually_sees_instance_skills(tmp_path, monkeypatch):
    """把两半接起来：解析器给出目录，Agent 真能发现里面的技能。

    原来只有 CLI 走这个组合，飞书 worker 建 Agent 时没传 —— 这条用例走的正是飞书
    那条组合（resolve → Agent），它在旧代码下会失败。

    config.yaml 里显式写了 enabled_only：SkillRegistry 把"实例目录存在但没配
    enabled_only"当成"一个技能都不给"（见 _enabled_only），所以不写全名的话这条
    用例会因为白名单为空而失败，测不到我们想测的目录解析。
    """
    from agent_core import Agent
    from tests.test_basic import MockDatabase, MockLLM

    _mk_skill(tmp_path / "skills" / "instance-only", name="instance-only")
    _write_cfg(tmp_path, "skills:\n  enabled_only:\n    - instance-only\n")
    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path))
    agent = Agent(
        db=MockDatabase(), llm=MockLLM(), namespace="t",
        skill_extra_dirs=resolve_instance_skill_dirs(), auto_register_tools=False,
    )
    assert "instance-only" in [d.name for d in agent.list_skill_defs()]


@pytest.mark.parametrize("path", ["run.py", "src/agent_core/cli.py"])
def test_every_channel_entrypoint_passes_instance_skills(path):
    """守这个不是洁癖：漏掉的正是当初那个 bug —— 没人报错，技能就是不出现。"""
    text = (REPO / path).read_text(encoding="utf-8")
    assert "skill_extra_dirs=resolve_instance_skill_dirs()" in text
