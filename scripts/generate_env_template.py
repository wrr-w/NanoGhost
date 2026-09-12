"""从代码中扫描所有 os.getenv() 调用，生成 .env.example。每次构建前运行。"""
import os
import re
import sys
from pathlib import Path

ENV_CATEGORIES = {
    "LLM": ["LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_SUPPORTS_VISION"],
    "Embedding": ["EMBED_MODEL", "EMBED_BASE_URL", "EMBED_API_KEY", "EMBED_LOCAL_MODEL"],
    u"飞书通道": ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_BOT_NAME",
                 "FEISHU_BOT_OPEN_ID", "FEISHU_VERBOSE", "LARK_CLI_PROFILE",
                 "FEISHU_ENABLED"],
    # AGENT_MODE 不在列表里：它还被 run.py 读（作为"调用方显式指定"），但**不能**
    # 写进 .env —— 见 EXCLUDE 里的说明
    "Agent": ["AGENT_BASE_URL", "AGENT_DB_PATH"],
    u"实例隔离": ["INSTANCE_DIR", "AGENT_NAMESPACE", "AGENT_WORKDIR", "AGENT_PROMPTS_DIR"],
    u"技能/MCP": ["AGENTS_SKILLS_DIR", "NANOGHOST_GLOBAL_CONFIG",
                  "NANOGHOST_INSTANCES_ROOT", "NANOGHOST_RUNPY"],
    # 注意：更新源（repo / download_base / asset_prefix）配在 update.json 里，
    # 不是环境变量 —— 见 docs/UPDATING.md。这里只放升级流程本身的开关。
    u"更新": ["NANOGHOST_DISABLE_AUTO_UPDATE",
              "NANOGHOST_UPDATE_LOG", "NANOGHOST_UPDATE_RESULT"],
    u"网络": ["NO_PROXY"],
}

DEFAULTS = {
    "LLM_SUPPORTS_VISION": "false",
    "AGENT_BASE_URL": "http://127.0.0.1:8000",
    "NO_PROXY": "*",
    "FEISHU_VERBOSE": "false",
    "NANOGHOST_DISABLE_AUTO_UPDATE": "false",
}

EXCLUDE = {
    "SSL_CERT_FILE", "NANOGHOST_CALLER", "MCP_REFRESH_INTERVAL",
    "PYTHONUNBUFFERED", "INSTANCE_DIR",
    # 这两个是**代码往里写**的，写进 .env 只会把人带偏：
    #   AGENT_MODE   —— 代码读它，但只把"调用方显式指定的值"当数（网关孵 worker
    #                   时传）。实例 .env 里配了会被忽略，唯一开关是
    #                   channel_directory.json。见 src/agent_core/config.py。
    #   AGENT_MODE_SOURCE —— 诊断用，记录上一条的值是哪来的。
    "AGENT_MODE", "AGENT_MODE_SOURCE",
}

CATEGORY_COMMENTS = {
    "LLM": u"LLM 大模型配置（必需）",
    "Embedding": u"Embedding 配置（留空使用内置模型）",
    u"飞书通道": u"飞书通道配置\n"
              u"填好 APP_ID / APP_SECRET 之后，还要在实例的 channel_directory.json 里\n"
              u"把 feishu 打开（\"channels\": {\"feishu\": {\"enabled\": true}}）。\n"
              u"通道的唯一开关在那里，本文件不再决定跑不跑飞书。",
    "Agent": u"Agent 后端",
    u"实例隔离": u"多实例隔离配置",
    u"技能/MCP": u"技能 / MCP 配置",
    u"更新": u"升级流程开关（更新源配在 ~/.nanoghost/update.json）",
    u"网络": u"网络 / 代理",
}


def scan_env_vars(src_dirs: list[str]) -> set[str]:
    vars_found = set()
    for src_dir in src_dirs:
        for dirpath, _dirnames, filenames in os.walk(src_dir):
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    content = open(path, encoding="utf-8", errors="ignore").read()
                except Exception:
                    continue
                for m in re.finditer(r'os\.getenv\(["\']([A-Z_]\w*)["\']|os\.environ\.get\(["\']([A-Z_]\w*)["\']|os\.environ\[["\']([A-Z_]\w*)["\']]', content):
                    for g in m.groups():
                        if g:
                            vars_found.add(g)
    return vars_found


def generate(vars_found: set[str], output_path: str) -> str:
    vars_found = vars_found - EXCLUDE
    vars_found.add("NO_PROXY")  # hardcoded write only, not read via os.getenv
    classified: dict[str, set[str]] = {}
    uncategorized = set(vars_found)
    for cat, keys in ENV_CATEGORIES.items():
        matched = set(k for k in keys if k in vars_found)
        if matched:
            classified[cat] = matched
            uncategorized.difference_update(matched)
    if uncategorized:
        classified[u"其他"] = uncategorized

    lines = []
    lines.append("# =============================================================================")
    lines.append("# NanoGhost 环境变量配置（自动生成 — 从代码 os.getenv() 扫描）")
    lines.append("# =============================================================================")
    lines.append("# 优先级：实例 .env (override=True) > 全局 ~/.nanoghost/.env > 系统环境变量")
    lines.append("# =============================================================================")

    for cat, keys in classified.items():
        comment = CATEGORY_COMMENTS.get(cat, cat)
        lines.append("")
        lines.append(f"# =============================================================================")
        for cl in str(comment).split("\n"):
            lines.append(f"# {cl}".rstrip())
        lines.append(f"# =============================================================================")
        for k in sorted(keys):
            default = DEFAULTS.get(k, "")
            lines.append(f"{k}={default}")

    lines.append("")
    content = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
    return content


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    project = os.path.dirname(root)  # repo root
    src_dirs = [os.path.join(project, "src")]
    run_py = os.path.join(project, "run.py")
    output = os.path.join(project, ".env.example")

    vars_found = scan_env_vars(src_dirs)
    if os.path.isfile(run_py):
        try:
            content = open(run_py, encoding="utf-8", errors="ignore").read()
            for m in re.finditer(r'os\.getenv\(["\']([A-Z_]\w*)["\']|os\.environ\.get\(["\']([A-Z_]\w*)["\']|os\.environ\[["\']([A-Z_]\w*)["\']]', content):
                for g in m.groups():
                    if g:
                        vars_found.add(g)
        except Exception:
            pass

    generate(vars_found, output)
    print(f"Generated {output} with {len(vars_found)} env vars:")
    for v in sorted(vars_found):
        print(f"  {v}")


if __name__ == "__main__":
    main()
