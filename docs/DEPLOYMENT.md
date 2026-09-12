# 部署与发布

这份文档回答两个问题：

- **用户拿到手之后怎么用** —— 装、配、跑、升级
- **开发者改完代码怎么发出去** —— 打版本、构建、发 release

升级机制本身的细节（退出码、结果文件、外部管理器调用契约）在
[`UPDATING.md`](UPDATING.md)，这里只讲"怎么用"。

---

## 一、用户视角

### 装

拿到 `NanoGhostSetup-<版本>.exe`，双击。

- 默认装到 `%LOCALAPPDATA%\Programs\NanoGhost`，**按用户安装、不弹 UAC**
  （安装向导里可以切成全机器安装，那时走 `%ProgramFiles%`）
- **目标机不需要 Python** —— 安装包就是 PyInstaller onedir 产物，装完即可用
- 安装包**不含源码**
- **安装程序会把安装目录写进 PATH**（按用户装 → `HKCU\Environment`；全机器装 →
  `HKLM\...\Session Manager\Environment`），并广播 `WM_SETTINGCHANGE`，所以装完
  不用重登录就能在任意目录敲 `NanoGhost.exe`。卸载时只删自己加的那一段 —— 你原本
  就把这个目录放在 PATH 里的话，它不动。不去卸载，只想手工删：把那条路径从 PATH 里
  去掉即可，没有别的地方需要清理。

### 首次运行

第一次运行会自动进入配置向导，要求填 LLM 的 API Key / Base URL / 模型名。

配好的东西落在：

```
%USERPROFILE%\.nanoghost\instances\<实例名>\.env
```

向导会检查"能不能连上模型"再放行，不是填完就算数。想手动重跑：

```bat
NanoGhost.exe setup
```

### 日常使用

```bat
NanoGhost.exe -I <实例名>                    :: CLI 交互对话
NanoGhost.exe gateway start -I <实例名>       :: 飞书 bot 常驻（后台守护）
NanoGhost.exe gateway status                 :: 看守护进程状态
NanoGhost.exe instance list                  :: 列出所有实例
NanoGhost.exe config list -I <实例名>         :: 看当前实例的配置
NanoGhost.exe --version                      :: 打印版本号（不联网）
```

多实例是**完全隔离**的：不同实例的记忆、会话、密钥互不可见，可以同时跑。

### 升级

三种方式，选一种：

| 方式 | 怎么做 | 适合 |
| --- | --- | --- |
| 自动提示 | 什么都不做，启动时会提示 | 手动跑的用户 |
| 手动 | `NanoGhost.exe update` | 想立刻升 |
| 由 OpenObstrator 管理 | 管理器调命令行 | 无人值守部署 |

启动时的检查**一天最多一次**，用 6 秒超时，失败静默 —— 不会因为网络问题
拖慢启动。不想要这个提示：

```bat
set NANOGHOST_DISABLE_AUTO_UPDATE=true
```

### 数据在哪，怎么备份

```
%USERPROFILE%\.nanoghost\
├── update.json                          更新配置
└── instances\<实例名>\
    ├── .env                             密钥、通道配置
    ├── memory...                        记忆
    └── ...                              会话库
```

**升级不碰这个目录，卸载也不会删。** 备份就是整个拷走；换机器就是拷过去。

---

## 二、开发者视角

### 版本号只有一个来源

仓库根目录的 `VERSION` 文件。安装包版本、程序内 `--version` 读的都是它。

改版本号**只需要改这一个文件**：

```bat
echo 1.1.0 > VERSION
```

> 升级判断是拿 GitHub 上最新 release 的 tag 和这个版本号比。**tag 必须和
> `VERSION` 一致**，否则客户端会误判 —— 比如 tag 是 `v1.1.0` 但包里 `VERSION`
> 还是 `1.0.0`，那升完还是提示有新版本，**无限升级**。
> `make_release.py` 会拦这种情况。

### 发布三步

