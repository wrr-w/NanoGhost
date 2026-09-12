import getpass
import json
import os
import time


def _global_config_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".nanoghost")


def _ensure_global_dir() -> None:
    d = _global_config_dir()
    os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(d, "instances"), exist_ok=True)


def _env_path(instance_dir: str | None = None) -> str:
    if instance_dir:
        return os.path.join(os.path.abspath(os.path.expanduser(instance_dir)), ".env")
    inst = (os.getenv("INSTANCE_DIR") or "").strip()
    if inst:
        return os.path.join(inst, ".env")
    return os.path.join(_global_config_dir(), ".env")


def _write_env(key: str, value: str, instance_dir: str | None = None) -> None:
    path = _env_path(instance_dir)
    lines: list[str] = []
    found = False
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(key + "=") or stripped.startswith("# " + key + "="):
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f"{key}={value}\n")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _read_env(key: str, instance_dir: str | None = None) -> str:
    path = _env_path(instance_dir)
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith(key + "="):
                return stripped[len(key) + 1:].strip().strip('"').strip("'")
    return ""


def _prompt(prompt: str, default: str = "", secret: bool = False) -> str:
    try:
        if secret:
            value = getpass.getpass(f"  {prompt}: ").strip()
        else:
            default_display = f" [{default}]" if default else ""
            value = input(f"  {prompt}{default_display}: ").strip()
        return value or default
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _test_llm_connection(api_key: str, base_url: str, model: str) -> tuple[bool, str]:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=15)
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=5,
        )
        return True, ""
    except Exception as e:
        return False, str(e)


def _create_instance_layout(inst: str) -> str:
    instance_dir = os.path.abspath(os.path.expanduser(inst))
    for sub in ("runtime", "data", "work", "prompts", "skills"):
        os.makedirs(os.path.join(instance_dir, sub), exist_ok=True)
    env_path = os.path.join(instance_dir, ".env")
    if not os.path.exists(env_path):
        _copy_env_template(env_path)
    return instance_dir


def _copy_env_template(dst: str) -> None:
    import sys as _sys
    if getattr(_sys, "frozen", False):
        src = os.path.join(_sys._MEIPASS, ".env.example")
    else:
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env.example")
    if os.path.isfile(src):
        with open(src, encoding="utf-8") as f:
            content = f.read()
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(content)


