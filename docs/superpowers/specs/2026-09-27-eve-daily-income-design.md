# EVE 每日收入统计器设计规格

## 目标

构建一个供单个玩家在本机运行、通过浏览器访问的中文网页应用。玩家可以通过 EVE Online SSO 逐个授权多个账号下的多个角色；应用读取每个已授权角色的钱包流水，默认合并全部启用角色，按中国标准时间（Asia/Shanghai，UTC+8）的自然日统计刷怪与任务产生的 ISK 收入、相关税费和税后净收入。

成功标准：

- 用户能重复使用 EVE SSO 添加多个角色；每个角色独立保存授权，网页默认显示所有启用角色的合计。
- 用户能按一个或多个角色筛选报表，不向网页前端暴露 Client Secret 或任何角色的刷新令牌。
- 页面准确展示今日与近 30 日的到账收入、税费和税后收入。
- 每日边界固定为北京时间 00:00:00（含）到次日 00:00:00（不含）。
- 重复同步不会重复计入同一条钱包流水。
- ESI 暂时不可用时保留已同步的数据，并向用户显示可理解的错误。
- 每个角色只统计它首次授权成功时刻及之后产生的流水；ESI 返回的更早历史不得写入或展示。
- 首次授权后的数据通过本地 SQLite 长期累计，重新授权或暂时停用角色不改变它的统计起点。

## 范围

### 纳入的收入

应用按 ESI 钱包流水的 `ref_type` 白名单分类，只纳入金额为正的记录：

- `bounty_prizes`：NPC 赏金结算。
- `ess_escrow_transfer`：由 ESS 托管释放的刷怪收入。
- `agent_mission_reward`：任务基础奖励。
- `agent_mission_time_bonus_reward`：任务限时奖励。

分类显示为“赏金”“ESS”“任务奖励”“任务限时奖励”。忠诚点、掉落物价值、市场出售收入、合同、玩家转账及其他钱包入账不在本期范围内。

### 纳入的税费

只纳入金额为负且直接对应上述活动的税务流水：

- `bounty_prize_corporation_tax`：赏金军团税。
- `agent_mission_reward_corporation_tax`：任务奖励军团税。
- `agent_mission_time_bonus_reward_corporation_tax`：任务限时奖励军团税。
- `npc_bounty_security_tax`：NPC 赏金安全税。
- `agent_mission_security_tax`：任务安全税。

统计使用这些税务流水的实际钱包变动绝对值。钱包记录中的附加 `tax` 字段可以原样存储以便排查，但不再次计入汇总，从而避免和独立税务流水重复计算。市场交易税、经纪人费、工业税、合同税等全部忽略。

### 汇总定义

- 到账收入：所选日期范围内纳入的正向收入流水之和。
- 已交税费：所选日期范围内纳入的负向税务流水绝对值之和。
- 税后收入：到账收入减去已交税费。
- 日归属：先把 ESI 的 UTC 时间转换为 `Asia/Shanghai`，再提取本地日期。

“到账收入”严格表示被纳入的正向钱包流水总额，不反推名义税前金额。这样所有数字都能与钱包流水逐条核对。

## 技术方案

应用采用 Python 3.11+ 单体架构：

- FastAPI 提供页面路由、OAuth 回调和 JSON API。
- Jinja2、原生 JavaScript 与 CSS 构建无需前端编译的响应式界面。
- SQLite 保存角色、加密令牌、钱包流水和同步状态。
- HTTPX 调用 EVE SSO 与 ESI。
- PyJWT 使用 EVE SSO 的 JWKS 验证访问令牌签名、签发者、受众、有效期与角色声明。
- Cryptography/Fernet 使用本机环境变量中的密钥加密刷新令牌。

项目不依赖 Node.js、外部数据库或前端 CDN。所有静态资源随项目提供；运行时只有 EVE SSO/ESI 需要网络。

## 组件边界

### 配置

配置从 `.env` 读取，至少包含：

- `EVE_CLIENT_ID`
- `EVE_CLIENT_SECRET`
- `EVE_CALLBACK_URL`，默认 `http://localhost:8000/auth/callback`
- `APP_SESSION_SECRET`
- `APP_ENCRYPTION_KEY`
- `DATABASE_PATH`，默认 `data/eve_income.sqlite3`
- `EVE_USER_AGENT`，必须包含应用名和用户自行填写的有效联系方式
- `ESI_COMPATIBILITY_DATE`，默认固定为 `2026-05-19`

`.env`、数据库和运行缓存必须在 `.gitignore` 中排除；仓库提供 `.env.example` 和生成安全密钥的说明。

### SSO 服务

