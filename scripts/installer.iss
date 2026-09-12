; NanoGhost Windows 安装包脚本 (Inno Setup 6)
;
; 编译:  scripts\build_installer.bat
; 或:    "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" scripts\installer.iss
;
; 先决条件: 先跑 build.bat 生成 dist\NanoGhost\（onedir 产物，必须整个目录）

#define MyAppName "NanoGhost"
#define MyAppExeName "NanoGhost.exe"
#define MyAppVersion Trim(FileRead(FileOpen("..\VERSION")))
#define MyBuildDir "..\dist\NanoGhost"
#define MyOutputDir "..\dist\installer"

[Setup]
; AppId 是升级识别的唯一标识，一旦发布不要再改，否则旧版本不会被覆盖安装
AppId={{7C4A1E92-5B3D-4F81-9A6E-2D8F0B7C1A34}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppName}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 默认按用户安装（不弹 UAC），用户仍可在安装界面切成全机器安装
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#MyOutputDir}
OutputBaseFilename=NanoGhostSetup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
; 升级时自动关闭正在运行的程序（走 Restart Manager），否则 exe 被占用会覆盖失败
CloseApplications=yes
RestartApplications=no

; 中文语言包（MIT，非官方翻译）。文件缺失时自动回退到英文，不会编译失败。
#ifexist "ChineseSimplified.isl"
[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
#endif

[Files]
Source: "{#MyBuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; 默认的自动更新配置。没有它客户装完就是"未配置 repo，升级不可用"——而客户
; 不可能知道这个文件的存在。（程序内也有默认 repo 兜底，这里落文件是为了让
; 配置可见、可改。）
;   onlyifdoesntexist   升级安装时不覆盖用户改过的配置
;   uninsneveruninstall 属于用户数据，随 .nanoghost 一起保留
; 注意是 {%USERPROFILE} 不是 {userprofile} —— Inno Setup 没有 userprofile 这个
; 常量，{%VAR} 才是"展开环境变量"的写法。写成 {userprofile} 会在编译期直接
; 报 Unknown constant 而整个安装包编不出来。
Source: "update.default.json"; DestDir: "{%USERPROFILE}\.nanoghost"; DestName: "update.json"; Flags: onlyifdoesntexist uninsneveruninstall

; 本机由外部项目负责启动和管理 NanoGhost，所以这里刻意不做三件事：
;   1. 不建快捷方式（[Icons] / [Tasks]）—— 手动点是废的，且会起一个管理器不知道的野进程
;   2. 不自动启动（[Run]）—— 同上
;   3. 不删用户数据（[UninstallDelete]）—— 见下方说明
; 保留的只有: 程序文件 + 卸载项。

; ── 部署注意事项 ──────────────────────────────────────────────
; 1. 用户数据在 %USERPROFILE%\.nanoghost（实例、记忆、会话库、密钥），不在安装目录内，
;    升级和卸载都不会动它。这是有意为之，不要加 UninstallDelete。
;
; 2. 上面的 CloseApplications=yes 会在安装时关掉正在运行的 NanoGhost。如果外部管理器
;    带自动拉起逻辑，它可能在文件覆盖期间把进程重新拉起来，导致覆盖失败（exe 被占用）。
;    升级前的正确做法：让外部管理器先停掉进程，装完再拉起。
;    程序内的 `NanoGhost.exe update` 同理——它最多等 60 秒进程退出，同样会被抢跑。
;
; 3. 部署后由外部管理器调起的命令行参考（实例名以实际为准）：
;      NanoGhost.exe -I <实例名>                    CLI 交互模式
;      NanoGhost.exe gateway start -I <实例名>      守护进程（飞书 bot）
;    实例由首次运行的配置向导创建，落在 %USERPROFILE%\.nanoghost\instances\<实例名>\
