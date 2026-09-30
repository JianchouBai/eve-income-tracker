# EVE 每日收益统计器

供单个玩家在 Windows 本机使用的 EVE 钱包流水与每日收益工具。它支持同一玩家多个账号下的多个角色、命名角色组、北京时间日报，以及刷怪收入、组外玩家转账和对应税费的来源与逐条明细。

## Windows 安装版使用

### 安装与启动

从项目的 GitHub Releases 下载版本化的 `EVE-Income-Tracker-<版本>-x64-Setup.exe`。每次发布都由维护者手动创建 GitHub Release；应用目前不自动更新。发布页应同时给出安装包的 SHA-256，下载后可用 `Get-FileHash` 核对。

安装包按当前 Windows 用户安装，不要求管理员权限，并创建开始菜单和桌面快捷方式。安装完成后直接双击 **EVE Income Tracker**：无需安装 Python，也无需打开终端。程序自带应用代码和页面资源；若电脑缺少 Microsoft Edge WebView2 Runtime，安装程序会调用微软官方引导程序安装它。

安装包目前没有代码签名证书，因此 Windows 可能显示“未知发布者”。只应运行从本项目正式 GitHub Release 下载且 SHA-256 与发布页一致的安装包；来源或哈希不明时请取消运行。

### 授权角色

安装版已经内置维护者注册的公开 Client ID。普通使用者不需要填写 Client ID，也不使用 Client Secret。点击“登录 EVE”或“添加角色”后，应用通过 PKCE 创建一次性授权尝试，并打开系统默认浏览器进入 EVE 官方 SSO；桌面窗口只轮询本机授权结果，不收集 EVE 密码。

EVE 应用只申请只读钱包权限 `esi-wallet.read_character_wallet.v1`。每次 EVE SSO 只授权本次选中的一个角色；同账号和不同账号下的每个角色都要分别授权。真实登录与同步需要能够访问 EVE 官方 SSO/ESI 服务。

新授权角色起初处于未分组。先创建角色组并为它命名，再将角色从“未分组角色”加入当前组；每个角色最多只能属于一个角色组。点击“同步钱包流水”会按当前角色组手动同步并合计，组内某个角色失败不会删除旧数据，也不会撤销其他角色已经完成的同步。

角色管理中的停用只影响默认报表选择，不改变组归属、授权或历史；手动勾选仍可查看它。重新启用后恢复参与默认合计。出现“需要重新授权”时，为对应角色重新授权；原组归属和统计起点会保留，不会创建重复角色。删除角色组不会删除角色、授权或钱包历史，原成员会回到未分组。

### 每台电脑的数据与旧版导入

每台电脑使用独立数据库，不会通过 Git 或应用自动同步到另一台电脑。首次在另一台电脑安装时会创建一套新数据，并需要为角色重新授权。

安装版的个人数据根目录为：

```text
%LOCALAPPDATA%\EVE Income Tracker
├─ data\eve_income.sqlite3
├─ config\local.key
├─ config\webview\
├─ logs\eve-income-tracker.log
└─ backups\
```

其中 `config\local.key` 是由 Windows DPAPI 保护的本机密钥，只供当前 Windows 用户读取。数据库、DPAPI 密钥、日志、备份、维护者的 `release-config.local.json` 和本地 `installer-output` 安装包均被仓库忽略，不会进入 Git，也不会随源码推送到远端仓库。

首次启动且尚无桌面数据库时，应用会询问是否导入旧版 v2 命令行数据库。选择导入只会把源数据库复制到上述新位置并在副本上迁移，源数据库不会被修改或删除；角色、角色组、钱包历史和原统计起点会保留。旧刷新令牌不会被搬入新的安全边界，因此导入后的每个角色都必须重新授权。选择“不导入”则为这台电脑创建空白的独立数据库。

### 升级与卸载

使用相同 AppId 的新安装包可以覆盖升级旧版本。升级只替换程序目录；升级不会删除 `%LOCALAPPDATA%\EVE Income Tracker` 下的数据库、密钥、日志和备份。数据库架构升级前会自动建立备份；若迁移失败，应用会尝试恢复原数据库。

卸载时默认保留个人数据，方便日后重装继续使用。只有主动选择“同时删除个人数据”，并对精确的 LocalAppData 路径进行第二次确认后，卸载程序才永久删除整套个人数据。静默卸载始终保留数据。

### 统计口径

每个角色从首次授权成功的精确时刻开始统计。ESI 可能返回该时刻之前的历史，但应用会主动丢弃，不导入也不展示。晚添加的角色有自己的独立起点；停用、重新启用和重新授权都不重置起点。首次自动同步失败也不会改变已保存的起点。

应定期手动同步，因为 ESI 的可回溯历史有限。日期按北京时间 `Asia/Shanghai` 的自然日分组，UTC 16:00 属于次日北京时间 00:00。默认显示最近 30 日，开始和结束日期都包含当天；单次查询最多 366 天，无活动日期补零。

- 到账收入是纳入白名单的正向钱包变动，不反推名义税前收益。
- 已交税费是对应负向税务流水的绝对值，不重复累加附加 `tax` 字段。
- 税后收入 = 到账收入 − 已交税费；金额使用十进制定点计算。
- 正向 `player_donation` 只有在付款角色不属于当前角色组时才计入收入，并在来源分项与流水明细中标为 `玩家转账`。
- 同一角色组内的角色相互转账不计入收入，即使付款角色已停用或未勾选进当前报表。
- `player_trading`、合同、忠诚点、掉落物估值及其他非目标收入不纳入合计，市场交易收入暂不统计。

