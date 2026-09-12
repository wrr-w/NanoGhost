# 升级机制

> 装完之后怎么用、怎么发一个新版本 —— 看 [`DEPLOYMENT.md`](DEPLOYMENT.md)。
> 这份文档讲的是**机制**：调用契约、退出码、结果文件、以及每个坑的成因。

NanoGhost 有两套独立的更新路径，用途不同，不要混用：

| 场景 | 方式 | 谁来做 |
| --- | --- | --- |
| 新机器第一次装 | Inno Setup 安装包（`dist/installer/*.exe`） | 人工双击 |
| 已装的机器升级 | GitHub Releases 自更新（`nanoghost update`） | 外部管理器 / 人工 |

安装包**不含源码**，也不从服务器拉代码 —— 它就是把 `dist/NanoGhost/` 整个目录
（PyInstaller onedir 产物）打包进去，装完即可用，目标机不需要 Python。

升级同理：下载的是**编译好的新版本压缩包**，只覆盖程序文件，不碰用户数据。

---

## 数据与程序分离

理解升级的前提是先理解这两条路径是分开的：

```
程序    %LOCALAPPDATA%\Programs\NanoGhost\     ← 升级时被整体覆盖
数据    %USERPROFILE%\.nanoghost\              ← 升级永远不碰
        ├── update.json                        ← 更新配置
        └── instances\<名字>\.env              ← 密钥、实例配置
```

升级脚本用 `robocopy /E` 覆盖安装目录，**只会覆盖、不会删除**安装目录里多出来的
文件。用户数据在另一个根目录下，无论如何都不受影响。

> ⚠️ `.env` 里有密钥。`scripts/make_release.py` 会主动拒绝把 `.env` / `*.pem` /
> `*.key` 打进升级包 —— 升级包是公开下载的。如果你改了 `build.spec` 的 `datas`，
> 打包时会直接报错拦下来，不要绕过它。

---

## 配置

`%USERPROFILE%\.nanoghost\update.json`：

```json
{
  "repo": "wrr-w/NanoGhost"
}
```

安装包会**自动装一份**这个文件（`scripts/update.default.json`）。这很重要：
客户不可能知道这个文件的存在，光靠"让客户自己配"等于装完就升不了级。

即使这个文件完全不存在（被删了、装在非标准位置、手工解压的绿色版），程序内部
也有 `DEFAULT_REPO` 兜底，所以**开箱即可升级**是第一原则。配置只在需要换源时
才有存在感。

### 全部可配项

| 键 | 默认 | 用途 |
| --- | --- | --- |
| `repo` | `wrr-w/NanoGhost` | 查哪个仓库的 latest release |
| `download_base` | 空（用 GitHub 原地址） | 换下载源：自建镜像或加速代理 |
| `asset_prefix` | `nanoghost` | 从多个 `.zip` 里挑升级包的前缀 |

`download_base` 解决的是**客户网络连不上 github.com** 的情况（实测本机就是
单点阻断：`github.com` 不通，其它 GitHub 域名正常）。它是"到
`releases/download` 为止"的前缀：

```json
{
  "repo": "wrr-w/NanoGhost",
  "download_base": "https://dl.example.com/nanoghost"
}
```
→ `https://dl.example.com/nanoghost/v1.0.0/NanoGhost-v1.0.0-win64.zip`

```json
{
  "download_base": "https://ghproxy.net/https://github.com/wrr-w/NanoGhost/releases/download"
}
```

注意 `download_base` **只改写下载地址，不改查版本的地址** —— 版本检查仍然走
`api.github.com`。如果客户连 API 也不通，那得换 `repo` 指向一个可达的镜像仓库，
或者由外部管理器跳过 `--check` 直接调用 `update --yes`。

配置文件读坏了（比如多了个逗号）不会被当成致命错误 —— 退回默认值继续工作，
不因为一个手滑让升级功能整个失效。

---

## 启动时的版本提示

程序启动时会检查一次新版本并在控制台提示。为了不让网络问题拖慢启动：

