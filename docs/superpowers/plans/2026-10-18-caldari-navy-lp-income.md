# C 海军 LP 收入统计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 读取 `LP对账-C海军` 的累计个人贡献，建立独立基线，将新增差额乘以 0.95 后计入成功同步当天的北京时间 LP 收入。

**Architecture:** LP 使用独立的快照、基线段和入账明细，继续共用现有 SQLite 数据库、ESI 客户端和手动同步入口。钱包与 LP 共用一次已验证授权，但独立提交、独立报告结果。项目首次按名称及 C 海军来源识别，之后按玩家军团 ID 和项目 UUID 追踪。

**Tech Stack:** Python 3.11+、FastAPI、httpx、PyJWT、SQLite、Decimal、原生 HTML/CSS/JavaScript、pytest、Ruff；不增加运行时依赖。

**Spec:** [已批准的设计](../specs/2026-10-18-caldari-navy-lp-income-design.md)。开始每项任务前同时阅读设计和本计划。

## Global Constraints

- 仅追踪 `LP对账-C海军`；发放 LP 的 NPC 军团必须仅为 `1000035`，项目必须为 `earn_loyalty_point`，倍率缺省或为 1。
- 保留 `esi-wallet.read_character_wallet.v1`，新增 `esi-corporations.read_projects.v1`；继续 PKCE、原 Client ID 和 `http://localhost:8000/auth/callback`，不使用 Client Secret。
- ESI 兼容日期保持 `2026-05-19`；列表使用 `state=All` 和游标分页；只请求角色自己的贡献。
- 首次成功观察只建立基线；`gross_lp = current_contributed - previous_contributed`，`deduction_lp = gross_lp × Decimal("0.05")`，`net_lp = gross_lp - deduction_lp`。
- `observed_at` 是响应成功校验后的 UTC 时间；按 `Asia/Shanghai` 归日；提交跨日不改归属；多日未同步的差额不向前摊分。
- 不降低异常读数的基线；配置不兼容后恢复必须开启新基线段；不同项目 ID 不互相做差；改名不重建基线。
- 钱包与 LP 独立提交和展示。LP 无权限不阻断钱包；未知显示 `—`，已确认没有新增才显示零；部分覆盖显示不完整提示。
- 精确整数与 Decimal 存储，API 使用十进制字符串，LP 展示最多两位小数；不与 ISK 相加，不逐次取整，不重算历史扣率。
- schema 2 升为 3，保留已有分组、启停、钱包起点和流水；旧版本导入、授权重置、目录迁移保留 LP 历史与基线。
- 本轮不操作开发者账户、不读取私有令牌、不运行真实收益验证、不构建安装包、不发布或推送。
- 执行前使用 using-git-worktrees 技能建立隔离工作区，分支建议 `feature/caldari-navy-lp-income`。计划审阅不创建产品代码。

## Review Focus

1. ESI 游标重复或分页响应损坏：有限终止且不选择不完整列表中的项目；Task 3 测试锁定。
2. 配置暂停后恢复或同名项目重建：已有累计值不混入新段收入，旧尾账缺口保留；Tasks 1、4、5 测试锁定。
3. 两次同步与重新授权交错：刷新令牌不被旧请求覆盖，入账只发生一次；Tasks 1、4 测试锁定。
4. 旧版导入或 DPAPI 授权重置：清除令牌和 scope，不清除 LP 业务表；Task 1 测试锁定。
5. 部分角色有历史收入但当前失去 LP 能力：仍显示已知合计并提示不完整，不将其变零；Tasks 5、6 测试锁定。

## 文件和接口边界

| 文件 | 职责 |
| --- | --- |
| 新建 `app/lp_models.py` | LP 数据类型、常量和金额拆分；不依赖 Web 或网络 |
| 新建 `app/lp_storage.py` | LP schema 创建、快照／基线／入账事务、查询和覆盖问题记录 |
| 新建 `app/lp_sync.py` | 项目选择、配置校验、尾账和单角色 LP 同步 |
| 新建 `app/lp_reporting.py` | 独立 LP 汇总、北京时间逐日分组和覆盖状态 |
| 修改 `app/storage.py`、`app/models.py`、`app/database_lifecycle.py` | schema 3、scope 持久化、升级和导入保留 |
| 修改 `app/sso.py`、`app/esi.py` | 请求并核验权限；共享 HTTP 重试和读取项目 |
| 修改 `app/sync.py`、`app/application.py`、`app/web.py` | 单角色串行、服务装配、授权保存、独立结果和报表序列化 |
| 修改 `app/static/app.js`、`app/static/app.css`、`app/templates/index.html` | 替换 LP 占位、状态和同步文案，保留现有布局 |
| 修改 `README.md`、`docs/windows-release-checklist.md` 及相关测试 | 权限升级操作、统计口径、人工验收和回归覆盖 |