def _write_channel_config(instance_dir: str, *, feishu_enabled: bool) -> str:
    """把通道开关落到实例的 channel_directory.json。

    这里（而不是 .env）才是"这个实例是飞书机器人还是终端"的唯一开关 —— 网关
    _auto_start_workers 读的就是它，控制台的通道面板写的也是它。原因见
    agent_core/config.py 里 resolve_agent_mode 的注释。

    形状和 gateway_server.save_channel_config 保持一致（updated_at + channels）。
    先写临时文件再 replace：直接覆盖的话，写一半崩掉会留下半截 JSON，而读的一方
    （_read_json）遇到坏文件是**当作没有配置**处理的 —— 症状就是通道悄悄关了。
    """
    path = os.path.join(instance_dir, "channel_directory.json")
    cfg: dict = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cfg = loaded
        except Exception:
            cfg = {}  # 坏文件按"没有"处理，下面整份重写
    channels = cfg.get("channels")
    if not isinstance(channels, dict):
        channels = {}
    feishu = channels.get("feishu")
    if not isinstance(feishu, dict):
        feishu = {}
    feishu["enabled"] = bool(feishu_enabled)
    channels["feishu"] = feishu
    cfg["channels"] = channels
    cfg["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    os.makedirs(instance_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def run_setup_wizard() -> bool:
    _ensure_global_dir()

    global_env = _env_path()
    has_global_key = bool((os.getenv("LLM_API_KEY") or "").strip())

    instances_root = os.path.join(_global_config_dir(), "instances")

    # ── Phase 1: 全局 LLM 配置 ──
    if not has_global_key:
        print()
        print("=" * 60)
        print("  NanoGhost - 首次运行配置向导")
        print("=" * 60)
        print()
        print(f"  全局配置目录: {_global_config_dir()}")
        print()

        print("── 步骤 1/2: LLM 大模型配置 ──")
        print()
        print("  请选择 LLM 提供商：")
        print("    [1] DeepSeek    (https://api.deepseek.com)")
        print("    [2] OpenAI      (https://api.openai.com)")
        print("    [3] 自定义")
        print()

        choice = _prompt("请输入编号", "1")
        if choice == "2":
            base_url = "https://api.openai.com/v1"
            default_model = "gpt-4o"
        elif choice == "3":
            base_url = _prompt("API 地址 (Base URL)", "")
            default_model = ""
        else:
            base_url = "https://api.deepseek.com"
            default_model = "deepseek-v4-flash"

        api_key = _prompt("API Key", "", secret=True)
        model = _prompt("模型名称", default_model)

        if not api_key or not base_url or not model:
            print()
            print("  [!] API Key / 地址 / 模型名称均为必填，配置已取消。")
            print()
            return False

        print()
        print("  正在测试 LLM 连接...")
        ok, err = _test_llm_connection(api_key, base_url, model)
        if ok:
            print(f"  [OK] 连接成功！")
        else:
            print(f"  [WARN] 连接测试失败: {err}")

        print()
        print("  正在保存全局配置...")
        _write_env("LLM_API_KEY", api_key)
        _write_env("LLM_BASE_URL", base_url)
        _write_env("LLM_MODEL", model)
        _write_env("LLM_SUPPORTS_VISION", "false")
        _write_env("NO_PROXY", "*")

        os.environ["LLM_API_KEY"] = api_key
        os.environ["LLM_BASE_URL"] = base_url
        os.environ["LLM_MODEL"] = model
        print(f"  [OK] 配置已保存到 {global_env}")
        print()

    # ── Phase 2: 创建实例（必填）──
    print("── 步骤 {}/2: 创建 Agent 实例 ──".format("2" if not has_global_key else "1"))
    print()
    print("  NanoGhost 必须运行在一个实例中。每个实例是一个独立的 Agent。")
    print("  可以在实例下配置独立的 API Key、飞书机器人等。")
    print()

    while True:
        inst_name = _prompt("输入实例名称", "").strip()
        if not inst_name:
            print("  [!] 必须至少创建一个实例才能使用。")
            print()
            continue

        inst = os.path.join(instances_root, inst_name)
        if os.path.exists(inst):
            print(f"  [!] 实例 {inst_name} 已存在，换个名字。")
            print()
            continue

        instance_dir = _create_instance_layout(inst)
        print()

        # 实例级 LLM 配置（可选，不填则继承全局）
        print("  实例可覆盖全局 LLM 配置（回车跳过则继承全局）：")
        inst_api_key = _prompt("  LLM_API_KEY（覆盖）", "", secret=True)
        inst_base_url = _prompt("  LLM_BASE_URL（覆盖）", "")
        inst_model = _prompt("  LLM_MODEL（覆盖）", "")

        # 飞书配置
        print()
        print("  飞书通道配置（可选，回车跳过使用 CLI 模式）：")
        feishu_app_id = _prompt("  FEISHU_APP_ID", "", secret=True)
        feishu_app_secret = ""
        feishu_bot_name = ""
        if feishu_app_id:
            feishu_app_secret = _prompt("  FEISHU_APP_SECRET", "", secret=True)
            feishu_bot_name = _prompt("  FEISHU_BOT_NAME", inst_name)
        # 填了 app_id 就是"这个实例是飞书机器人" —— 开关写进 channel_directory.json，
        # **不写 .env**。写 .env 是旧做法：网关孵 worker 时传的 AGENT_MODE 会被
        # .env 顶掉，于是 worker 起来跑 CLI、秒退、再被体检孵一遍，无限循环。
        channel_path = _write_channel_config(instance_dir, feishu_enabled=bool(feishu_app_id))

        # 写入实例 .env（所有键都写，留空继承全局）
        _write_env("LLM_API_KEY", inst_api_key, instance_dir=instance_dir)
        _write_env("LLM_BASE_URL", inst_base_url, instance_dir=instance_dir)
        _write_env("LLM_MODEL", inst_model, instance_dir=instance_dir)
        _write_env("LLM_SUPPORTS_VISION", "", instance_dir=instance_dir)
        _write_env("EMBED_MODEL", "", instance_dir=instance_dir)
        _write_env("EMBED_BASE_URL", "", instance_dir=instance_dir)
        _write_env("EMBED_API_KEY", "", instance_dir=instance_dir)
        _write_env("EMBED_LOCAL_MODEL", "", instance_dir=instance_dir)
        _write_env("FEISHU_APP_ID", feishu_app_id, instance_dir=instance_dir)
        _write_env("FEISHU_APP_SECRET", feishu_app_secret, instance_dir=instance_dir)
        _write_env("FEISHU_BOT_NAME", feishu_bot_name, instance_dir=instance_dir)
        _write_env("FEISHU_BOT_OPEN_ID", "", instance_dir=instance_dir)
        _write_env("LARK_CLI_PROFILE", "", instance_dir=instance_dir)
        _write_env("FEISHU_VERBOSE", "", instance_dir=instance_dir)
        _write_env("AGENT_BASE_URL", "", instance_dir=instance_dir)
        _write_env("AGENT_PROMPTS_DIR", "", instance_dir=instance_dir)
        _write_env("NO_PROXY", "*", instance_dir=instance_dir)

        inst_env = os.path.join(instance_dir, ".env")
        print(f"  [OK] 实例 {inst_name} 已创建，配置保存到 {inst_env}")
        print(f"       通道开关: {channel_path}")
        if feishu_app_id:
            print("       飞书通道已启用；要在这个实例上跑交互终端，用 "
                  f"AGENT_MODE=cli nanoghost -I {inst_name} 覆盖一次")
        print()

        if _prompt("是否再创建一个实例？(y/N)", "N").lower() not in ("y", "yes"):
            break

    print()
    print("=" * 60)
    print("  配置完成！")
    print()
    print("  启动方式：")
    print("    nanoghost gateway start -I <实例名>  # 启动守护进程（已启用的通道在里面）")
    print("    nanoghost -I <实例名>       # 直接跑这个实例（开着飞书就是飞书机器人）")
    print("    AGENT_MODE=cli nanoghost -I <实例名>  # 强制一次 CLI 交互终端")
    print("=" * 60)
    print()

    from dotenv import load_dotenv
    if os.path.isfile(global_env):
        load_dotenv(dotenv_path=global_env, override=True)
    return True


def ensure_llm_configured() -> bool:
    api_key = (os.getenv("LLM_API_KEY") or "").strip()
    if api_key:
        return True
    return run_setup_wizard()
