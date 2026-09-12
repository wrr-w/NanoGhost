r"""把 dist/NanoGhost/ 打成 GitHub Releases 用的升级包。

用法:
    python scripts/make_release.py            # 只打包
    python scripts/make_release.py --publish  # 打包并调 gh 发布 Release

为什么需要这个脚本（而不是直接右键压缩 dist/NanoGhost）:

1. **zip 里必须是 dist/NanoGhost/ 的内容，不能带 NanoGhost/ 这一层目录。**
   客户端 update.py 把包解压到 staging 后直接 robocopy 覆盖安装目录。如果包里多
   一层 NanoGhost\，覆盖出来会变成 <安装目录>\NanoGhost\NanoGhost.exe —— 程序就
   在原来的位置消失了，看起来像"升级把程序升没了"。

2. **必须确保没有 .env 混进去。** 升级包是要公开下载的。.env 里有密钥，
   PyInstaller 的 COLLECT 不会主动收，但一旦有人改了 build.spec 的 datas，
   这个检查就是最后一道闸。

3. **release 里只能有一个 .zip。** update.py 取 assets 里第一个 .zip，多放一个
   别的 zip（比如源码包）就可能下错东西。
"""
import argparse
import hashlib
import os
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 绝不能进升级包的东西。命中就直接失败，不做静默过滤 —— 静默过滤会让人以为
# 包是干净的，而实际原因是规则漏了。宁可报错让人来看一眼。
FORBIDDEN_NAMES = {".env"}
FORBIDDEN_DIRS = {"instances", ".git", "__pycache__", ".venv", "venv"}
# 密钥库格式没有"公开"的用法，一律拦
FORBIDDEN_SUFFIXES = (".pfx", ".p12", ".keystore", ".jks")
# .pem / .key / .crt 既可能是公开证书也可能是私钥，只能看内容。
# certifi 的 cacert.pem 是公开 CA 证书链，合法且必须打包 —— 第一版实现按后缀
# 一律拦下，把它误杀了。
PRIVATE_KEY_MARKERS = (
    b"PRIVATE KEY",
    b"-----BEGIN RSA ",
    b"-----BEGIN DSA ",
    b"-----BEGIN EC ",
    b"-----BEGIN OPENSSH ",
    b"PuTTY-User-Key-File",
)


def read_version() -> str:
    path = os.path.join(ROOT, "VERSION")
    if not os.path.isfile(path):
        sys.exit("[ERROR] 找不到 VERSION 文件")
    return open(path, encoding="utf-8").read().strip()


def secret_reason(path: str, name: str) -> str | None:
    """如果这个文件不该进升级包，返回原因；否则返回 None。"""
    low = name.lower()
    if low in FORBIDDEN_NAMES:
        return "文件名本身就是凭据文件"
    if low.endswith(FORBIDDEN_SUFFIXES):
        return "密钥库格式"
    if low.endswith((".pem", ".key", ".crt", ".cer")):
        try:
            head = open(path, "rb").read(65536)
        except OSError:
            return None
        if any(m in head for m in PRIVATE_KEY_MARKERS):
            return "内容含私钥"
    return None