所有 LP 数据类型使用冻结 dataclass；日期时间为有时区 UTC，项目 ID 规范化为 UUID 字符串。下列类型由 Task 1 定义，后续任务只使用它们，不另建同名返回字典：

- `LpProject(corporation_id, project_id, name, state, source_corporation_ids, multiplier, current, desired, participation_limit)`：来源集合及倍率允许未知，用于明确拒绝不兼容配置。
- `LpContribution(contributed: int, last_modified: datetime | None)`；`LpObservation(character_id, project: LpProject, contribution: LpContribution, observed_at: datetime)`。
- `LpAmounts(gross: Decimal, deduction: Decimal, net: Decimal)`；`LpAmounts.zero()` 返回三项零值。
- `LpTracking`：角色／玩家军团／项目键、当前段、已接受值、基线时间、最后成功时间和尾账状态。
- `LpIncomeEntry`：快照 ID、角色与项目身份、段 ID、前后值、起止观察时间、扣率和 `amounts: LpAmounts`。
- `LpSegment`：段 ID、角色与项目键、开始时间、结束时间及开启原因。
- `LpProblem`：角色、可选项目键、`code`、开始和结束时间、`irrecoverable`；失败恢复且累计连续时不留下永久缺口。
- `LpHistory(entries, segments, problems, tracking)`：各项为相应记录的 tuple，供报表消费。
- `LpSyncResult(character_id: int, status: str, completed_at: datetime, baseline_created: bool, amounts: LpAmounts, error_code: str | None, warnings: tuple[str, ...])`。
- LP 状态为 `baseline`、`ok`、`authorization_required`、`unavailable`、`anomaly`、`failed`。错误与提示均使用白名单 code，原始响应不回传。

## Task 1: LP 数据模型和持久化

**Files:** Create `app/lp_models.py`、`app/lp_storage.py`、`tests/test_lp_storage.py`；Modify `app/models.py:CharacterRecord`、`app/storage.py:Database.initialize/CharacterRepository`、`app/database_lifecycle.py:_REQUIRED_SCHEMA/_invalidate_credentials`、`tests/test_storage.py`、`tests/test_database_lifecycle.py`。

**Interfaces:** Consumes `Database.connection()` 和现有角色记录。Produces 上述 LP 类型、`split_lp(gross: int, rate: Decimal = Decimal("0.05")) -> LpAmounts`、`create_lp_schema(connection: sqlite3.Connection) -> None`，以及 `LpRepository(database: Database)` 的方法：

```python
get_selected_project(corporation_id: int) -> LpProject | None
select_project(project: LpProject, selected_at: datetime) -> None
list_tracking(character_id: int) -> tuple[LpTracking, ...]
commit_observation(observation: LpObservation, *, deduction_rate: Decimal = Decimal("0.05"), rebaseline: bool = False, tail_settled: bool = False) -> LpSyncResult
record_problem(character_id: int, code: str, at: datetime, *, corporation_id: int | None = None, project_id: str | None = None, irrecoverable: bool = False) -> None
resolve_problems(character_id: int, at: datetime) -> None
report_history(character_ids: set[int], start_utc: datetime, end_utc: datetime) -> LpHistory
```

`CharacterRecord` 增加末尾默认字段 `granted_scopes: tuple[str, ...] = ()`；`upsert_authorization(..., *, granted_scopes: tuple[str, ...] = ())` 保留原位置参数；新增 `replace_granted_scopes(character_id: int, scopes: tuple[str, ...]) -> None`。

- [ ] **Step 1: 写持久化失败测试。** `test_first_observation_is_only_baseline` 断言已有 `1_000_000` 贡献得到 `status == "baseline"` 且没有收入。`test_delta_persists_precise_amounts` 使用后续 `1_200_000`，断言：

