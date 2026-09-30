# EVE Income Tracker Windows 桌面版设计规格

日期：2026-09-29  
状态：已获用户批准

## 背景与目标

现有应用是仅供单个玩家本机使用的 FastAPI 网页工具，已经提供 EVE 角色授权、角色组、钱包同步、收入报表和流水明细，但日常使用仍要从 PowerShell 启动 Uvicorn。

目标是提供类似 pyfa 的 Windows 体验：通过安装程序安装，之后双击快捷方式，在独立窗口中查看钱包流水，不显示终端，也不要求目标电脑安装 Python。同一个安装包可以安装到另一台电脑，但每台电脑使用独立数据库和独立角色授权，不做设备间同步。

成功标准：

- 安装后可以从桌面或开始菜单打开独立窗口。
- 复用现有钱包流水、角色组、同步和报表。
- 只有 EVE 官方授权阶段临时打开系统浏览器，完成后桌面窗口自动更新。
- 客户端不包含或要求填写 Client Secret，授权改用 PKCE。
- 桌面版后续覆盖安装升级时，保留本机角色、分组、授权状态和全部钱包历史。
- 当前命令行版数据库可以一次性导入；历史保留，角色重新授权一次。
- Git 只保存源码、测试和构建配置，不保存钱包数据、令牌或本机密钥。
- 后续 UI 和业务开发继续修改现有 HTML、CSS、JavaScript 与 FastAPI 代码。

## 范围与非目标

范围包括 Windows 桌面启动层、单实例控制、本地服务生命周期、桌面本地会话、PKCE、应用数据目录、旧数据库导入、升级备份、PyInstaller、Inno Setup、WebView2 检查、构建脚本、测试和文档。

不包括：

- macOS、Linux、移动端、多用户、局域网访问或公网部署。
- 两台电脑之间同步或合并数据库。
- 自动更新服务；首版手工运行新版安装包升级。
- 商业代码签名；个人使用的安装包可能显示未知发布者。
- 使用原生控件重写当前网页 UI。
- 把安装包直接提交进 Git；安装包作为 GitHub Release 附件发布。
- 在安装包中保存 Client Secret、旧版 .env 或用户钱包数据。

## 方案选择

采用 PyWebView、现有 FastAPI、PyInstaller onedir 和 Inno Setup。

PyWebView 通过 Windows WebView2 承载现有网页，复用当前前端、API 和业务层。FastAPI 与 Uvicorn 在同一个程序进程的后台线程运行，只监听本机回环地址。PyInstaller onedir 生成自包含程序目录，Inno Setup 再封装成一个安装 EXE。

不采用 Qt WebEngine，因为安装体积和打包复杂度更高；不重写原生 UI，因为会增加长期维护成本；不采用 PyInstaller onefile 运行形态，因为每次启动都要解压且更难排错。用户仍只接触一个安装程序和一个快捷方式。

## 运行时架构

新增桌面入口 app/desktop.py：

1. 获取当前 Windows 用户范围的命名互斥量，阻止重复启动。
2. 解析安装资源目录和 LocalAppData 数据目录。
3. 初始化本机密钥、脱敏日志和数据库；升级前备份并迁移。
4. 在后台线程启动 Uvicorn，在端口 8000 同时绑定 IPv4 127.0.0.1 与 IPv6 ::1 回环地址，关闭访问日志，使用一个 worker。
5. 等待 /healthz；超时、端口冲突或初始化失败时显示原生中文错误。
6. 生成短时、单次桌面启动令牌，创建 PyWebView 窗口并加载 localhost。
7. 首页消费令牌、建立签名会话并立即重定向到干净 URL。
8. 关闭窗口时正常停止服务和 HTTP 客户端，并释放互斥量。

窗口使用 localhost，服务同时占用 127.0.0.1 与 ::1 回环地址，以匹配 http://localhost:8000/auth/callback 的 IPv4/IPv6 解析且不监听局域网。第二次启动由互斥量拦截；无关程序占用 8000 时只提示，不终止占用者。

现有 build_app 保持依赖注入结构并增加明确运行模式。命令行开发模式继续可用；安装版使用桌面模式。桌面自动会话不能削弱普通浏览器开发模式的认证和 CSRF。

## 组件边界

桌面启动器负责单实例、路径、服务器生命周期、健康检查、窗口、原生错误和打开系统浏览器。桌面桥只暴露授权需要的最小方法；外部 URL 必须使用 HTTPS、主机是 login.eveonline.com，且没有用户信息或异常端口。

