# EVE 每日收益统计器

供单个玩家在本机使用的中文网页工具，支持同一玩家多个 EVE 账号下的多个角色，并可把角色组织成命名角色组。使用官方只读钱包权限，按当前角色组合计已选角色的刷怪收入、外部玩家捐赠及对应税费，同时提供来源、角色和逐条流水分项。

需要 **Python 3.11+**；下面的 Windows PowerShell 示例使用 Python 3.12。无需 Node.js、前端构建或外部数据库。页面资源保存在本地，真实授权与同步需要访问 EVE 官方 SSO/ESI 服务。

## Windows 桌面入口（开发与构建验证）

桌面版复用同一套 FastAPI、HTML、CSS 和 JavaScript。开发桌面入口需要额外安装锁定的 Windows 依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
.\.venv\Scripts\python.exe -m app.desktop
```

桌面入口只监听 `127.0.0.1:8000`，窗口固定使用 EdgeChromium/WebView2；个人数据库、DPAPI 密钥、日志和备份保存在当前 Windows 用户的 `%LOCALAPPDATA%\EVE Income Tracker`。源码及打包产物可用以下命令执行不访问 EVE 的烟雾检查：

```powershell
.\.venv\Scripts\python.exe -m app.desktop --smoke-test
```

正常桌面启动读取随构建产物提供的公开 `release-config.json`，不读取 `.env`；命令行开发模式仍使用后文的 `.env` 和 Uvicorn 启动方式。

## 1. 创建虚拟环境

在项目根目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

直接调用虚拟环境的 Python，无需修改 PowerShell 执行策略或激活脚本。使用其他受支持版本时，可将创建环境命令中的 `-3.12` 替换为对应版本。

## 2. 注册 EVE 应用

登录 [EVE Developers Portal](https://developers.eveonline.com/) 创建 **Authentication & API Access** 应用，并按官方 [SSO 说明](https://developers.eveonline.com/docs/services/sso/) 设置：

- 回调地址必须精确为 `http://localhost:8000/auth/callback`，包括协议、主机、端口和路径。
- 只申请 `esi-wallet.read_character_wallet.v1`，用于读取角色钱包流水，不修改游戏内数据。
- 将你自己的 Client ID 和 Client Secret 填入本地 `.env`，不要上传或公开。

真实登录必须由用户提供自己的 EVE 应用凭据，并在浏览器中完成授权；自动化测试与本地冒烟检查不能证明真实 EVE 登录或同步成功。

## 3. 配置本地环境

首次配置时复制模板；已有 `.env` 时保留原文件：

```powershell
if (-not (Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
notepad .env
```

分别运行以下命令，将输出填写到对应配置项。每项只需生成一次，切勿把输出粘贴到公开聊天、日志或仓库。

生成至少 32 个字符的 `APP_SESSION_SECRET`：

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

生成 Fernet `APP_ENCRYPTION_KEY`：

```powershell
.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

| 配置项 | 填写内容 |
| --- | --- |
| `EVE_CLIENT_ID` | 自己注册的应用 Client ID |
| `EVE_CLIENT_SECRET` | 自己注册的应用 Client Secret |
| `EVE_CALLBACK_URL` | `http://localhost:8000/auth/callback` |
| `APP_SESSION_SECRET` | 上面生成的随机会话密钥 |
| `APP_ENCRYPTION_KEY` | 上面生成的 Fernet 密钥，必须妥善保存 |
| `DATABASE_PATH` | 默认 `data/eve_income.sqlite3`；相对路径以项目根目录为准 |
| `EVE_USER_AGENT` | 保留应用名 `eve-income-tracker`，将 `your-email@example.com` 替换成自己的有效联系邮箱 |
| `ESI_COMPATIBILITY_DATE` | 固定为 `2026-05-19`，不要随意改成当天日期 |

`.env` 从项目根目录加载。模板中的空凭据和示例邮箱不能直接用于授权；缺失配置时首页只列出需要填写的环境变量名，禁用登录与同步，不显示密钥值。

## 4. 启动、添加角色与建立角色组

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

打开 `http://localhost:8000`。浏览器地址和注册回调都使用 `localhost`，不要在登录途中换成 `127.0.0.1`，以免会话 Cookie 不匹配。停止服务时在该终端按 `Ctrl+C`。

每次 EVE SSO 只授权登录流程中选中的一个角色。通过“添加角色”为每个角色分别完成授权；同账号的其他角色、不同账号的角色都要重复此步骤。新授权角色起初处于未分组，先在监控台创建角色组并为它命名，再从“未分组角色”将角色加入当前组。每个角色最多只能属于一个角色组；要换组时，先将角色移回未分组，再切换目标组并加入。

