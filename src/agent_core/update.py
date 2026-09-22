import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).parent)
    return os.getcwd()


# 默认仓库。必须有 —— 客户不可能知道 update.json 这个文件的存在，只靠配置文件
# 的话装机后开箱就是"未配置 repo，升级不可用"。update.json 里显式配的 repo
# 优先，便于换源或自建分发。
DEFAULT_REPO = "wrr-w/NanoGhost"


def _config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".nanoghost", "update.json")


def _read_config() -> dict:
    path = _config_path()
    if not os.path.isfile(path):
        return {}
    try:
        data = json.loads(open(path, encoding="utf-8").read())
        return data if isinstance(data, dict) else {}
    except Exception:
        # 配置坏了就退回默认值（默认 repo 能正常工作），不因为一个手滑的逗号
        # 让整个升级功能失效
        return {}


def _apply_download_base(url: str, cfg: dict) -> str:
    """按 download_base 改写下载地址前缀。

    客户机器直连 github.com 可能不通（实测本机就是单点阻断），需要把下载源
    指到自建服务器或加速代理。配置的是"到 releases/download 为止"的前缀：

        自建   "https://dl.example.com/nanoghost"
               → https://dl.example.com/nanoghost/v1.0.0/NanoGhost-...zip
        代理   "https://ghproxy.net/https://github.com/wrr-w/NanoGhost/releases/download"

    留空就用 GitHub 原始地址。
    """
    base = (cfg.get("download_base") or "").strip().rstrip("/")
    if not base:
        return url
    marker = "/releases/download/"
    i = url.find(marker)
    if i < 0:
        return url          # 不是标准 release 地址，不认识就别乱改
    return f"{base}/{url[i + len(marker):]}"


def _check_github(repo: str, asset_prefix: str = "nanoghost",
                  timeout: int = 15) -> tuple[bool, str, str]:
    import requests
    try:
        r = requests.get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        tag = str(data.get("tag_name", "")).strip().lstrip("v")
        if not tag:
            return False, "", "no tag_name found"

        # GitHub 自动生成的 "Source code (zip)" 不在 assets 里（它走 zipball_url），
        # 所以正常情况下 assets 里只有一个 .zip。但手滑多传一个 zip 就会命中错误
        # 的文件，所以先按文件名前缀挑，挑不到再退回"第一个 .zip"。
        assets = [
            (str(a.get("name", "")).lower(), str(a.get("browser_download_url", "")))
            for a in (data.get("assets") or [])
        ]
        prefix = (asset_prefix or "").strip().lower()
        # 自 v1.2 起走「安装器即更新器」：取官方 Inno 安装包 .exe（不再用 zip）。
        # 仍先按前缀挑（NanoGhostSetup-*.exe），挑不到再退回第一个 .exe。
        if prefix:
            for name, url in assets:
                if url and name.endswith(".exe") and name.startswith(prefix):
                    return True, tag, url
        for name, url in assets:
            if url and name.endswith(".exe"):
                return True, tag, url
        return False, "", "no .exe asset found in release"
    except Exception as e:
        return False, "", str(e)


def check_for_updates(timeout: int = 15) -> tuple[bool, str, str]:
    from agent_core.version import current_version, compare_versions

    cfg = _read_config()
    repo = (cfg.get("repo") or DEFAULT_REPO).strip()

    local = current_version()
    ok, remote_version, url = _check_github(
        repo, cfg.get("asset_prefix") or "nanoghost", timeout=timeout)
    if not ok:
        return False, "", f"检查更新失败: {url}"
    url = _apply_download_base(url, cfg)
    if compare_versions(remote_version, local) > 0:
        return True, remote_version, url
    return False, remote_version, url


def update_result_path() -> str:
    """覆盖结果文件路径。外部管理器轮询这个文件判断升级是否成功。

    内容: "OK" 表示成功；"FAIL:<阶段>" 表示失败（timeout/expand/copy）。
    可用环境变量 NANOGHOST_UPDATE_RESULT 覆盖（服务账号下 %TEMP% 可能不同）。
    """
    override = (os.getenv("NANOGHOST_UPDATE_RESULT") or "").strip()
    if override:
        return override
    return os.path.join(tempfile.gettempdir(), "nanoghost_update_result.txt")