Web 应用继续负责页面、API、角色、角色组、报表、同步、会话和 CSRF。业务层不依赖 PyWebView 或打包环境。

授权尝试保存在进程内，包含随机 state、PKCE verifier、所属会话、创建时间和结果。它支持创建、完成、查询和过期清理，不持久化授权码、verifier 或访问令牌。

路径与密钥服务集中决定开发与安装模式的资源、数据和日志位置。业务模块不再通过项目根目录推导安装版数据库。密钥服务生成 Fernet 密钥并使用 Windows DPAPI 按当前用户保护。

PyInstaller spec 收集模板、静态资源、时区数据和 PyWebView Windows 依赖，并排除未使用的 Qt 与 CEF 后端。Inno Setup 只安装程序、快捷方式和卸载器。

## 数据、配置与 Git

程序安装到 %LOCALAPPDATA%\Programs\EVE Income Tracker\。

个人数据保存到：

- %LOCALAPPDATA%\EVE Income Tracker\data\eve_income.sqlite3
- %LOCALAPPDATA%\EVE Income Tracker\config\local.key
- %LOCALAPPDATA%\EVE Income Tracker\logs\eve-income-tracker.log
- %LOCALAPPDATA%\EVE Income Tracker\backups\

安装目录按只读处理。每台电脑首次运行时独立创建数据和密钥。

发布构建需要公开的 Client ID、固定回调、ESI 兼容日期和有效 User-Agent 联系信息。构建脚本从被 Git 忽略的本地配置或环境变量读取，并生成只进入构建产物的配置。Client Secret 从设置、示例、测试和文档删除。

Fernet 密钥首次启动时随机生成，经 Windows DPAPI 当前用户范围保护后写入 local.key；刷新令牌继续以 Fernet 密文保存在 SQLite。钱包流水不做整库加密，依赖 Windows 用户目录权限保护。

Git 忽略数据库、备份、密钥、日志、.env、本机构建配置、WebView2 下载文件、build、dist 和安装包输出。远端仓库只保存源码、固定依赖、构建脚本、示例配置和文档。

## 数据库升级与旧版导入

继续使用 PRAGMA user_version 和顺序迁移：

- 高于应用支持版本时拒绝启动，不尝试降级。
- 迁移前关闭写入并把数据库复制到 backups，名称包含版本和 UTC 时间。
- 迁移在事务中完成；失败时回滚并恢复备份。
- 成功后至少保留最近三份自动备份。
- 覆盖安装不接触个人数据目录。

首次启动且目标数据库不存在时提供“导入旧版数据库”。应用先验证用户选择的 eve_income.sqlite3，再复制并迁移。保留角色、角色组、启用状态、统计起点、最后同步时间和全部钱包流水。

旧令牌来自 Client Secret 流程并由旧 .env 密钥加密。桌面版不复制旧 .env；导入事务清空旧令牌并把角色标记为需要重新授权。重新授权沿用现有 upsert，不重置分组、统计起点或历史流水。

卸载默认保留个人数据。只有用户明确选择“同时删除个人数据”时才删除精确的数据目录。旧数据库导入始终先复制后验证，不修改原文件；目标已有数据库时拒绝覆盖。

## PKCE 授权

1. 桌面会话通过 CSRF 保护请求创建授权尝试。
2. 服务生成高熵 state 和 code_verifier，以 SHA-256 生成无填充 base64url challenge，方法固定 S256。
3. 服务返回不可猜测的尝试 ID 和 EVE 官方授权 URL。
4. 桌面桥校验 URL 后用系统浏览器打开；账号、密码与 2FA 只输入官方页面。
5. EVE 回调 localhost。服务验证未过期且未使用的 state，并用 Client ID、授权码、回调与 verifier 换取令牌。
6. 沿用 JWT 签名、算法、密钥 ID、签发者、受众、有效期、角色身份、名称和钱包 scope 校验。
7. 成功后加密刷新令牌、保存角色并尝试首次同步；同步失败不撤销授权。
8. 回调页只显示安全结果，不显示授权码、令牌或提供方原始错误。
9. 桌面页轮询所属尝试，成功后刷新数据并删除尝试。

授权尝试十分钟过期、只能消费一次并绑定发起会话。未知、过期、重复和跨会话状态都被拒绝。刷新令牌时发送 refresh token 和 Client ID，不发送 Basic Client Secret。令牌被撤销时只要求该角色重新授权，保留历史。

## 本地安全