- 使用 OAuth 2.0 Authorization Code 流程。
- 只请求 `esi-wallet.read_character_wallet.v1`。
- 登录时生成高强度随机 `state`，存入签名、HttpOnly、SameSite=Lax 的会话 Cookie，并在回调中常量时间比对。
- 通过 SSO 的 well-known 元数据发现授权、令牌与 JWKS 地址，并设置合理缓存。
- 回调交换令牌后验证 JWT；只接受 EVE 官方签发者、包含本应用 Client ID 与 `EVE Online` 的受众、未过期令牌以及 `CHARACTER:EVE:<id>` 格式的主体。
- 仅在服务端保存经 Fernet 加密的刷新令牌。访问令牌只保存在内存或短期会话状态中。
- EVE SSO 每次只授权用户在登录流程中选择的一个角色；用户通过“添加角色”重复授权多个角色。
- 新角色的 JWT 验证通过后，应用以服务器当前 UTC 时刻写入不可变的 `tracking_started_at`，再发起该角色的首次同步。
- 每个角色独立保存令牌和同步状态。重新授权已有角色时更新该角色的令牌，不创建重复记录，也不覆盖原有 `tracking_started_at`；授权新角色时追加到角色列表。
- 报表默认合计所有启用角色，但每条流水始终保留所属角色 ID，查询层不得把同一流水归到其他角色。

### ESI 客户端与同步服务

- 请求 `/characters/{character_id}/wallet/journal/`，处理 `X-Pages` 分页。
- 所有请求携带 `ESI_COMPATIBILITY_DATE` 指定的 `X-Compatibility-Date`、包含应用名与有效联系方式的 `User-Agent`，并使用 Bearer 令牌；未填写有效 `EVE_USER_AGENT` 时拒绝发起同步。
- 在访问令牌到期前使用刷新令牌换取新令牌；刷新失败时标记角色需要重新授权，但保留历史数据。
- 校验并解析全部分页后，只保留流水时间 `date >= tracking_started_at` 的记录；ESI 返回的更早流水直接丢弃，不写入数据库。边界使用 UTC 感知时间比较，不按日期粗略截断。
- 每个角色完整获取本次可用分页后，在一个独立数据库事务中按 `(character_id, journal_id)` upsert；该角色任一分页失败则不写入它的不完整批次。
- “同步全部”按角色依次执行，返回每个角色的成功、失败和新增条数。一个角色失败不回滚其他角色已完成的同步，也不清除任何旧数据。
- 同步成功后记录该角色的同步时间与新增条数。ESI 返回 420/429 时遵守 `Retry-After`，5xx 或网络错误进行有限次数、带抖动的退避重试。
- 不在后台高频轮询；首次授权后自动同步一次，之后由用户点击“同步数据”。

### 数据存储

`characters`：角色 ID（唯一键）、名称、加密刷新令牌、授权状态、是否纳入合计、不可变的首次授权时间 `tracking_started_at`、最后成功同步时间。

`wallet_journal`：角色 ID、ESI 流水 ID、UTC 时间、原始金额、余额、`ref_type`、描述、原因、关联 ID、税字段及必要的原始可选字段。`(character_id, journal_id)` 为唯一键。

金额列使用 SQLite `TEXT` 保存规范化的十进制定点字符串，读取后恢复为 Python `Decimal`；不得让 SQLite 的 `NUMERIC` 亲和性或 Python 二进制浮点数参与 ISK 累加。

查询层按 UTC 范围过滤候选记录，再以 `Asia/Shanghai` 规则归组。数据库迁移使用应用内按版本执行的幂等建表/升级函数，不引入独立迁移服务。

## 页面与交互

### 未授权状态

首页显示应用用途、只读权限说明、北京时间统计口径和“使用 EVE Online 登录”按钮。配置缺失时不生成无效登录链接，而是显示需要填写的配置项与 README 路径，绝不回显密钥值。

### 仪表盘

授权后首页显示：

- 已纳入统计的角色数量、角色名称列表、每个角色的统计起始时间、北京时间与各角色最后同步时间。
- 今日到账、今日税费、今日税后收入三张摘要卡。
- 最近 30 个北京时间自然日的收入/税费趋势图；使用本地 JavaScript Canvas 绘制。
- 按赏金、ESS、任务奖励、任务限时奖励拆分的汇总。
- 每日列表：日期、到账、税费、税后收入；无活动的日期也显示 0，保证趋势连续。
- 日期范围选择（默认近 30 日，最多查询本地已有历史）和“同步数据”按钮。
- 角色多选筛选器；默认选择全部已纳入角色，并显示各角色收入/税费分项。
- 可展开的已纳入流水明细，用于与游戏钱包核对。
- 角色管理区提供“添加角色”“重新授权”和“从合计中停用”；停用不删除该角色的历史或令牌。

视觉采用原创的深色太空仪表盘风格，不复制 EVE 客户端素材。桌面和移动浏览器均可用，键盘焦点清晰，并尊重 `prefers-reduced-motion`。

### JSON API

- `GET /api/characters`：返回已授权角色及其启用、授权和同步状态。
- `GET /api/dashboard?from=YYYY-MM-DD&to=YYYY-MM-DD&character_id=<id>...`：返回所选角色的合并摘要、连续日序列、类型汇总、角色分项和同步状态；未传角色参数时使用全部启用角色。
- `GET /api/entries?from=YYYY-MM-DD&to=YYYY-MM-DD&character_id=<id>...`：返回所选角色已纳入的收入与税务流水。
- `POST /api/sync`：依次同步全部启用角色，返回逐角色结果和完成时间。
- `POST /api/characters/{character_id}/enabled`：启用或停用角色参与默认合计。
- `POST /auth/logout`：清除浏览器会话；默认不删除本地已同步历史或已保存授权。