```python
assert result.amounts == LpAmounts(Decimal("200000"), Decimal("10000"), Decimal("190000"))
assert len(history.entries) == 1
assert history.entries[0].previous_contributed == 1_000_000
assert history.entries[0].current_contributed == 1_200_000
```

同时覆盖 `test_equal_and_lower_readings_do_not_rebook_or_lower_baseline`、`test_failed_income_insert_rolls_back_snapshot_and_baseline`、`test_two_connections_book_same_increment_once`、`test_rebaseline_keeps_old_entries_and_excludes_paused_growth`。分别断言零新增／异常、触发器阻断收入写入后全回滚、两个连接最终只有一笔差额、新段首读不入账。bool、负数、`2**63`、无时区时间拒绝；`split_lp(1).net == Decimal("0.95")`，20 次单点与一次 20 点的净值同为 `Decimal("19")`。

- [ ] **Step 2: 运行红灯。** `./.venv/Scripts/python.exe -m pytest tests/test_lp_storage.py -q`，预期因新模块／行为尚不存在失败；不得接受环境或导入路径配置导致的假红灯。
- [ ] **Step 3: 实现模型、schema 和事务。** 建表 `lp_projects`、`lp_tracking`、`lp_segments`、`lp_snapshots`、`lp_income_entries`、`lp_problems`，对应上述类型。项目复合主键 `(corporation_id, project_id)`，为选中项目设置每军团唯一的部分索引；追踪键为三元组；入账 `snapshot_id` 唯一；角色外键有效。快照和收入均关联段。金额与时间为规范化 TEXT，贡献为 INTEGER 并检查非负及上界。`BEGIN IMMEDIATE` 内重新读取基线，写快照／收入／基线后一起提交；异常观察保留原段值。`rebaseline` 关闭旧段并建立新段，记录不可恢复缺口。仅正差额写收入。

将最新 schema 设为 3，0／1／2 均迁移到同一结构，保留旧 schema 创建函数作为迁移步骤。`characters.granted_scopes` 为 JSON TEXT 默认 `[]`，规范化且只写已验证值。生命周期识别包含新表和新列；导入及授权重置同时清空 scope 和凭证，保留 LP 表。新增 `test_v3_import_and_authorization_reset_preserve_lp_and_clear_scopes`，用带基线和入账的数据库断言保留前后读数和 LP 金额。真正的 v2 固定结构 fixture 仍保持 v2；只更新“最新版本”断言，不把所有字面量 2 全局替换。

- [ ] **Step 4: 运行绿灯。** `./.venv/Scripts/python.exe -m pytest tests/test_lp_storage.py tests/test_storage.py tests/test_database_lifecycle.py -q`，预期全通过；包括幂等迁移、失败回滚、备份恢复和未来版本拒绝。
- [ ] **Step 5: 提交。** 仅暂存本任务文件，`git diff --cached --check` 通过后提交 `feat: persist LP observations and income safely`。

## Task 2: 升级角色授权能力

**Files:** Modify `app/sso.py:WALLET_SCOPE/build_authorization_url/verify_access_token`、`app/web.py:callback`、`tests/test_sso.py`、`tests/test_web.py`。

**Interfaces:** Consumes Task 1 的 scope 持久化；Produces `PROJECTS_SCOPE = "esi-corporations.read_projects.v1"`、`REQUESTED_SCOPES = (WALLET_SCOPE, PROJECTS_SCOPE)`。`verify_access_token(access_token: str) -> CharacterIdentity` 保留签名，只要求钱包 scope，返回所有已验证 scope；授权 callback 保存 `identity.scopes`。

- [ ] **Step 1: 写失败测试。** `test_authorization_requests_wallet_and_projects_scope` 断言 URL 解码后的 `scope == "esi-wallet.read_character_wallet.v1 esi-corporations.read_projects.v1"` 且 PKCE 参数不变。`test_wallet_only_identity_remains_valid` 断言旧令牌通过验证。`test_reauthorization_saves_scopes_without_resetting_tracking` 断言角色组、钱包起点、LP 基线和入账原样保留，仅凭证和已验证 scope 更新；拒绝签名无效的 scope 声明且不存储。保留原 issuer、audience、算法和到期校验测试。
- [ ] **Step 2: 运行红灯。** `./.venv/Scripts/python.exe -m pytest tests/test_sso.py tests/test_web.py -q`，新增 URL／持久化断言因尚未实现而失败。
- [ ] **Step 3: 实现请求和保存。** scope 以空格连接，新增权限不是基本登录的强制条件。真实 callback 和其 fixtures 均保存通过 JWT 验证的 scope；此任务不在刷新时猜测额外权限，不增 Client Secret 或个人 LP scope。
- [ ] **Step 4: 运行绿灯。** 重跑上述命令，预期全通过，且敏感信息不进入错误或日志。
- [ ] **Step 5: 提交。** 检查暂存差异后提交 `feat: request corporation project read access`。

