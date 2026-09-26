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


def append_daily_line(instance_dir: str, line: str, day_str: str | None = None) -> Path:
    """往当日的 memory.daily/YYYY-MM-DD.md 追加一行。

    自动创建 memory.daily/ 目录与当日文件（长期/当日分层的**写入侧**；
    此前只有读侧，目录从未被创建，这也是 daily 记忆一直为空的原因）。
    """
    import datetime as _dt

    day = day_str or _dt.date.today().isoformat()
    ensure_memory_layout(instance_dir)
    path = daily_memory_path(instance_dir, day)
    if not path.is_file():
        path.write_text("# NanoGhost Daily Memory\n\n", encoding="utf-8")
    text = (line or "").rstrip()
    if text:
        with path.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    return path


# ---------------------------------------------------------------------------
# 注入策略：默认只把 memory.md 的「章节索引」注入 system prompt，
# 正文按需用 memory_read 工具取（见 docs/memory-system-v3-spec.md：
# "No automatic injection except memory.md section index"）。
# 实例 config.yaml 可覆盖：
#   memory:
#     inject: index        # index(默认) | full(旧行为) | off
#     inline_max_lines: 15 # ≤N 行的短章节直接内联
#     index_max_chars: 2500# 注入块硬上限（字符）
# ---------------------------------------------------------------------------

MEMORY_INJECT_MODES = ("index", "full", "off")


def truncate_memory(text: str, max_chars: int | None) -> str:
    """硬上限截断，并留下"怎么取全文"的提示。"""
    if not max_chars or len(text) <= max_chars:
        return text
    note = '\n\n…（索引已截断；用 memory_read(action="index" | "section" | "detail") 取全文）'
    keep = max(0, max_chars - len(note))
    return text[:keep].rstrip() + note


def parse_memory_sections(text: str) -> list[dict]:
    """按 "## " 切章节，返回 [{"section": 名称, "lines": [非空行, ...]}]。

    解析口径与 memory_read(action="index"/"section") 保持一致，避免两处显示不一致。
    """
    sections: list[dict] = []
    current: str | None = None
    lines: list[str] = []
    for line in text.split("\n"):
        if line.startswith("## "):
            if current is not None:
                sections.append({"section": current, "lines": [l for l in lines if l.strip()]})
            current = line.strip("# ").strip()
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections.append({"section": current, "lines": [l for l in lines if l.strip()]})
    return sections


def build_memory_block(
    instance_dir: str,
    *,
    inline_max_lines: int = 15,
    max_chars: int = 2500,
    snippet_chars: int = 48,
) -> str | None:
    """生成 memory.md 的『索引块』：短章节内联正文，长章节只留一行摘要。

    分级降级，保证不超过 max_chars：
      ① 短章节内联正文 + 长章节带摘要
      ② 去掉内联正文（只留标题）+ 长章节带摘要
      ③ 全部只留章节名
    """
    text = _read_text(long_term_memory_path(instance_dir))
    if not text:
        return None
    sections = parse_memory_sections(text)
    if not sections:
        # 没有 "## " 章节：当作纯文本，按上限截断
        return truncate_memory(text, max_chars)

    inline: list[dict] = []
    indexed: list[tuple[str, int, str]] = []
    for s in sections:
        n = len(s["lines"])
        if 0 < n <= inline_max_lines:
            inline.append(s)
        else:
            first = s["lines"][0] if s["lines"] else ""
            indexed.append((s["section"], n, first[:snippet_chars]))

    usage = (
        '取正文：memory_read(action="section", section="<名称>")；'
        '按词搜索：memory_read(action="detail", section="<名称>", keyword="<词>")'
    )

    def render(with_inline_body: bool, with_snippets: bool) -> str:
        out: list[str] = [
            "> 长期记忆正文不常驻上下文：短章节已内联，长章节请用 memory_read 按需取。"
        ]
        if inline:
            out.append("")
            out.append("### 已内联（短章节）")
            for s in inline:
                out.append(f"- **{s['section']}**（{len(s['lines'])} 行）")
                if with_inline_body:
                    for l in s["lines"][:inline_max_lines]:
                        out.append("  " + l)
        if indexed:
            out.append("")
            out.append(f"### 仅索引（长章节 · {len(indexed)} 节）")
            for name, n, snip in indexed:
                if with_snippets and snip:
                    out.append(f"- **{name}** — {n} 行 · 「{snip}」")
                else:
                    out.append(f"- **{name}** — {n} 行")
            out.append("")
            out.append(usage)
        return "\n".join(out)

    for with_body, with_snip in ((True, True), (False, True), (False, False)):
        block = render(with_body, with_snip)
        if len(block) <= max_chars:
            return block
    return truncate_memory(render(False, False), max_chars)


def load_memory_inject_config(instance_dir: str) -> tuple[str, int, int]:
    """从实例 config.yaml 读注入策略，返回 (mode, inline_max_lines, index_max_chars)。"""
    mode, inline_max, max_chars = "index", 15, 2500
    cfg_path = _instance_root(instance_dir) / "config.yaml"
    try:
        import yaml

        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        m = cfg.get("memory")
        if isinstance(m, dict):
            mode = str(m.get("inject", mode)).strip().lower()
            inline_max = int(m.get("inline_max_lines", inline_max))
            max_chars = int(m.get("index_max_chars", max_chars))
    except Exception:
        pass
    if mode not in MEMORY_INJECT_MODES:
        mode = "index"
    return mode, max(1, inline_max), max(300, max_chars)


def build_injected_memory(instance_dir: str) -> str | None:
    """按实例配置生成要注入 system prompt 的记忆块（默认：仅索引）。"""
    mode, inline_max, max_chars = load_memory_inject_config(instance_dir)
    if mode == "off":
        return None
    if mode == "full":
        text = read_long_term_memory_block(instance_dir)
        return truncate_memory(text, max_chars) if text else None
    return build_memory_block(
        instance_dir, inline_max_lines=inline_max, max_chars=max_chars
    )