def update_log_path() -> str:
    """非交互模式下升级脚本的日志路径（交互模式下输出直接打在控制台）。"""
    override = (os.getenv("NANOGHOST_UPDATE_LOG") or "").strip()
    if override:
        return override
    return os.path.join(tempfile.gettempdir(), "nanoghost_update.log")


def _write_update_bat(
    zip_path: str,
    target_dir: str,
    exe_name: str,
    interactive: bool = True,
    restart: bool = True,
) -> str:
    """生成并写出升级脚本，返回其路径。

    四个关键点（原实现在这里连续翻车）：

    1. 不能用 [System.IO.Compression.ZipFile]::ExtractToDirectory —— .NET Framework
       版本遇到目标已存在的文件会直接抛异常且**不覆盖**。覆盖安装时目标目录里
       几乎所有文件都已存在，必然第一条就失败。改用 Expand-Archive -Force。
    2. 解压/覆盖失败时必须中止，不能删掉安装包、更不能重启旧版本 —— 否则调用方
       看到"升级完成"，跑的却还是旧代码。
    3. 等待必须用 ping 而不是 timeout —— timeout 依赖控制台输入，在
       CREATE_NO_WINDOW 下会立刻报错返回，导致 60 次循环瞬间跑完、误判超时。
    4. 文件必须写成 CRLF，且**只包含 ASCII 字符**。cmd 按字节偏移跟踪批处理位置，
       文件里的多字节字符会让偏移错位，cmd 从行中间开始执行，输出 'd_iscc' 这类
       碎片报错。CRLF 只能挡住一部分情况，彻底的做法是不放多字节字符 —— 这个脚本
       跑在无人值守路径上，宁可日志是英文也不能靠运气。
       所以下面的提示文字一律用英文，这是刻意的，不要"顺手翻译回中文"。

    流程：等进程退出 → 解压到 staging → robocopy 覆盖到安装目录 → 删包 → 写结果 → 重启。

    interactive=True:  附加控制台窗口，失败时 pause，输出打屏
    interactive=False: 无窗口，输出重定向到 update_log_path()，不 pause
    restart=False:     覆盖完不拉起程序，交给外部管理器
    """
    bat = os.path.join(tempfile.gettempdir(), "nanoghost_update.bat")
    result = update_result_path()
    log = update_log_path()

    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "title NanoGhost Updater",
        "",
        f'set "ZIP={zip_path}"',
        f'set "TARGET={target_dir}"',
        f'set "EXE={exe_name}"',
        'set "STAGE=%TEMP%\\nanoghost_stage"',
        f'set "RESULT={result}"',
        "",
    ]
    if interactive:
        lines += ["call :main", "exit /b %errorlevel%"]
    else:
        lines += [f'call :main >> "{log}" 2>&1', "exit /b %errorlevel%"]
    lines += [
        "",
        ":main",
        'if exist "%RESULT%" del /f /q "%RESULT%"',
        "",
        "echo Waiting for NanoGhost to exit...",
        "set /a _n=0",
        ":waitloop",
        'tasklist /FI "IMAGENAME eq %EXE%" 2>nul | find /I "%EXE%" >nul',
        "if errorlevel 1 goto :extract",
        "set /a _n+=1",
        "if %_n% GEQ 60 goto :fail_timeout",
        "ping -n 2 127.0.0.1 >nul 2>&1",
        "goto :waitloop",
        "",
        ":extract",
        'if exist "%STAGE%" rmdir /s /q "%STAGE%"',
        "echo Extracting package...",
        'powershell -NoProfile -Command "Expand-Archive -LiteralPath $env:ZIP -DestinationPath $env:STAGE -Force"',
        "if errorlevel 1 goto :fail_expand",
        "",
        "echo Copying into install dir...",
        'robocopy "%STAGE%" "%TARGET%" /E /NFL /NDL /NJH /NJS /NP',
        "if errorlevel 8 goto :fail_copy",
        'rmdir /s /q "%STAGE%"',
        'del /f /q "%ZIP%"',
        'echo OK> "%RESULT%"',
        "echo Update complete.",
    ]
    if restart:
        lines += [
            f'start "" "{os.path.join(target_dir, exe_name)}"',
            "echo Restarted.",
        ]
    else:
        lines += ["echo Restart skipped (external manager will start it)."]
    lines += [
        "exit /b 0",
        "",
        ":fail_timeout",
        "echo [ERROR] NanoGhost is still running (wait timed out), update aborted.",
        'echo FAIL:timeout> "%RESULT%"',
        "goto :fail",
        "",
        ":fail_expand",
        "echo [ERROR] Failed to extract the package. The download may be corrupt.",
        'echo FAIL:expand> "%RESULT%"',
        "goto :fail",
        "",
        ":fail_copy",
        "echo [ERROR] Failed to write into the install dir. The exe may still be locked.",
        'echo FAIL:copy> "%RESULT%"',
        "goto :fail",
        "",
        ":fail",
        "echo.",
        "echo Nothing was changed. The package is kept at: %ZIP%",
        "echo Extract it manually over: %TARGET%",
    ]
    if interactive:
        lines += ["pause"]
    lines += ["exit /b 1", ""]

    # newline="" 关掉文本模式的换行翻译，否则显式的 \r\n 会被再翻译一次变成 \r\r\n
    with open(bat, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")
    return bat


def _validate_package(path: str, expected: int) -> str:
    """确认下到的确实是个完整的 zip。没问题返回 ""，否则返回给用户看的原因。

    为什么非查不可（两个都是实测撞到的）:

    1. release 刚发布完的几秒内，服务端还没传播完，请求会返回 **200 但内容是
       错误页**而不是包。只看状态码会当成成功，然后写进临时包，最后在解压阶段
       炸成 "FAIL:expand" —— 报错指向"包损坏"，让人往完全错误的方向排查。
    2. 这条线路（国内直连 GitHub）传输中断很常见，会留下半个文件。

    两者都是"重试就能好"的情况，所以错误信息直接这么写，别让人去查网络配置。
    """
    if expected and os.path.getsize(path) != expected:
        return (f"下载不完整: 收到 {os.path.getsize(path):,} / 应为 {expected:,} 字节。"
                f"传输中断，重试即可。")
    with open(path, "rb") as f:
        head = f.read(4)
    if head[:2] != b"PK":
        return ("下载到的不是压缩包（可能刚发布还没传播完，或被网络设备拦截）。"
                "等一两分钟重试。")
    return ""


def _download(url: str, dest: str, interactive: bool) -> tuple[bool, str]:
    """下载并校验，失败自动重试。

    升级包有 70MB+，在真实网络下一次拉成功的概率不高。没有重试的话，用户看到的
    是"升级失败"然后得自己再点一次 —— 而这类失败绝大多数重试就好了。
    """
    import requests

    last = "未知错误"
    for attempt in range(1, 4):
        try:
            if interactive and attempt > 1:
                print(f"  第 {attempt} 次重试...")
            with requests.get(url, stream=True, timeout=(15, 120)) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                got = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)
                        got += len(chunk)
                        if interactive and total > 0:
                            print(f"\r  进度: {got * 100 // total}% ({got}/{total})",
                                  end="", flush=True)
                if interactive:
                    print()
            err = _validate_package(dest, total)
            if not err:
                return True, ""
            last = err
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            if interactive:
                print()
        if interactive:
            print(f"  下载失败: {last}")
        # 半截文件留着只会让下次的校验更迷惑，直接清掉重来
        try:
            if os.path.isfile(dest):
                os.remove(dest)
        except OSError:
            pass

    return False, f"下载失败（已重试 3 次）: {last}"