## Task 3: ESI 项目和个人贡献读取

**Files:** Modify `app/esi.py:EsiClient`、`tests/test_esi.py`；Create `tests/test_lp_esi.py`。

**Interfaces:** Consumes Task 1 的 `LpProject`、`LpContribution`。Produces 以下 `EsiClient` 方法，字符串项目 ID 在构造 URL 前校验 UUID；`EsiNotFoundError(EsiError)` 表示 404，`EsiProjectPermissionError(EsiError)` 表示项目 403：

```python
async def fetch_character_corporation(self, character_id: int) -> int: ...
async def fetch_corporation_projects(self, access_token: str, corporation_id: int) -> tuple[LpProject, ...]: ...
async def fetch_project(self, access_token: str, corporation_id: int, project_id: str) -> LpProject: ...
async def fetch_project_contribution(self, access_token: str, corporation_id: int, project_id: str, character_id: int) -> LpContribution: ...
```

列表返回的 `LpProject` 可带未知来源／倍率，必须读取详情才能用于绑定。共享私有 `_get_response(access_token: str | None, url: str, *, params: dict[str, str | int] | None = None) -> httpx.Response`；URL 只由内部固定主机及校验后的 ID 构造。

- [ ] **Step 1: 写失败测试。** `test_project_cursor_pages_are_complete_and_state_all` 用两页列表并断言第二页 `after` 为第一页的 `cursor.after`，所有请求含 `state=All`、`limit=100`、兼容日期和 User-Agent。`test_project_detail_preserves_source_multiplier_limits_and_state` 覆盖唯一 C 海军、混合来源、缺省倍率和已结束项目。`test_own_contribution_returns_integer_and_optional_modified_time` 断言路径包含被授权角色 ID，缺少 `last_modified` 仍可用。`test_repeated_cursor_and_malformed_later_page_fail_without_partial_list` 断言有限请求后安全失败，不返回第一页结果。参数化 bool／负数／浮点／溢出／非法 UUID／无时区时间，均拒绝。
- [ ] **Step 2: 运行红灯。** `./.venv/Scripts/python.exe -m pytest tests/test_lp_esi.py -q`，预期缺少读取方法而失败。
- [ ] **Step 3: 实现接口与有限请求。** 新项目及角色资料使用根 API `https://esi.evetech.net` 的规范路径；钱包保持既有 latest URL。抽取现有三次重试、Retry-After 上限及错误转换，钱包行为保持兼容。分页从 `after=0` 开始，缺失游标或空页终止；记录已见游标，重复或超过 100 页安全失败。JSON 仍用精确 Decimal 解码。项目 403 不抛钱包整体授权失效；401 仍为授权错误；404 可供尾账逻辑处理。首期不发条件请求，异常 304 拒绝；相同缓存读数自然不产生新增。增加服务端限流、网络失败、非法 User-Agent 和错误脱敏测试。
- [ ] **Step 4: 运行绿灯。** `./.venv/Scripts/python.exe -m pytest tests/test_esi.py tests/test_lp_esi.py -q`，预期旧钱包及新项目测试全通过。
- [ ] **Step 5: 提交。** 检查暂存差异后提交 `feat: read corporation LP project contributions from ESI`。

## Task 4: 项目追踪和独立同步

**Files:** Create `app/lp_sync.py`、`tests/test_lp_sync.py`；Modify `app/sync.py`、`app/application.py`、`app/web.py:AppServices/callback/synchronize`、`tests/test_sync.py`、`tests/test_web.py`。