```bat
REM 1. 改版本号
echo 1.1.0 > VERSION

REM 2. 构建（PyInstaller + Inno Setup 安装包一起做）
scripts\build_installer.bat rebuild

REM 3. 打升级包并发布到 GitHub Releases
venv\Scripts\python.exe scripts\make_release.py --publish
```

只想打包不想发：

```bat
venv\Scripts\python.exe scripts\make_release.py
```

它会打出可以直接粘贴的 `gh release create` 命令，你先看看包里有什么再决定。

`make_release.py` 不是简单压缩，它做四件事，每件都是踩过的坑：

1. **内容放在 zip 根目录**（不套一层 `NanoGhost/`）—— 套了的话升级完程序会
   变成 `<安装目录>\NanoGhost\NanoGhost.exe`，看起来像"升级把程序升没了"
2. **校验包里的 `VERSION` 和源码一致** —— 防上面说的无限升级
3. **拒绝 `.env` / `*.pem` / `*.key` 进包** —— 升级包是公开下载的，`.env` 里有密钥
4. **确认 `NanoGhost.exe` 在 zip 根目录** —— 防打包结构错

### 发布前检查清单

- [ ] `VERSION` 改过了，且和要打的 tag 一致
- [ ] `scripts\build_installer.bat rebuild` 跑完了（产物在 `dist\installer\`）
- [ ] `make_release.py` 没报错（报了就先修，别绕过）
- [ ] release 里的 zip 名字是 `NanoGhost-v<版本>-win64.zip`
      —— 客户端按 `nanoghost` 前缀挑，别改成别的名字
- [ ] release 里**没有**多余的手传 `.zip`（选包逻辑会先挑前缀匹配的，但别考验它）

### 发布后验证

在**旧版本**的机器上：

```bat
NanoGhost.exe update --check
echo exit=%errorlevel%
```

返回 **10** 才说明真的认出了新版本。

> ⚠️ 刚发布完的几秒内，下载可能拿到 200 但内容是错误页（服务端还没传播完）。
> 程序会识别并提示"等一两分钟重试" —— 这不是 bug，别去查网络。

### 发错了怎么回退

**不要在同一 tag 上换包。** 已经拉过的客户端和服务端可能还缓存着旧内容，
换包会得到"有的人升上去了有的人没有"这种最难查的状态。

正确做法：把有问题的 release 标记为 draft 或删掉，然后**发一个更高的 patch
版本**（`1.1.1`）。

已经升上去的客户端不会自动回退 —— 回退要单独出一个"低版本号大内容"的版本
才行，一般不划算，想清楚再说。

---

## 三、三个入口的分工

容易混，所以明确一次：

| 入口 | 干什么 | 谁触发 |
| --- | --- | --- |
| `dist/installer/NanoGhostSetup-*.exe` | **第一次装** | 人工双击 |
| GitHub release 里的 zip | **已装的机器升级** | 程序自己 / 管理器 |
| OpenObstrator | 编排放**什么时候**停、升、起 | 外部管理器 |

安装包和升级包的内容是**同一个** `dist/NanoGhost/` 目录，只是包装不同：
前者是 Inno Setup 的 exe，后者是 zip。所以升级完的效果和重装一遍是一样的。

---

## 四、排障

| 现象 | 原因 | 怎么办 |
| --- | --- | --- |
| 升完还是旧版本号 | `VERSION` 没打进包，或 `robocopy` 没跑 | 看更新日志；确认 `make_release.py` 的版本校验没被绕过 |
| "检查更新失败" | 连不上 `api.github.com` | 见 `UPDATING.md` 的 `download_base` / 换 `repo` |
| 结果文件是 `FAIL:copy` | exe 还被占用 | 管理器**先停进程**再升级，装完再拉起 |
| 结果文件是 `FAIL:timeout` | 进程 60 秒内没退出 | 同上 |
| "下载到的不是压缩包" | 刚发布没传播完 | 等一两分钟重试，**不是**网络配置问题 |
| "下载不完整" | 传输中断 | 直接重试，程序自己已经试过 3 次了 |
| 更新日志里是英文 | 故意的 | 见 `UPDATING.md`「为什么不写中文日志」 |