def apply_update(
    download_url: str,
    interactive: bool = True,
    restart: bool = True,
) -> tuple[bool, str]:
    """下载**官方 Inno 安装包**并以脱离进程组的方式静默启动它。

    与旧实现（下 zip → 写 .bat → 等进程退出 → robocopy 覆盖）的区别：

      * 不再写/跑任何 .bat，不依赖外部脚本；
      * 关旧进程、覆盖文件、装完把自己拉起来，全部由安装包自己做
        （installer.iss 的 `CloseApplications` + `[Run]`）；
      * Windows 上正在运行的 exe 不能被覆盖，所以替换只能发生在**本进程退出之后** ——
        安装包是 DETACHED 起的，本进程随后退出不会把它带走。

    返回 (True, "") 只代表**安装包已启动**，不代表已经装好；真正的替换在
    本进程退出之后才发生。调用方应当随即退出。

    restart=True 时由安装包的 [Run] 负责把 NanoGhost 拉回来（= 自动重启）。
    """
    if not download_url:
        return False, "未提供下载地址"

    try:
        tmp_exe = os.path.join(
            tempfile.gettempdir(),
            os.path.basename(download_url.split("?")[0]) or "NanoGhostSetup.exe")
        log_path = os.path.join(tempfile.gettempdir(), "nanoghost_install.log")

        if interactive:
            print(f"  下载安装包: {download_url}")
        ok, err = _download(download_url, tmp_exe, interactive)
        if not ok:
            return False, err

        # 基本体检：别把半截包 / 错误页当安装包喂给系统
        try:
            if os.path.getsize(tmp_exe) < 1024 * 1024:
                return False, "安装包异常：体积过小（可能是错误页或半截包）"
            with open(tmp_exe, "rb") as f:
                if f.read(2) != b"MZ":
                    return False, "安装包异常：不是 Windows 可执行文件"
        except OSError as e:
            return False, str(e)

        argv = [tmp_exe, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART",
                "/NOCLOSEAPPLICATIONS",       # 关不关自己由我们控制：本进程马上退出
                f"/LOG={log_path}"]
        # DETACHED_PROCESS + 新进程组：本进程随后退出，安装包必须活下来干完活
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=None if interactive else subprocess.DEVNULL,
            stderr=None if interactive else subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                           | subprocess.DETACHED_PROCESS),
        )
        return True, ""
    except Exception as e:
        return False, str(e)