日期参数按北京时间日期解释，结束日期包含整天。拒绝无效日期、开始日期晚于结束日期以及超过 366 天的单次范围。

## 错误处理

- OAuth 被取消、`state` 不匹配、JWT 校验失败：拒绝登录并显示重新尝试提示，不记录令牌。
- 某个角色未授权或授权已撤销：仅将该角色标记为需要重新授权；缓存报表仍可读取但明确标注该角色不是最新数据，其他角色不受影响。
- 首次授权成功但自动同步失败：保留已经确定的 `tracking_started_at`，下次同步仍从该时刻开始，不回填更早数据。
- ESI 限流、维护或网络失败：API 返回结构化错误码和中文消息；页面保留当前数据，不清空图表。
- 数据库不可写或迁移失败：启动失败并打印明确路径与修复建议，避免以半可用状态运行。
- 未知 `ref_type`：保存原始流水但不计入；日志记录该类型，页面不报错。
- ESI 金额或时间格式非法：放弃整个本次同步事务并报告数据校验错误。

## 安全边界

- 服务默认只监听 `127.0.0.1:8000`，不默认暴露到局域网。
- Cookie 设置 HttpOnly、SameSite=Lax；本地 HTTP 下不设置 Secure，文档说明公网部署必须改用 HTTPS 与 Secure Cookie。
- 修改状态的请求使用会话 CSRF 令牌。
- 日志对授权码、访问令牌、刷新令牌、Client Secret 和 Cookie 做脱敏，异常信息不得包含这些值。
- SQL 全部参数化；模板默认转义；API 对日期、分页和字符串长度执行上限校验。

## 测试策略

单元测试覆盖：

- UTC 到北京时间的日期归属，包括 UTC 16:00 对应次日北京时间 00:00。
- 收入白名单、税费白名单、正负金额约束以及未知类型忽略。
- `Decimal` 汇总与税后计算。
- 连续日期补零与 366 天范围限制。
- 相同流水重复导入时的幂等性。
- 多角色合并、角色筛选、相同流水 ID 在不同角色下互不冲突，以及停用角色不进入默认合计。
- 首次授权前一秒的流水被丢弃、恰好位于 `tracking_started_at` 的流水被保留，以及重新授权不重置统计起点。
- 刷新令牌加密往返以及错误密钥拒绝解密。

集成测试使用 HTTPX MockTransport 或等价的内存传输，不访问真实 EVE 服务，覆盖：

- 登录 URL 的 scope/state 与回调 state 校验。
- JWT 声明验证。
- 多角色授权、多页钱包同步、逐角色令牌刷新、单个角色部分分页失败回滚、其他角色继续同步与限流错误映射。
- 首页、仪表盘 API、同步 API 的未授权与已授权流程。

最终验收运行完整 pytest 套件，并以测试专用配置启动应用，执行首页和健康检查的本地冒烟测试。真实 EVE 登录需要用户自行提供开发者应用凭据，因此作为人工验收步骤记录，不在自动化测试中伪造成功声明。

## 文档与运行

README 用中文说明：

1. 在 EVE Developers Portal 创建 Authentication & API Access 应用。
2. 注册精确回调地址 `http://localhost:8000/auth/callback`。
3. 复制 `.env.example` 为 `.env`，填写 Client ID、Client Secret 并生成两项应用密钥。
4. 创建虚拟环境并安装锁定依赖。
5. 启动 `uvicorn app.main:app --host 127.0.0.1 --port 8000`。
6. 打开 `http://localhost:8000`，通过“添加角色”为每个需要统计的角色分别完成授权，然后同步全部角色。

文档明确说明 ESI 钱包日志会返回首次授权之前的可用历史，但应用会主动丢弃它们。每个角色从首次授权成功的精确时刻开始统计，并依靠以后持续同步长期累计；晚于其他角色添加的新角色拥有自己的独立起点。

## 非目标

- 不支持军团钱包、多个本地使用者、多人权限管理或公网部署；支持同一位本地使用者聚合多个 EVE 账号下的多个角色。
- 不统计市场、制造、采矿、合同、忠诚点和资产估值。
- 不自动修改游戏内数据；申请的 ESI 权限是只读钱包权限。
- 不构建桌面安装包、移动应用或后台定时服务。

## 官方接口依据

- EVE SSO 使用 OAuth 2.0，回调地址必须与注册值匹配，并应校验 `state` 与令牌声明：<https://developers.eveonline.com/docs/services/sso/>
- ESI 是官方 REST API，受保护接口通过 SSO 授权，并支持固定兼容日期：<https://developers.eveonline.com/docs/services/esi/overview/>
- 钱包流水类型以 ESI 官方 `eve-glue` 枚举为准：<https://github.com/esi/eve-glue/blob/master/eve_glue/wallet_journal_ref.py>
