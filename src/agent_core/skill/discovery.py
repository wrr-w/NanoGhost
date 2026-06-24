import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import SkillDefinition, SkillGroup

logger = logging.getLogger("agent_core")

# 技能根目录：默认 ~/.agents/skills，可通过环境变量覆盖（用于多实例隔离）
AGENTS_SKILLS_DIR = os.path.expanduser(os.getenv("AGENTS_SKILLS_DIR", "~/.agents/skills"))

# 缓存上次扫描时间戳和结果（按根目录缓存）
_cache: Dict[str, Tuple[float, List[SkillDefinition]]] = {}


def _list_subdirs(basedir: str) -> List[str]:
    """列出 basedir 下的所有子目录名（非递归）。"""
    try:
        return [d.name for d in Path(basedir).iterdir() if d.is_dir()]
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        return []


def _has_skill_md(directory: str) -> bool:
    return os.path.isfile(os.path.join(directory, "SKILL.md"))


def _is_category_dir(directory: str) -> bool:
    """判断一个目录是否为技能分组目录：包含至少一个子目录且有 SKILL.md。"""
    if not os.path.isdir(directory):
        return False
    for sub in _list_subdirs(directory):
        if _has_skill_md(os.path.join(directory, sub)):
            return True
    return False


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

            if value.startswith("[") and value.endswith("]"):
                parsed_list = [v.strip().strip('"').strip("'") for v in value[1:-1].split(",") if v.strip()]
                value = parsed_list

            if indent == 0:
                current_nested = None
                current_nested_key = None
                if value:
                    result[key] = value
                else:
                    result[key] = {}
                    current_nested = result[key]
                    current_nested_key = key
            elif indent > 0 and current_nested is not None:
                current_nested[key] = value.strip('"').strip("'") if isinstance(value, str) else value
        else:
            if current_nested_key and isinstance(result.get(current_nested_key), dict):
                continue
            if current_nested_key and isinstance(result.get(current_nested_key), str):
                result[current_nested_key] += " " + stripped

    return result, content


def load_skill_from_dir(skill_dir: str, group: str = "") -> Optional[SkillDefinition]:
    """从指定目录加载 SKILL.md，返回 SkillDefinition。

    Args:
        skill_dir: 技能目录路径。
        group: 所属分组名称（空表示无分组）。
    """
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
        group=group,
        license=license_val,
        compatibility=compatibility,
        version=version,
        platforms=platforms,
        tags=tags,
        related_skills=related_skills,
        metadata=metadata,
    )


def _load_group_skills(base_dir: str, group_name: str, loaded_names: Set[str]) -> Tuple[List[SkillDefinition], Optional[SkillGroup]]:
    """递归加载一个分组目录下的所有技能。

    Args:
        base_dir: 分组目录路径。
        group_name: 分组名称。
        loaded_names: 已加载技能名集合（避免重复）。

    Returns:
        (skills_list, group_definition)
        group_definition 为 None 表示隐式分组（无 SKILL.md）。
    """
    skills: List[SkillDefinition] = []
    group_def: Optional[SkillGroup] = None

    # 检查分组目录本身是否有 SKILL.md（作为分组入口技能）
    if _has_skill_md(base_dir):
        sd = load_skill_from_dir(base_dir, group="")
        if sd is not None:
            group_def = SkillGroup(name=group_name, description=sd.description)
            # 把分组入口也注册为可用的技能（name=分组名，group=分组名）
            entry_sd = SkillDefinition(
                name=group_name,
                description=sd.description,
                content=sd.content,
                filepath=sd.filepath,
                group=group_name,
                tags=sd.tags,
                metadata=sd.metadata,
            )
            loaded_names.add(group_name)
            skills.append(entry_sd)

    # 扫描子目录
    for sub_name in _list_subdirs(base_dir):
        sub_dir = os.path.join(base_dir, sub_name)
        if not _has_skill_md(sub_dir):
            continue
        if sub_name in loaded_names:
            continue
        skill = load_skill_from_dir(sub_dir, group=group_name)
        if skill is not None:
            loaded_names.add(sub_name)
            skills.append(skill)

    if not group_def:
        group_def = SkillGroup(name=group_name)

    desc_suffix = f" — {group_def.description}" if group_def.description else ""
    logger.info(f"[SkillDiscovery] 分组 [{group_name}]{desc_suffix} ({len(skills)} 个子技能)")
    return skills, group_def