**Interfaces:** Consumes Tasks 1–3 的 repository、类型和 ESI 方法。Produces `validate_lp_project(project: LpProject) -> str | None`、`choose_lp_project(projects: tuple[LpProject, ...]) -> LpProject` 和 `LpSyncService(repository: LpRepository, esi: EsiClient, clock: Callable[[], datetime])`；其 `async sync_character(character_id: int, access_token: str, scopes: tuple[str, ...]) -> LpSyncResult` 不刷新令牌。选择失败使用安全 `LpProjectSelectionError(code)`。

`SyncResult` 末尾增加 `lp: LpSyncResult | None = None`，原字段仍表示钱包结果。`SyncService` 构造增加关键字参数 `lp: LpSyncService | None = None`，保留原位置参数；保留 `sync_character` 和 `sync_group` 签名，新增 `async save_authorization_and_sync(identity: CharacterIdentity, tokens: TokenSet, authorized_at: datetime) -> SyncResult`，与普通同步使用同一角色锁。

- [ ] **Step 1: 写失败测试。** `test_selects_only_unique_valid_active_named_project` 同时提供其他 LP、错误名称、历史同名和唯一正确活跃项目，断言选中正确 UUID；两项同级有效匹配拒绝。`test_renamed_binding_uses_original_id` 断言改名继续用原 ID。`test_closed_project_tail_precedes_new_project_baseline` 断言旧尾账记入一次，新项目首读无收入；404 时记录不可恢复缺口后仍可建立新基线。`test_incompatible_configuration_recovery_opens_new_segment` 断言暂停期间增长不入账。`test_project_and_personal_caps_warn_without_inventing_income` 断言达到上限或结束状态有提示。角色换军团不跨键相减，旧权限被拒时不阻断新来源。

为共享同步增加 `test_wallet_and_lp_commit_independently`，覆盖一方失败、缺项目 scope 和双方成功；`test_refresh_verifies_subject_and_saves_scope_once` 断言一次刷新、令牌身份相符、实际 scope 更新；`test_concurrent_sync_and_reauthorization_do_not_overwrite_fresh_token` 用 asyncio Event 控制交错，断言同一角色串行、最新凭证保留、差额只记一笔。

- [ ] **Step 2: 运行红灯。** `./.venv/Scripts/python.exe -m pytest tests/test_lp_sync.py tests/test_sync.py -q`，预期尚无 LP 服务／并发保护而失败。
- [ ] **Step 3: 实现选择、尾账和协调。** 项目名精确为 `LP对账-C海军`，仅唯一 C 海军来源和倍率 1 可用；列表完成后才选择。已绑定时先直接读取，不因列表缺失改绑。新来源只建立基线；每角色保存最终尾账是否已读取，不能用军团级绑定状态替代。配置不兼容后用 repository 的问题记录和新段规则恢复；普通超时不自动重建。每次有效贡献解析完成立即捕获 `clock()` 再持久化。

协调服务将刷新、旋转凭证写入和两个数据源读取放在每角色锁内；callback 的凭证保存改由 `save_authorization_and_sync` 在同一锁内执行，再调用不重复加锁的私有同步函数。刷新后调用 SSO 验证并检查角色 ID；更新已验证 scope，钱包与 LP 分别捕获并提交。LP 403、缺项目和业务异常不标记整个角色失效；真正 invalid_grant／令牌校验失败沿用授权恢复流程。捕获钱包网络失败后继续尝试 LP；统一刷新失败时不尝试任一数据源。更新 FakeSso／FakeSync 的契约，不关闭新验证来迁就旧 fixture。

服务装配构造共享 `EsiClient`、`LpRepository` 和 `LpSyncService`。`AppServices` 末尾增加可选 LP repository，不破坏现有位置构造；即使 ESI 未配置，也能查询已保存 LP 历史。同步 JSON 保留钱包字段，新增独立 `lp` 对象和白名单错误，callback 的同步提示分别反映两类结果。

- [ ] **Step 4: 运行绿灯。** `./.venv/Scripts/python.exe -m pytest tests/test_lp_sync.py tests/test_sync.py tests/test_web.py tests/test_desktop.py -q`，预期全通过；保留旧钱包失败隔离、轮换令牌和登录后初次同步行为。
- [ ] **Step 5: 提交。** 检查暂存差异后提交 `feat: synchronize LP income independently from wallet data`。

## Task 5: LP 报表和覆盖状态

