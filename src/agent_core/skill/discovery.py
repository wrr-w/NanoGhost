import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import SkillDefinition

logger = logging.getLogger("agent_core")

# 技能根目录：仅 ~/.agents/skills
AGENTS_SKILLS_DIR = os.path.expanduser("~/.agents/skills")


def _list_skill_names(basedir: str) -> List[str]:
    try:
        return [
            d.name for d in Path(basedir).iterdir()
            if d.is_dir() and (d / "SKILL.md").is_file()
        ]
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        return []


def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """解析 SKILL.md 的 YAML frontmatter。

    尝试使用 yaml 模块（如已安装），否则用简化的逐行解析器。
    兼容 opencode/claude-code 生态的标准 frontmatter 格式。
    """
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text.strip()

    fm_text = parts[1]
    content = parts[2].strip()

    # 优先使用 yaml（如果安装了 pyyaml）
    try:
        import yaml as _yaml
        fm = _yaml.safe_load(fm_text)
        if isinstance(fm, dict):
            return fm, content
    except ImportError:
        pass
    except Exception:
        pass

    # 简化回退解析器
    result: Dict[str, Any] = {}
    current_nested_key: Optional[str] = None
    current_nested: Optional[Dict[str, str]] = None

    for line in fm_text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())

        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()

            if indent == 0:
                current_nested = None
                current_nested_key = None
                if value:
                    result[key] = value.strip('"').strip("'")
                else:
                    result[key] = {}
                    current_nested = result[key]
                    current_nested_key = key
            elif indent > 0 and current_nested is not None:
                current_nested[key] = value.strip('"').strip("'")
        else:
            if current_nested_key and isinstance(result.get(current_nested_key), dict):
                continue
            if current_nested_key and isinstance(result.get(current_nested_key), str):
                result[current_nested_key] += " " + stripped

    return result, content


def load_skill_from_dir(skill_dir: str) -> Optional[SkillDefinition]:
    """从指定目录加载 SKILL.md，返回 SkillDefinition。"""
    skill_md = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(skill_md):
        return None

    try:
        with open(skill_md, encoding="utf-8") as f:
            raw = f.read()
    except Exception as e:
        logger.warning(f"[SkillDiscovery] 读取失败 {skill_md}: {e}")
        return None

    fm, content = _parse_frontmatter(raw)

    name = fm.get("name", "").strip()
    description = fm.get("description", "").strip()

    if not name:
        logger.warning(f"[SkillDiscovery] {skill_md} 缺少 frontmatter name，跳过")
        return None
    if not description:
        logger.warning(f"[SkillDiscovery] {skill_md} 缺少 frontmatter description，跳过")

    license_val = fm.get("license", "").strip() or None
    compatibility = fm.get("compatibility", "").strip() or None
    version = fm.get("version", "").strip() or None

    # platforms: YAML list e.g. [linux, macos, windows]
    raw_platforms = fm.get("platforms", [])
    platforms = [p.strip() for p in raw_platforms] if isinstance(raw_platforms, list) else []

    # metadata (包括 hermes 块)
    metadata: Dict[str, str] = {}
    raw_meta = fm.get("metadata", {})
    if isinstance(raw_meta, dict):
        for k, v in raw_meta.items():
            if isinstance(v, str):
                metadata[k] = v
            elif isinstance(v, list):
                metadata[k] = ",".join(str(i) for i in v)
    # 展平 hermes 子字段（值可能是 list 或 str）
    hermes_block = raw_meta.get("hermes", {}) if isinstance(raw_meta, dict) else {}
    if isinstance(hermes_block, dict):
        for hk, hv in hermes_block.items():
            if isinstance(hv, list):
                metadata[f"hermes.{hk}"] = ",".join(str(i) for i in hv)
            elif isinstance(hv, str):
                metadata[f"hermes.{hk}"] = hv

    # tags: 来自 metadata.hermes.tags 或顶层 tags
    tags: List[str] = []
    raw_tags = fm.get("tags", [])
    if isinstance(raw_tags, list):
        tags = [str(t).strip() for t in raw_tags if t]
    elif isinstance(raw_tags, str):
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    hermes_tags = (hermes_block.get("tags", []) if isinstance(hermes_block, dict) else [])
    if isinstance(hermes_tags, list):
        for t in hermes_tags:
            s = str(t).strip()
            if s and s not in tags:
                tags.append(s)

    related_skills: List[str] = []
    raw_related = fm.get("related_skills", [])
    if isinstance(raw_related, list):
        related_skills = [str(r).strip() for r in raw_related if r]
    elif isinstance(raw_related, str):
        related_skills = [r.strip() for r in raw_related.split(",") if r.strip()]
    hermes_related = (hermes_block.get("related_skills", []) if isinstance(hermes_block, dict) else [])
    if isinstance(hermes_related, list):
        for r in hermes_related:
            s = str(r).strip()
            if s and s not in related_skills:
                related_skills.append(s)

    return SkillDefinition(
        name=name,
        description=description,
        content=content or raw,
        filepath=os.path.abspath(skill_md),
        license=license_val,
        compatibility=compatibility,
        version=version,
        platforms=platforms,
        tags=tags,
        related_skills=related_skills,
        metadata=metadata,
    )


def discover_skills(extra_dirs: Optional[List[str]] = None) -> List[SkillDefinition]:
    """从 ~/.agents/skills 发现所有 SKILL.md 技能。

    Args:
        extra_dirs: 额外扫描目录（运行时传入，用于测试或动态加载）。

    Returns:
        SkillDefinition 列表。
    """
    search_paths: List[str] = []
    seen_paths: set[str] = set()

    # 1. ~/.agents/skills
    if os.path.isdir(AGENTS_SKILLS_DIR):
        norm = os.path.normpath(os.path.realpath(AGENTS_SKILLS_DIR))
        if norm not in seen_paths:
            seen_paths.add(norm)
            search_paths.append(AGENTS_SKILLS_DIR)

    # 2. 额外目录
    if extra_dirs:
        for d in extra_dirs:
            p = os.path.expanduser(d)
            norm = os.path.normpath(os.path.realpath(p))
            if os.path.isdir(p) and norm not in seen_paths:
                seen_paths.add(norm)
                search_paths.append(p)

    results: List[SkillDefinition] = []
    loaded_names: set[str] = set()

    for base in search_paths:
        for skill_name in _list_skill_names(base):
            if skill_name in loaded_names:
                continue
            skill = load_skill_from_dir(os.path.join(base, skill_name))
            if skill is not None:
                loaded_names.add(skill_name)
                results.append(skill)
                logger.info(
                    f"[SkillDiscovery] 发现技能 [{skill.name}]: {skill.description} "
                    f"({skill.filepath})"
                )

    return results