- Uvicorn 只绑定 127.0.0.1 与 ::1 回环地址，不提供改为 0.0.0.0 或 :: 的桌面设置。
- 关闭访问日志，避免回调参数进入普通日志。
- 桌面启动令牌至少 256 位随机、短时有效、一次消费并清理 URL。
- Cookie 保持签名、HttpOnly 和 SameSite=Lax；修改请求继续校验 CSRF。
- JS 桥不提供任意命令、任意文件读取或任意 URL 打开。
- 日志不记录凭据、令牌、授权码、Cookie、verifier、钱包描述或数据库内容。
- 数据目录不可写、DPAPI 失败或数据库损坏时停止写入，不自动删除数据库。
- 本机密钥损坏时可重置授权并重新授权角色，但保留角色结构和历史。

## 安装、构建与发布

PyInstaller 使用 Windows x64、onedir 和 windowed。主程序包含产品名、版本和图标，不创建控制台窗口。依赖固定版本。

Inno Setup 使用固定 AppId、PrivilegesRequired=lowest 和当前用户安装目录，创建桌面与开始菜单快捷方式。相同 AppId 的新版覆盖升级旧程序。

WebView2 使用 Evergreen Runtime。安装程序检查官方注册信息；缺失时运行随包附带的微软 Evergreen Bootstrapper。失败时中止安装并显示错误。应用启动时再做一次检查。

新增 scripts/build_windows.ps1：

1. 验证 Python、Inno Setup、发布配置和 WebView2 Bootstrapper。
2. 运行完整 pytest、Ruff 和 git diff --check。
3. 清理项目专用构建输出。
4. 通过 PyInstaller spec 生成 onedir。
5. 用临时 LocalAppData 冒烟测试打包产物，不读取真实数据，不访问 EVE。
6. 用 Inno Setup 生成版本化安装包。
7. 输出安装包绝对路径和 SHA-256。

版本号只有一个来源，并写入 Python 包、EXE 和安装脚本。正式发布从干净提交构建。首版由用户手工把安装包上传到 GitHub Release。

## 错误处理

启动阶段以原生中文对话框报告：程序已运行、端口 8000 被占用、WebView2 不可用、数据目录不可写、密钥不可读、数据库版本过新或损坏、迁移失败以及健康检查超时。

运行阶段继续区分网络不可用、EVE 限流、授权失效、数据无效和同步失败。单个角色失败保留旧数据且不撤销其他角色已完成同步。每个错误都给出下一步操作。

## 测试策略

至少覆盖：

- 开发与安装模式路径，以及不同模拟用户目录之间的数据隔离。
- DPAPI 密钥生成、复用和损坏处理。
- Git 忽略规则、旧版版本 2 数据库导入、未知或损坏数据库拒绝。
- 迁移前备份、保留最近三份、失败回滚与恢复。
- PKCE URL、challenge、verifier、令牌交换与刷新均不使用 Client Secret。
- 未知、过期、重复和跨会话回调拒绝；现有 JWT 严格验证继续通过。
- 桌面启动令牌、CSRF 和桌面桥 URL 白名单。
- 日志和错误不泄露授权码、verifier、令牌或密钥。
- 健康检查后才创建窗口，重复启动与端口冲突可见，关闭窗口正常停止服务。
- 模板、静态资源、时区和 SQLite 在打包产物中可用。
- 安装脚本稳定升级、默认卸载保留数据、WebView2 检测与安装。
- 第二台干净 Windows 电脑的安装、启动、重新授权、同步和卸载。
- 现有配置、SSO、ESI、同步、存储、报表、Web、前端和安全回归测试。
- 完成前运行完整 pytest、Ruff、git diff --check、源码和打包冒烟测试。

## 实施与交付

规格提交到当前 main。实施计划获批后，在独立 worktree 和功能分支开发：

- 分支：feature/windows-desktop-app
- worktree：.worktrees/windows-desktop-app

功能分支不得包含真实 .env、本机构建配置、数据库、密钥、WebView2 下载产物或安装包。实现完成并验证后，由用户决定合并、推送功能分支和创建 GitHub Release。

## 参考

- EVE SSO 与 PKCE：https://developers.eveonline.com/docs/services/sso/
- PyWebView 打包：https://pywebview.idepy.com/en/guide/freezing
- PyInstaller：https://pyinstaller.org/en/stable/operating-mode.html
- WebView2 Runtime：https://learn.microsoft.com/microsoft-edge/webview2/concepts/distribution
- Inno Setup AppId：https://jrsoftware.org/ishelp/topic_setup_appid.htm
- Inno Setup 权限：https://jrsoftware.org/ishelp/topic_setup_privilegesrequired.htm