首次授权后自动同步该角色一次，但未分组角色的流水不会混入任何组的报表。之后点击“同步钱包流水”按当前角色组手动同步，组内各角色分别返回结果；应用不在后台高频轮询。某个角色失败不会清除旧报表，也不会撤销其他角色已成功的同步。

角色管理中的停用只将该角色移出当前组的默认报表选择，不改变组归属，也不删除历史或授权；手动勾选仍可查看它。同步仍以当前组为范围。重新启用后恢复参与默认合计。提示“需要重新授权”时，为对应角色再次完成 SSO；重新授权会更新令牌、保留原组归属，不创建重复角色。

删除角色组不会删除角色、授权或钱包历史，原成员会回到未分组。角色只有加入另一个组后，其既有流水才会按新组范围参与报表；删除操作本身不会改写任何钱包流水。

## 5. 统计口径

每个角色从**首次授权成功的精确时刻**开始统计。ESI 可能返回该时刻之前的历史，但应用会主动丢弃，不导入也不展示。晚添加的角色有自己的独立起点；停用、重新启用和重新授权都不重置起点。首次自动同步失败也不会改变已保存的起点。

之后持续手动同步，将 ESI 仍可提供的流水长期保存在本地 SQLite；ESI 的可回溯历史有限，长期不运行可能遗漏已不再提供的流水，因此应定期同步。

日期一律按北京时间 `Asia/Shanghai` 的自然日分组，UTC 16:00 属于次日北京时间 00:00。默认显示最近 30 日，开始和结束日期均包含当天；单次查询最多 366 天，无活动日期补零。

- 到账收入是纳入白名单的正向钱包变动，不反推名义税前收益。
- 已交税费是对应负向税务流水的绝对值，不重复累加附加 `tax` 字段。
- 税后收入 = 到账收入 − 已交税费。金额使用十进制定点计算，避免浮点累加误差。
- 正向 `player_donation` 只有在付款角色不属于当前角色组时才计入收入，并在来源分项与流水明细中单独标为 `玩家转账`；总收入仍会把它与刷怪收入合计显示。
- 同一角色组内的角色相互转账不计入收入，即使付款角色已停用或没有被勾选进当前报表。其他组角色或未分组角色的正向捐赠视为组外转账。
- `player_trading`、合同、忠诚点、掉落物估值及其他非目标收入不纳入合计，市场交易收入暂不统计。

## 6. 本地安全与备份

只监听 `127.0.0.1`，不要改成 `0.0.0.0`、开放路由器端口或暴露给局域网。应用面向一个本地使用者，不提供多人权限隔离，也不支持直接公网部署。任何另行设计的远程部署至少必须使用 HTTPS 与 Secure Cookie，并重新审查访问控制；本地默认配置不适用于公网。

会话 Cookie 使用签名、HttpOnly 和 SameSite=Lax；修改数据的请求还有 CSRF 校验。刷新令牌在 SQLite 中由 Fernet 加密，访问令牌仅用于服务端处理。启动命令关闭 Uvicorn 访问日志，避免 OAuth 回调 URL 中的授权码出现在常规访问记录中。不要分享 `.env`、数据库、Cookie、令牌或含敏感信息的截图。

备份前停止应用，然后备份 SQLite 文件，并将 `.env` 中的 `APP_ENCRYPTION_KEY` 安全地另行保存；备份数据库也包含私人钱包历史、角色组名称和成员关系，需要保护。恢复加密刷新令牌必须使用原有加密密钥。密钥丢失或随意替换会使已有令牌无法解密，需要重新授权；恢复已有数据库后重新授权仍保留角色原统计起点。不要为了每次启动而重生成加密密钥。

更换会话密钥会使浏览器会话失效；退出登录只清除浏览器会话，不删除本地历史和已保存的角色授权。`.env`、默认 `data/` 和 SQLite 文件均已由 Git 忽略；自行选择其他备份路径时也应避免提交它们。

## 7. 本地验证与排查

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe scripts/smoke_app.py
```

冒烟检查会用临时配置和临时数据库，在 `127.0.0.1:8765` 启动真实 Uvicorn，仅检查首页和 `/healthz`。成功输出 `SMOKE_OK healthz=200 home=200`，随后清理本次子进程和临时数据；它不读取真实 EVE 凭据、不发起 SSO/ESI 请求。如果 8765 端口已被占用，脚本会失败退出，不终止占用者，请先自行处理端口冲突。

首页提示配置缺失时检查 `.env`；回调失败时检查注册地址完全一致，并从原浏览器重新开始登录。同步失败时保留旧数据，可稍后重试；授权失效则为该角色重新授权。数据库路径不可写时检查路径和当前用户权限，不要删除数据库来“修复”启动失败。