- **一天最多认真查一次**（时间戳记在 `~/.nanoghost/.last_update_check`）
- 自动检查用更短的超时（6 秒，而不是命令行的 15 秒）
- 任何失败（网络、解析、超时）一律**静默**，绝不阻塞或影响启动

关掉：

```bat
set NANOGHOST_DISABLE_AUTO_UPDATE=true
```

> 关掉的只是启动提示。`nanoghost update` 子命令不受影响，外部管理器走那条路。

---

## 外部管理器调用契约

管理器负责 NanoGhost 的生命周期，升级流程是：

```
1. 停止 NanoGhost              （必须，否则 exe 被占用）
2. nanoghost update --check --json
        ↓ exit 0 → 已是最新，直接启动，结束
        ↓ exit 1 → 检查失败（网络等），启动旧版本，结束
        ↓ exit 10 → 有新版本，继续
3. nanoghost update --yes --no-restart --json
        ↓ exit 10 → 覆盖脚本已后台启动，继续
        ↓ exit 1  → 下载/启动失败，启动旧版本，结束
4. 轮询 result_file 直到出现内容
5. 启动 NanoGhost
```

### 命令与退出码

```
nanoghost update --check --json
    0   已是最新
    10  有新版本
    1   检查失败

nanoghost update --yes --no-restart --json
    0   已是最新，什么都没做
    10  下载完成，覆盖脚本已在后台启动（覆盖尚未发生！）
    1   失败，已下载的包会保留
```

`--check` 只查不下载。`--yes` 关闭进度输出和失败时的 `pause`（管理器调用必须加，
否则可能挂在等按键上）。`--no-restart` 让覆盖脚本不要在事后拉起程序，由管理器
自己启动。`--json` 让输出只剩一行 JSON，便于解析。

### exit 10 不等于升级完成

这是最容易踩的坑。`apply_update()` 只负责**启动**覆盖脚本，真正的覆盖要等
NanoGhost 进程退出、文件锁释放之后才发生。所以 exit 10 的含义是"已开始"，
不是"已完成"。

管理器必须轮询结果文件：

```
result_file  默认 %TEMP%\nanoghost_update_result.txt
内容         "OK"             覆盖成功
             "FAIL:timeout"   程序没能在 60 秒内退出
             "FAIL:expand"    安装包解压失败（下载损坏）
             "FAIL:copy"      写安装目录失败（exe 仍被占用）
```

调用方在发起升级前应该先删掉这个文件，靠"文件出现"判断本次结束 —— 实际上
`apply_update()` 自己会先删一次陈旧的，但管理器自己也删一下更稳。

路径可用环境变量覆盖，**服务账号下 `%TEMP%` 可能和管理员账号不是同一个**，
这种情况必须显式指定，否则管理器在错误的目录里等一个永远不会出现的文件：

```bat
set NANOGHOST_UPDATE_RESULT=C:\ng\update_result.txt
set NANOGHOST_UPDATE_LOG=C:\ng\update.log
```

`NANOGHOST_UPDATE_LOG` 是覆盖脚本的日志（非交互模式下才有用，交互模式直接打屏）。
脚本本身**输出全是英文**，这是刻意的 —— 见下面「为什么不写中文日志」。

### 判断本次是否真的装上去了

结果文件写 "OK" 之后，重新启动的进程读到的新版本号应当等于 `--check` 报的
`latest_version`。管理器可以用这条做二次确认：

```
nanoghost --version
```

`--json` 输出示例（`--check` 命中时）：

```json
{"ok": true, "update_available": true, "current_version": "1.0.0",
 "latest_version": "1.1.0", "download_url": "https://github.com/.../x.zip"}
```

---

## 下载会重试，且校验内容

升级包 70MB+，在真实网络下一次拉成功的概率并不高。所以下载失败会**自动重试
3 次**（每次之间删掉半截文件重来），全失败才返回 `exit 1`，并给出可读原因。

拿到文件后还会验两道，两道都是实测撞出来的：

1. **文件长度**是否等于 `Content-Length` —— 抓传输中断。
2. **头两个字节是不是 `PK`** —— 抓"拿到一个 200 和 4.7KB 的 HTML 错误页"。