### 本地安全、备份与排查

应用只监听 `127.0.0.1:8000`，不要改为 `0.0.0.0` 或暴露给局域网/公网。它面向一个本地使用者，不提供多人权限隔离。任何远程部署都需要另行设计 HTTPS、Secure Cookie 和访问控制；安装版的只读 ESI 权限不代表本机数据可以公开。

备份前先关闭应用，再复制整个 `%LOCALAPPDATA%\EVE Income Tracker`。数据库包含私人钱包历史；`local.key` 受 DPAPI 保护，不应上传或分享。把数据库复制到另一台电脑后，即使同时复制密钥，通常仍需要重新授权。丢失或损坏密钥不会删除历史，但已有令牌将无法解密。

重复启动只会提示应用已经运行，不会启动第二个本地服务。端口 8000 被占用、WebView2 缺失、数据库不可写或迁移失败时，应用会显示固定的中文错误；详细诊断记录在本机 `logs` 目录。不要通过删除数据库来修复启动问题，应先保留数据库、日志和备份。

发布维护者应在发布前逐项执行 [Windows 发布验收清单](docs/windows-release-checklist.md)。

## 开发与维护

安装版是普通用户的推荐入口；下面的流程只用于继续开发、运行源码或制作自己的安装包。源码模式需要 **Python 3.11+**，以下 Windows PowerShell 示例使用 Python 3.12。

### 创建开发环境

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

### 注册公共 EVE 应用

登录 [EVE Developers Portal](https://developers.eveonline.com/) 创建 **Authentication & API Access** 应用，并按官方 SSO 说明设置：

- 回调地址必须精确为 `http://localhost:8000/auth/callback`。
- 只申请 `esi-wallet.read_character_wallet.v1`。
- 只保存公开的 Client ID；本项目的 PKCE 流程不使用 Client Secret。

自动化测试和本地烟雾检查不能证明真实登录成功；真实登录必须使用你自己的公开 Client ID，并在系统浏览器完成授权。

### 配置与启动命令行开发服务

首次配置时复制模板；已有 `.env` 时保留原文件：

```powershell
if (-not (Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
notepad .env
```

生成至少 32 个字符的 `APP_SESSION_SECRET`：

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

生成开发模式使用的 Fernet `APP_ENCRYPTION_KEY`：

```powershell
.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

在 `.env` 中填写自己的 `EVE_CLIENT_ID`、上述两个随机值，并将 `EVE_USER_AGENT` 的示例邮箱替换为有效联系邮箱。默认 `DATABASE_PATH` 是 `data/eve_income.sqlite3`；不要提交 `.env`、数据库或密钥。`APP_ENCRYPTION_KEY` 丢失会导致开发数据库中的旧刷新令牌无法解密，需要为角色重新授权，但不会删除历史。

启动保留的命令行开发流程：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

打开 `http://localhost:8000`。浏览器地址和注册回调都使用 `localhost`，不要在登录途中切换成 `127.0.0.1`。停止服务时在终端按 `Ctrl+C`。

开发桌面入口复用同一套 FastAPI、HTML、CSS 和 JavaScript：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
.\.venv\Scripts\python.exe -m app.desktop
.\.venv\Scripts\python.exe -m app.desktop --smoke-test
```

### 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m ruff check app scripts tests
.\.venv\Scripts\python.exe scripts/smoke_app.py
```

源码烟雾检查使用临时配置和临时数据库，在 `127.0.0.1:8765` 启动真实 Uvicorn。成功时输出 `SMOKE_OK healthz=200 home=200`；它不读取真实 EVE 凭据，也不访问 SSO/ESI。

### 构建 Windows 安装包

发布构建要求 64 位 Windows、64 位 Python 3.12、锁定的 `requirements-build.txt`、带官方简体中文语言文件的 Inno Setup 7，以及微软官方 Evergreen WebView2 Bootstrapper。将微软签名的引导程序保存为 `vendor\MicrosoftEdgeWebview2Setup.exe`。

在项目根目录创建被 Git 忽略的 `release-config.local.json`。它只能包含公开 Client ID、固定回调地址、带联系邮箱的 User-Agent 和 ESI 兼容日期，不包含 Client Secret：

```json
{
  "eve_client_id": "你的公开 Client ID",
  "eve_callback_url": "http://localhost:8000/auth/callback",
  "eve_user_agent": "eve-income-tracker/0.1 (your-valid-email@example.com)",
  "esi_compatibility_date": "2026-05-19"
}
```

从干净的发布提交运行：

```powershell
py -3.12 -m pip install -r requirements-build.txt
.\scripts\build_windows.ps1
```

构建脚本会验证公开配置与微软签名，运行全量测试、Ruff 和 Git 差异检查，生成资源并执行 PyInstaller 冻结烟雾检查，最后用 Inno Setup 7 生成 `installer-output\EVE-Income-Tracker-<版本>-x64-Setup.exe` 并打印绝对路径和 SHA-256。`release-config.local.json`、WebView2 引导程序、`build`、`dist` 与 `installer-output` 均留在本机，不上传远端仓库。