**Files:** Create `app/lp_reporting.py`、`tests/test_lp_reporting.py`；Modify `app/web.py:dashboard/_public_character`、`tests/test_web.py`；原 `app/reporting.py` 的 ISK 算法保持原义。

**Interfaces:** Consumes `LpRepository.report_history`、现有 `DateRange` 和相同的角色筛选集合。Produces `build_lp_report(history: LpHistory, selected_ids: set[int], period: DateRange) -> LpReport`、`LpReport`／`LpReportRow`／`LpCoverage`，定义在 `app/lp_reporting.py`。每行有 `amounts: LpAmounts` 和 `coverage: LpCoverage`；report 有 `total`、`daily: dict[date, LpReportRow]`、`characters: dict[int, LpReportRow]`；coverage 有 `has_coverage`、`complete`、`started_at` 和 `warnings`。`serialize_lp(row: LpReportRow) -> dict` 放在 Web 层，格式固定：

```json
{"gross":"200000","deduction":"10000","net":"190000","has_coverage":true,"complete":true,"started_at":"2026-10-18T00:00:00+00:00","warnings":[]}
```

无覆盖时三项金额仍为字符串零、`has_coverage=false`，前端据此显示 `—`；不能用数值零推断覆盖。summary、每个 daily 和 character 行各增加 `lp`；`categories` 仍只有 ISK，C 海军 LP 来源从 summary 的 `lp` 单独显示。角色 API 只增加安全 LP 状态与时间，不公开令牌或整个 JWT。

- [ ] **Step 1: 写失败测试。** `test_lp_uses_observed_beijing_day_not_commit_or_modified_time` 使用 `2026-10-18T15:59:59Z` 和 `2026-10-18T16:00:00Z`，断言分别落在北京时间 18 日和 19 日；让数据库提交及 last_modified 时间不同仍不改归属。`test_missing_days_are_not_backfilled` 断言数日差额全在新同步日。`test_split_and_combined_sync_have_identical_net` 断言净值均为 `Decimal("19")`。

`test_before_baseline_is_unknown_and_after_baseline_zero_is_known` 断言历史起算前 `has_coverage is False`，有成功观察且无增长的当日为真且净值零；跨起算日的周期 `complete is False`。`test_known_history_survives_current_permission_loss` 断言收入 `Decimal("190000")` 保留且 `complete is False`；无法恢复的旧段问题出现在关联历史日期的 warnings。范围内的无活动日期在有覆盖时补零，尚未覆盖日期不声称零；沿用当前同步失败和永久缺口的区分，不对未知未来日期宣称完整。

Web 测试同一 `group_id`、选中／停用／跨组角色和 366 天限制，断言 LP 只取相同角色集合，原 ISK 三项金额及流水列表完全相同；金额 JSON 是字符串，无 scope／凭证泄漏。

- [ ] **Step 2: 运行红灯。** `./.venv/Scripts/python.exe -m pytest tests/test_lp_reporting.py tests/test_web.py -q`，预期缺少 LP 汇总或字段而失败。
- [ ] **Step 3: 实现聚合与序列化。** 只汇总保存的入账 amounts，不按当前 5% 重算。coverage 由基线段、成功观察、当前未解决问题和范围内不可恢复缺口计算；截至最后成功观察的范围才有覆盖依据。顶部周期和逐月账本分别调用同一计算函数，复用既有 Web 的验证后角色集合，不扩大查询权限。
- [ ] **Step 4: 运行绿灯。** `./.venv/Scripts/python.exe -m pytest tests/test_lp_reporting.py tests/test_reporting.py tests/test_web.py -q`，预期 LP 与 ISK 回归全通过。
- [ ] **Step 5: 提交。** 检查暂存差异后提交 `feat: expose LP totals and coverage in income reports`。

## Task 6: 接入界面和操作说明

**Files:** Modify `app/static/app.js`、`app/static/app.css`、`app/templates/index.html`、`README.md`、`docs/windows-release-checklist.md`、`tests/test_frontend.py`、`tests/test_frontend_periods.py`、`tests/test_docs.py`；Create `tests/test_lp_end_to_end.py`。

**Interfaces:** Consumes Tasks 4–5 的独立 LP JSON。Produces 前端 `lp(value: string, places = 2) -> string` 和 `lpReading(data: object) -> string`：十进制字符串分组并保留最多两位小数；未知返回 `— LP`，已确认零值返回 `0 LP`。LP 状态和警告只用 DOM `textContent`，不注入 HTML。