def _last_check_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".nanoghost", ".last_update_check")


# 启动路径上自动检查的最小间隔。这个函数是**同步**调用的（run.py / cli.py 在进
# REPL 之前调用它），所以它的耗时直接加在用户等待启动的时间上。网络不通时一次
# 检查要耗到超时，天天卡着就不是能忍受的了，因此限制成一天最多认真查一次。
AUTO_CHECK_INTERVAL = 24 * 3600
# 自动检查单独用更短的超时：GitHub API 正常时 1~2 秒内就返回，等满 15 秒
# 只可能是网络不通，而那种情况下这个结果也没人会看。
AUTO_CHECK_TIMEOUT = 6


def _should_auto_check() -> bool:
    """判断今天是否已经查过。读不到/写不了时间戳都当作"该查"，不影响功能。"""
    try:
        last = float(open(_last_check_path(), encoding="utf-8").read().strip())
    except Exception:
        return True
    import time
    return (time.time() - last) >= AUTO_CHECK_INTERVAL


def _mark_checked() -> None:
    import time
    try:
        path = _last_check_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def auto_check_and_notify() -> None:
    """启动时提示新版本。失败一律静默 —— 这条路不该影响用户开程序。

    这里**不判断** update.json 是否存在。默认 repo 已经兜底，再加一道"必须先配
    置"的闸门就等于客户装完永远收不到提示。想关掉用 NANOGHOST_DISABLE_AUTO_UPDATE。
    """
    disabled = (os.getenv("NANOGHOST_DISABLE_AUTO_UPDATE") or "").strip().lower() in ("1", "true", "yes")
    if disabled:
        return
    if not _should_auto_check():
        return
    # 先记时间戳再查：查的过程中被 Ctrl+C 打断也不该让下次启动又卡一遍
    _mark_checked()
    try:
        has_update, version, _url = check_for_updates(timeout=AUTO_CHECK_TIMEOUT)
    except Exception:
        return
    if has_update:
        from agent_core.version import current_version
        local = current_version()
        print()
        print(f"  [INFO] 新版本可用: v{version} (当前: v{local})")
        print(f"  运行 'nanoghost update' 进行升级。")
        print()