def _dir_mtime(basedir: str) -> float:
    """递归获取目录树下最新文件的 mtime。"""
    latest = 0.0
    try:
        for root, _dirs, files in os.walk(basedir):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    mt = os.path.getmtime(fp)
                    if mt > latest:
                        latest = mt
                except OSError:
                    pass
    except Exception:
        pass
    return latest


def _should_rescan(base: str) -> bool:
    mt = _dir_mtime(base)
    cached = _cache.get(base)
    if cached is None:
        return True
    cached_mt, _ = cached
    return mt > cached_mt


def discover_skills(extra_dirs: Optional[List[str]] = None, force: bool = False) -> List[SkillDefinition]:
    """从 ~/.agents/skills 发现所有 SKILL.md 技能（支持递归分组结构）。

    目录结构约定：
        ~/.agents/skills/
            lark/                          ← 分组目录（显式：有 SKILL.md 且有子技能）
                SKILL.md                   ← 可选，分组描述
                lark-im/
                    SKILL.md
                lark-calendar/
                    SKILL.md
            local-search/                  ← 平铺技能（有 SKILL.md，无子技能）
                SKILL.md

    Args:
        extra_dirs: 额外扫描目录（运行时传入，用于测试或动态加载）。

    Returns:
        SkillDefinition 列表（所有技能扁平化，分组信息在 group 字段）。
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
        if not force and not _should_rescan(base):
            _, cached_results = _cache.get(base, (0, []))
            results.extend(cached_results)
            for r in cached_results:
                loaded_names.add(r.name)
            continue

        base_results: List[SkillDefinition] = []

        for entry_name in _list_subdirs(base):
            entry_dir = os.path.join(base, entry_name)

            # 情况 A: 分组目录（包含子技能）
            if _is_category_dir(entry_dir):
                sub_skills, group_def = _load_group_skills(entry_dir, entry_name, loaded_names)
                base_results.extend(sub_skills)
                # 如果分组有 SKILL.md，其本身也会作为 group_def 注册（作为分组元信息）
                # 不把分组本身当作普通技能添加到列表中

            # 情况 B: 平铺技能（有 SKILL.md，不是分组）
            elif _has_skill_md(entry_dir):
                if entry_name in loaded_names:
                    continue
                skill = load_skill_from_dir(entry_dir, group="")
                if skill is not None:
                    loaded_names.add(entry_name)
                    base_results.append(skill)

            # 情况 C: 隐式分组（无 SKILL.md 但有子技能）
            else:
                has_sub_skills = any(
                    _has_skill_md(os.path.join(entry_dir, sub))
                    for sub in _list_subdirs(entry_dir)
                )
                if has_sub_skills:
                    sub_skills, _ = _load_group_skills(entry_dir, entry_name, loaded_names)
                    base_results.extend(sub_skills)

        results.extend(base_results)
        _cache[base] = (time.time(), base_results)

    return results


def discover_groups(extra_dirs: Optional[List[str]] = None, force: bool = False) -> List[SkillGroup]:
    """发现技能分组（含子技能列表）。

    返回 SkillGroup 列表，按分组名称排序。
    无分组的技能归入 "other" 组。
    """
    skills = discover_skills(extra_dirs=extra_dirs, force=force)
    groups_map: Dict[str, SkillGroup] = {}

    for sd in skills:
        g = sd.group or "_ungrouped"
        if g not in groups_map:
            groups_map[g] = SkillGroup(name=g)
        groups_map[g].skills.append(sd)

    # 按名称排序
    result = []
    for gname in sorted(groups_map.keys()):
        if gname == "_ungrouped":
            continue
        groups_map[gname].skills.sort(key=lambda s: s.name)
        result.append(groups_map[gname])
    if "_ungrouped" in groups_map:
        groups_map["_ungrouped"].skills.sort(key=lambda s: s.name)
        result.append(groups_map["_ungrouped"])

    return result