def collect_files(src: str) -> list[str]:
    """返回相对于 src 的文件路径列表（正斜杠），并做安全检查。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(src):
        rel_dir = os.path.relpath(dirpath, src)
        if rel_dir == ".":
            rel_dir = ""
        # 就地裁剪，避免 walk 进入这些目录
        dirnames[:] = [d for d in dirnames if d not in FORBIDDEN_DIRS]

        for name in filenames:
            rel = os.path.join(rel_dir, name).replace("\\", "/").lstrip("/")
            top = rel.split("/")[0]

            reason = secret_reason(os.path.join(dirpath, name), name)
            if reason:
                sys.exit(f"[ERROR] 升级包里出现了敏感文件: {rel} ({reason})\n"
                         f"        升级包是公开下载的。检查 build.spec 的 datas 是否误收了运行期数据。")
            if top in FORBIDDEN_DIRS:
                sys.exit(f"[ERROR] 升级包里出现了不该打包的目录: {rel}")
            out.append(rel)
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--publish", action="store_true",
                    help="打包后调用 gh release create 发布")
    ap.add_argument("--notes", default="", help="Release 说明")
    args = ap.parse_args()

    version = read_version()
    src = os.path.join(ROOT, "dist", "NanoGhost")
    exe = os.path.join(src, "NanoGhost.exe")

    if not os.path.isfile(exe):
        sys.exit(f"[ERROR] 找不到 {exe}\n"
                 f"        先跑 scripts\\build_installer.bat rebuild，或 pyinstaller build.spec -y")

    # 版本一致性检查：dist 里的 VERSION 必须和源码的一致，否则升级完
    # 客户端读到的还是旧版本号，会反复提示"有新版本"、无限升级。
    #
    # PyInstaller 6.x 的 onedir 把 datas 放进 _internal/，所以 VERSION 在
    # _internal/VERSION 而不是包根目录。第一版只找了根目录，文件不存在 →
    # 整个检查被 if 静默跳过 —— 一个永远不执行的"安全检查"比没有还危险，
    # 它会让人以为查过了。所以改成找不到就直接报错。
    candidates = [
        os.path.join(src, "_internal", "VERSION"),
        os.path.join(src, "VERSION"),
    ]
    bundled_version_file = next((p for p in candidates if os.path.isfile(p)), None)
    if bundled_version_file is None:
        sys.exit("[ERROR] 在 dist 里找不到 VERSION，试过:\n"
                 + "".join(f"        {p}\n" for p in candidates)
                 + "        没有它就核对不了版本，先确认构建配置没被改过。")
    bundled = open(bundled_version_file, encoding="utf-8").read().strip()
    if bundled != version:
        sys.exit(f"[ERROR] 版本不一致: 源码 VERSION={version}，"
                 f"但包里是 {bundled}\n"
                 f"        （取自 {bundled_version_file}）\n"
                 f"        改了 VERSION 之后必须重新构建再打包，否则客户端装完\n"
                 f"        读到的还是旧版本号，会一直提示有新版本。")

    files = collect_files(src)
    if not files:
        sys.exit("[ERROR] dist/NanoGhost 是空的")

    # 顶层必须有 NanoGhost.exe，否则包结构错了
    if "NanoGhost.exe" not in files:
        sys.exit("[ERROR] NanoGhost.exe 不在包根目录，包结构不对（多套了一层目录？）")

    outdir = os.path.join(ROOT, "dist", "release")
    os.makedirs(outdir, exist_ok=True)
    tag = f"v{version}"
    zip_name = f"NanoGhost-{tag}-win64.zip"
    zip_path = os.path.join(outdir, zip_name)

    if os.path.exists(zip_path):
        os.remove(zip_path)

    # ZIP_DEFLATED: 升级包走网络下载，体积比压缩速度重要
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            z.write(os.path.join(src, rel.replace("/", os.sep)), rel)

    size = os.path.getsize(zip_path)
    sha = hashlib.sha256(open(zip_path, "rb").read()).hexdigest()

    print(f"版本    : {version}")
    print(f"文件数  : {len(files)}")
    print(f"输出    : {zip_path}")
    print(f"大小    : {size:,} bytes ({size / 1024 / 1024:.1f} MB)")
    print(f"SHA256  : {sha}")
    print()
    print("包里顶层（前 10 项）:")
    for rel in files[:10]:
        print(f"  {rel}")
    if len(files) > 10:
        print(f"  ... 共 {len(files)} 项")

    if args.publish:
        notes = args.notes or f"NanoGhost {tag}"
        cmd = ["gh", "release", "create", tag, zip_path, "--title", tag, "--notes", notes]
        print()
        print("发布:", " ".join(cmd))
        rc = subprocess.call(cmd, cwd=ROOT)
        if rc != 0:
            return rc
        print(f"[OK] 已发布 {tag}")
    else:
        print()
        print("下一步 —— 发布 Release（客户端优先挑名字以 'nanoghost' 开头的 .zip，")
        print("挑不到才退回第一个 .zip；别往同一个 release 里再传别的 zip）:")
        print()
        print(f'  gh release create {tag} "{zip_path}" --title "{tag}" --notes "NanoGhost {tag}"')

    return 0


if __name__ == "__main__":
    sys.exit(main())