第 2 条特别值得说：release 刚发布完的几秒内服务端还没传播完，请求会返回
**200 但内容是错误页**。只看状态码会当成成功，然后在解压阶段炸成 `FAIL:expand`
—— 报错指向"安装包损坏"，把人往完全错误的方向带。有了这道校验，用户看到的是
"刚发布还没传播完，等一两分钟重试"，这才是他该做的事。

> 这两类失败**重试就能好**，所以错误信息就是这么写的。看到它们不要先去查网络配置。

---

## 发布新版本

```bat
REM 1. 改版本号
echo 1.1.0 > VERSION

REM 2. 重新构建（含安装包）
scripts\build_installer.bat rebuild

REM 3. 打升级包并发布
python scripts\make_release.py --publish
```

只打包不发布：`python scripts\make_release.py`，它会打印出可以直接粘贴的
`gh release create` 命令。

`make_release.py` 会做三件事，每件都是踩过的坑：

1. **把 `dist/NanoGhost/` 的内容放在 zip 根目录**，不套一层目录。套了的话覆盖出来
   会变成 `<安装目录>\NanoGhost\NanoGhost.exe`，程序在原来位置消失，看起来像
   "升级把程序升没了"。
2. **校验 `dist/` 里的 `VERSION` 和源码 `VERSION` 一致**。不一致的话升级完客户端
   读到的还是旧版本号，会反复提示有新版本、无限升级。

   ⚠️ 这个文件在 **`dist/NanoGhost/_internal/VERSION`**，不是包根目录 ——
   PyInstaller onedir 把 `datas` 放在 `_internal/` 下。找错路径的话这道校验会
   静默失效（一个永远不运行的安全检查比没有更危险，因为它让人以为查过了），
   所以脚本现在两个路径都试，找不到就**报错退出**而不是跳过。
3. **拒绝 `.env` / `*.pem` / `*.key` 进包**（见上文）。

### 关于 release 里的多个 `.zip`

GitHub 自动生成的 `Source code (zip)` **不在 API 的 `assets` 数组里**（它走
`zipball_url` 字段），所以它不会被客户端误当成升级包 —— 这点实测确认过，不是
推测。

真正的风险是手动往同一个 release 多传一个 `.zip`。客户端的选择顺序是：

1. 优先取文件名以 `asset_prefix` 开头的 `.zip`（默认 `nanoghost`）；
2. 挑不到再退回取第一个 `.zip`。

所以只要升级包保持 `NanoGhost-vX.Y.Z-win64.zip` 这个命名，就不会选错。
如果产品改名了，在 `update.json` 里加 `asset_prefix` 即可：

```json
{ "repo": "wrr-w/NanoGhost", "asset_prefix": "nanoghost" }
```

---

## 本地验证升级流程

不需要真的发 release，把 `update.json` 的 repo 指到一个测试仓库即可：

```bat
nanoghost update --check --json
echo exit=%errorlevel%
```

想验证完整的覆盖流程（含文件锁、解压覆盖、结果文件），参考
`src/agent_core/update.py` 里 `_write_update_bat()` 的 docstring —— 那里记了四个
曾经翻车的点，改这个函数前先读一遍。

---

## 为什么不写中文日志

升级脚本是**运行时生成**的 `.bat`，生成时必须满足两个条件，否则 cmd 会解析错位、
从行中间开始执行命令（表现为日志里冒出 `'d_iscc' 不是内部或外部命令` 这种碎片）：

1. **换行必须是 CRLF**，不能是裸 LF。
2. **文件里不能有多字节字符**（中文）。CRLF 只能挡住一部分情况，实测多字节字符
   仍然会让 cmd 的字节偏移错位。

所以 `update.py` 生成的脚本提示文字一律用英文。这是刻意的，**不要"顺手翻译回
中文"**。同理 `scripts/build_installer.bat` 也是纯 ASCII。

日志给人看还是给管理器看：管理器场景下没人看日志，英文完全够用；真出问题时
`FAIL:<阶段>` 已经指明了阶段，比中文提示更有用。