- [ ] **Step 1: 写行为测试。** 在现有 Node DOM harness 中验证 summary、来源、角色分项和月账本都显示相同的税后 LP；断言 `lp("190000") == "190,000"`、`lp("0.95") == "0.95"`，大于 JS 安全整数的字符串也不丢精度。未知读数显示 `— LP`，部分覆盖保留金额且显示“统计不完整”。同一角色钱包成功／LP 待授权时两种状态都可见，切换组或周期后迟到响应不覆盖新页面。调整旧的 LP 占位断言，保留 ISK、日期切换和存储设置交互测试。

模拟集成测试使用临时数据库、MockTransport 和已验证身份替身：两角色首次同步仅建基线，第二次各增 100 LP；断言组净 LP 为字符串 `"190"`，重复同步不变，数据库重启后仍为 `"190"`。随后让一个项目 403，断言该角色钱包仍成功、历史净 LP 保留且覆盖不完整。真实数据库和凭证不得进入 fixture。

- [ ] **Step 2: 运行红灯。** `./.venv/Scripts/python.exe -m pytest tests/test_frontend.py tests/test_frontend_periods.py tests/test_lp_end_to_end.py -q`，预期占位尚未替换或没有新显示行为而失败。
- [ ] **Step 3: 实现显示和说明。** 修改 UI 前阅读仓库 `.codex/skills/frontend-design/SKILL.md`，沿用现有结构、响应式和 reduced-motion 行为；不增加无关页面。按钮统一为“同步收益”，主 LP 标签为“C 海军 LP”，旁注“按同步日统计，扣除 5%”。显式展示起算时间、待授权、无项目和异常状态，保留已知数值。README 更新开发者添加 scope → 升级程序 → 每角色重新授权的顺序，清除“LP 尚未接入”和“只有钱包权限”等过期说法，说明无需清空数据库。相应更新文档契约测试的 scope 集合与按钮文案；历史设计文档不做批量改写。发行检查清单增加新旧库升级、钱包／LP 部分失败和真实项目核验步骤，但不写成已完成发布。
- [ ] **Step 4: 运行绿灯及最终验证。** 先运行上述三个文件及 `tests/test_docs.py`，再运行 `./.venv/Scripts/python.exe -m pytest -q`、`./.venv/Scripts/python.exe -m ruff check app scripts tests`、`./.venv/Scripts/python.exe scripts/smoke_app.py`、`git diff --check`。预期全部测试和 lint 通过，冒烟输出 `SMOKE_OK healthz=200 home=200`。确认 Node 行为测试实际运行；若 Node 不可用，报告未验证项，不能把跳过描述为通过。使用临时数据查看桌面或浏览器的 LP 金额、异常状态、窄屏布局和不同月份，不调用真实 EVE 数据。只在新增修改、失败或未决问题需要时重复测试。
- [ ] **Step 5: 提交并交付。** 检查只含本期文件后提交 `feat: display Caldari Navy LP income and sync status`；请求一次整分支代码审查并修复有依据的问题，再按 finishing-a-development-branch 技能交付。报告自动化结果、人工 UI 验证和未完成的真实 ESI 验证，未经授权不合并、推送或发布。

## 实现后的真实接入验收

维护者在 EVE Developers 为原应用启用 `esi-corporations.read_projects.v1`，需要统计的角色通过官方 SSO 重新授权。应用读取 `LP对账-C海军` 的真实 UUID 和仅 C 海军配置，首次仅建立基线；获取一笔已知 LP 后再次同步，核对差额及 95% 结果，再领取奖励复读确认贡献不降低。未执行这些步骤时，交付说明只能声称模拟验证通过，不能声称真实账号接入已验证。

## 执行交接

依赖顺序为 Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6。任务大量共用模型、服务和 Web 接口，推荐本会话由主代理逐项实现，再做一次独立的整分支审查；不在实现中并发修改相同文件。另一选项是每项任务分别交给实现代理和审查代理，增加独立检查但需要更多上下文和交接。

用户审阅本计划并选择执行方式后，按所选方式执行；书面设计的批准不替代本计划审阅。执行前确认工作区隔离、基线状态和依赖可用，再按各任务的红灯、实现、绿灯和提交顺序推进。
