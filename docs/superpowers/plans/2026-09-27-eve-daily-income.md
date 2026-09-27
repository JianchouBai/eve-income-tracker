# EVE 每日收入统计器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个在本机浏览器运行的多角色 EVE 收入仪表盘，从每个角色首次授权时刻起，按北京时间合并统计刷怪/任务收入及相关税费。

**Architecture:** FastAPI 单体应用负责 EVE SSO、ESI 同步、SQLite 持久化、JSON API 和服务端页面；浏览器端仅使用随项目提供的 HTML/CSS/JavaScript。领域分类、报表、存储、SSO、ESI 同步和 Web 路由通过明确接口分离，外部 HTTP、时钟和等待函数均可注入，以便测试不访问真实 EVE 服务。

**Tech Stack:** Python 3.11+、FastAPI 0.141.1、Uvicorn 0.54.0、HTTPX 0.28.1、Jinja2 3.1.6、PyJWT 2.15.0、cryptography 50.0.1、pydantic-settings 2.15.0、itsdangerous 2.2.0、SQLite、pytest 9.1.1、Ruff 0.16.9。

**Spec:** `docs/superpowers/specs/2026-09-27-eve-daily-income-design.md`

## Global Constraints

- Python 最低版本为 3.11；依赖版本使用上方已核对的固定版本。
- 服务默认只监听 `127.0.0.1:8000`，不实现公网或多人部署。
- ESI scope 只能是 `esi-wallet.read_character_wallet.v1`，兼容日期默认固定为 `2026-05-19`。
- 每个角色的 `tracking_started_at` 在首次成功授权时确定，重新授权和停用均不得重置。
- 只持久化 `date >= tracking_started_at` 的钱包流水；角色间唯一键为 `(character_id, journal_id)`。
- 默认合计全部启用角色，同时支持多选角色筛选；任何查询不得混合流水所属角色。
- 日归属固定使用 `Asia/Shanghai`，日期范围为首尾均包含且最多 366 天。
- 收入白名单固定为 `bounty_prizes`、`ess_escrow_transfer`、`agent_mission_reward`、`agent_mission_time_bonus_reward`。
- 税费白名单固定为三种军团税以及 `npc_bounty_security_tax`、`agent_mission_security_tax`；附加 `tax` 字段不重复汇总。
- ESI JSON 数字以 `Decimal` 解析，金额在 SQLite 中以规范化十进制 `TEXT` 保存；不得使用二进制浮点累加。
- Client Secret、授权码、访问/刷新令牌、Cookie 不得出现在页面、API 错误或日志中；刷新令牌必须经 Fernet 加密。
- 不使用 Node.js、外部数据库或 CDN；所有页面资源随仓库提供。
- 每项产品行为严格遵循 RED → GREEN → REFACTOR；每个任务完成后运行任务测试和完整测试套件。

## Review Focus

- ESI 返回高精度金额、科学计数法或非法数值时：合法值精确往返，非法值使整个角色批次回滚（Task 2、5 的测试）。
- ESI 时间缺少时区、恰好等于起点或位于北京时间跨日边界时：缺时区拒绝，等于起点保留，UTC 16:00 归次日（Task 2、5 的测试）。
- 多角色出现相同 `journal_id`、停用角色或未知筛选 ID 时：角色数据互不覆盖，默认合计排除停用角色，未知 ID 返回 400（Task 2、3、6 的测试）。
- 刷新令牌轮换、错误加密密钥或授权撤销时：保留统计起点与缓存流水，更新密文或仅标记需重新授权，错误不泄密（Task 3、4、5、6 的测试）。
- ESI 分页头异常、分页中途失败、420/429 或单角色失败时：限制页数、逐角色事务回滚、遵守有限退避，其他角色继续同步（Task 5 的测试）。

---

## File Map

- `app/config.py`：环境配置、公开配置问题诊断。
- `app/models.py`：领域数据类与 API 无关的值对象。
- `app/reporting.py`：收入/税费分类、北京时间范围和多角色汇总。
- `app/storage.py`：SQLite 初始化、角色仓库和钱包流水仓库。
- `app/security.py`：刷新令牌加密与常量时间校验辅助函数。
- `app/sso.py`：OAuth 元数据、授权 URL、令牌交换/刷新和 JWT 校验。
- `app/esi.py`：ESI 钱包分页、精确 JSON 解码、限流与重试。
- `app/sync.py`：逐角色及全部角色同步编排。
- `app/web.py`：FastAPI 工厂、会话/CSRF、页面与 JSON 路由。
- `app/main.py`：从环境创建默认应用的薄入口。
- `app/templates/index.html`：未授权页和仪表盘语义结构。
- `app/static/app.css`：响应式深色太空仪表盘样式。
- `app/static/app.js`：筛选、同步、列表和 Canvas 趋势图交互。
- `tests/`：按上述组件一一对应的 pytest 测试。
- `README.md`、`.env.example`：中文安装、EVE 应用注册和使用说明。

### Task 1: Project foundation and validated settings

**Files:**
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `app/__init__.py`
- Create: `app/config.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Consumes: process environment and optional `.env`.
- Produces: `Settings(BaseSettings)`, `Settings.configuration_issues() -> tuple[str, ...]`, `Settings.is_esi_ready -> bool`.

- [ ] **Step 1: Add pinned dependency and tooling manifests**

Pin the production packages listed in **Tech Stack** in `requirements.txt`, pin pytest/Ruff in `requirements-dev.txt`, make the dev file include the production file, and configure pytest plus Ruff (`target-version = 'py311'`) in `pyproject.toml`. Ignore `.env`, `.venv/`, `data/`, `*.sqlite3`, caches and coverage artifacts.

- [ ] **Step 2: Install the development environment**

Run: `python -m pip install -r requirements-dev.txt`
Expected: exit 0 with all pinned packages installed.

- [ ] **Step 3: Write the failing settings tests**

`tests/test_config.py` must prove:

```python
def test_configuration_issues_name_missing_fields_without_echoing_values():
    settings = Settings(
        _env_file=None,
        eve_client_id='',
        eve_client_secret='do-not-print',
        app_session_secret='short',
        app_encryption_key='invalid',
        eve_user_agent='eve-income-tracker/0.1 (your-email@example.com)',
    )
    issues = settings.configuration_issues()
    assert 'EVE_CLIENT_ID' in issues
    assert 'APP_SESSION_SECRET' in issues
    assert 'APP_ENCRYPTION_KEY' in issues
    assert 'EVE_USER_AGENT' in issues
    assert 'do-not-print' not in repr(issues)
```

Also assert defaults `http://localhost:8000/auth/callback`, `data/eve_income.sqlite3`, `2026-05-19`, and that a 32+ character session secret, valid Fernet key and contact-bearing User-Agent make `is_esi_ready` true.

- [ ] **Step 4: Run the tests to verify RED**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL because `app.config.Settings` does not exist.

- [ ] **Step 5: Implement minimal settings behavior**

Implement the exact interfaces above. Configuration diagnostics return only environment variable names, never values; a User-Agent containing `your-email@example.com` remains invalid. Resolve a relative `DATABASE_PATH` against the project root.

- [ ] **Step 6: Verify GREEN and lint**

Run: `python -m pytest tests/test_config.py -v`
Expected: all tests PASS.

Run: `python -m ruff check app tests`
Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
git add .gitignore pyproject.toml requirements.txt requirements-dev.txt app/__init__.py app/config.py tests/test_config.py
git commit -m 'build: scaffold income tracker configuration'
```

### Task 2: Domain classification, Beijing dates, and multi-character reports

**Files:**
- Create: `app/models.py`
- Create: `app/reporting.py`
- Create: `tests/test_reporting.py`

**Interfaces:**
- Consumes: `CharacterRecord` and `JournalEntry` value objects containing timezone-aware UTC timestamps and `Decimal` amounts.
- Produces: `classify_entry(entry: JournalEntry) -> ClassifiedEntry | None`, `parse_date_range(start: date, end: date) -> DateRange`, `build_dashboard(entries: Sequence[JournalEntry], characters: Sequence[CharacterRecord], selected_ids: set[int] | None, period: DateRange) -> DashboardReport`; `None` means all enabled characters.

- [ ] **Step 1: Write failing classification and time-boundary tests**

Create helpers that build real `JournalEntry` instances. Tests must assert all four income types require `amount > 0`, all five tax types require `amount < 0`, unknown/wrong-sign records return `None`, and `tax` metadata is not added to tax totals.

```python
def test_utc_1600_belongs_to_next_beijing_day():
    entry = journal(
        date_utc='2026-09-26T16:00:00Z',
        amount='100.10',
        ref_type='bounty_prizes',
    )
    report = build_dashboard(
        [entry],
        [enabled_character(1)],
        {1},
        period('2026-09-27', '2026-09-27'),
    )
    assert report.daily[0].date.isoformat() == '2026-09-27'
    assert report.daily[0].income == Decimal('100.10')
```

Add a test that rejects naive timestamps.

- [ ] **Step 2: Run the focused tests to verify RED**

Run: `python -m pytest tests/test_reporting.py -v`
Expected: FAIL because `app.models` and `app.reporting` do not exist.

- [ ] **Step 3: Implement domain value objects and classification**

Define frozen, slotted data classes for `CharacterRecord`, `JournalEntry`, `ClassifiedEntry`, `DateRange`, `DailyTotal`, `CategoryTotal`, `CharacterTotal`, and `DashboardReport`. Keep API serialization out of these types. Implement whitelist/sign classification with `Decimal` only.

- [ ] **Step 4: Verify classification GREEN**

Run: `python -m pytest tests/test_reporting.py -v`
Expected: classification and boundary tests PASS.

- [ ] **Step 5: Add failing aggregation and input-bound tests**

Assert exact Decimal totals for two enabled characters, a disabled character omitted from the default selection, an explicit subset, continuous zero-filled days, income/tax/net formulas, and category/character breakdowns. Assert an inclusive 366-day range succeeds, 367 days fails, and start-after-end fails.

- [ ] **Step 6: Implement report aggregation**

Implement `parse_date_range` and `build_dashboard`; convert period bounds to UTC once, filter by character before aggregation, group with `ZoneInfo('Asia/Shanghai')`, and return canonical `Decimal` values.

- [ ] **Step 7: Verify GREEN and commit**

Run: `python -m pytest tests/test_reporting.py -v`
Expected: all tests PASS.

Run: `python -m pytest -v`
Expected: full suite PASS.

```bash
git add app/models.py app/reporting.py tests/test_reporting.py
git commit -m 'feat: aggregate Beijing-time character income'
```

### Task 3: Exact SQLite persistence and encrypted refresh tokens

**Files:**
- Create: `app/storage.py`
- Create: `app/security.py`
- Create: `tests/test_storage.py`
- Create: `tests/test_security.py`

**Interfaces:**
- Consumes: `Settings.database_path`, `CharacterRecord`, `JournalEntry`, Fernet key.
- Produces: `Database.initialize() -> None`; `CharacterRepository.upsert_authorization(character_id: int, name: str, encrypted_refresh_token: str, authorized_at: datetime) -> CharacterRecord`; `CharacterRepository.list_all() -> list[CharacterRecord]`; `set_enabled`, `replace_refresh_token`, `mark_authorization_required`; `JournalRepository.commit_sync_batch(character_id: int, entries: Sequence[JournalEntry], completed_at: datetime) -> int`; `JournalRepository.list_for_report(character_ids: set[int], start_utc: datetime, end_utc_exclusive: datetime) -> list[JournalEntry]`; `TokenCipher.encrypt(str) -> str`; `TokenCipher.decrypt(str) -> str`.

- [ ] **Step 1: Write failing storage schema and character tests**

Using a temporary SQLite file, assert `initialize()` is idempotent, foreign keys are enabled, and `upsert_authorization` creates an enabled/authorized character. Re-authorizing the same ID must update name/token while preserving the first `tracking_started_at`; different characters remain distinct.

- [ ] **Step 2: Run storage tests to verify RED**

Run: `python -m pytest tests/test_storage.py -v`
Expected: FAIL because `app.storage` does not exist.

- [ ] **Step 3: Implement database and character repository**

Create schema version 1 with `characters` and `wallet_journal`; store all timestamps as canonical UTC ISO strings and all amounts as canonical decimal `TEXT`. Create indexes on `wallet_journal(character_id, date_utc)` and use parameterized SQL only.

- [ ] **Step 4: Add failing journal precision and idempotency tests**

Assert `Decimal('9007199254740993.01')` round-trips exactly, a repeated sync batch reports zero new rows, changed metadata upserts without duplicating, and the same journal ID under two character IDs yields two rows. Assert a raised exception leaves both journal rows and `last_synced_at` unchanged.

- [ ] **Step 5: Implement journal repository and verify GREEN**

Implement journal upsert and the character's `last_synced_at` update inside one explicit transaction; compute new count from existing composite keys before upsert. Reject a `JournalEntry.character_id` that differs from the batch character.

Run: `python -m pytest tests/test_storage.py -v`
Expected: all storage tests PASS.

- [ ] **Step 6: Write failing token cipher tests**

Assert Fernet encryption does not contain plaintext, decrypt round-trips, and a different key raises `InvalidToken` without including ciphertext or plaintext in its message.

- [ ] **Step 7: Implement `TokenCipher`, verify, and commit**

Run: `python -m pytest tests/test_security.py tests/test_storage.py -v`
Expected before implementation: security tests FAIL because `TokenCipher` is missing; after implementation: all PASS.

Run: `python -m pytest -v`
Expected: full suite PASS.

```bash
git add app/storage.py app/security.py tests/test_storage.py tests/test_security.py
git commit -m 'feat: persist encrypted multi-character wallet data'
```

### Task 4: EVE SSO protocol and JWT verification

**Files:**
- Create: `app/sso.py`
- Create: `tests/test_sso.py`

**Interfaces:**
- Consumes: `Settings`, injected `httpx.AsyncClient`, injected UTC clock.
- Produces: `SsoMetadata`, `TokenSet`, `CharacterIdentity`; `SsoClient.build_authorization_url(state: str) -> str`; `exchange_code(code: str) -> TokenSet`; `refresh(refresh_token: str) -> TokenSet`; `verify_access_token(access_token: str) -> CharacterIdentity`.

- [ ] **Step 1: Write failing authorization and token HTTP tests**

Using `httpx.MockTransport`, assert metadata comes from `https://login.eveonline.com/.well-known/oauth-authorization-server`, the authorization URL includes exact callback, state, response type and only `esi-wallet.read_character_wallet.v1`. Assert code exchange uses form data plus HTTP Basic auth and refresh uses `grant_type=refresh_token`; neither exception nor captured application log contains code, Client Secret or token.

- [ ] **Step 2: Run the tests to verify RED**

Run: `python -m pytest tests/test_sso.py -v`
Expected: FAIL because `app.sso.SsoClient` does not exist.

- [ ] **Step 3: Implement metadata discovery and OAuth requests**

Cache metadata and JWKS in memory for one hour, require HTTPS EVE endpoints, set explicit HTTP timeouts, and map non-2xx responses to sanitized `SsoError(code, user_message)`.

- [ ] **Step 4: Add failing JWT verification tests**

Generate an RSA test key/JWKS and signed tokens. Accept issuer `https://login.eveonline.com/` or `login.eveonline.com` only when `aud` contains both the configured Client ID and `EVE Online`, `scp` includes the wallet scope, `exp` is valid, and `sub` exactly matches `CHARACTER:EVE:<positive integer>`. Assert wrong signature, missing either audience, expired token, missing scope and malformed subject are rejected.

- [ ] **Step 5: Implement strict JWT verification**

Use the token header `kid` to select a JWKS key; allow only RS256. Let PyJWT verify signature, expiry and issuer, then separately require the complete audience set, wallet scope and subject format. Return role ID/name/scope through `CharacterIdentity`; never trust query parameters for identity.

- [ ] **Step 6: Verify SSO GREEN and commit**

Run: `python -m pytest tests/test_sso.py -v`
Expected: all tests PASS.

Run: `python -m pytest -v`
Expected: full suite PASS.

```bash
git add app/sso.py tests/test_sso.py
git commit -m 'feat: authenticate EVE characters securely'
```

### Task 5: ESI wallet client and transactional multi-character sync

**Files:**
- Create: `app/esi.py`
- Create: `app/sync.py`
- Create: `tests/test_esi.py`
- Create: `tests/test_sync.py`

**Interfaces:**
- Consumes: injected HTTP client/sleeper/jitter source, `SsoClient`, `TokenCipher`, repositories and enabled `CharacterRecord` rows.
- Produces: `EsiClient.fetch_wallet_journal(access_token: str, character_id: int) -> list[JournalEntry]`; `SyncService.sync_character(character_id: int, initial_access_token: str | None = None) -> SyncResult`; `SyncService.sync_enabled() -> SyncAllResult`.

- [ ] **Step 1: Write failing ESI parsing and pagination tests**

With `MockTransport`, return two pages containing amounts `9007199254740993.01` and `1e-2`. Assert exact `Decimal` values, aware UTC timestamps, `X-Compatibility-Date: 2026-05-19`, configured User-Agent and Bearer auth. Assert `X-Pages` missing means one page, while non-integer, below 1 or above 100 raises a sanitized `EsiDataError`.

- [ ] **Step 2: Run ESI tests to verify RED**

Run: `python -m pytest tests/test_esi.py -v`
Expected: FAIL because `app.esi.EsiClient` does not exist.

- [ ] **Step 3: Implement precise ESI decoding and pagination**

Decode response bytes using `json.loads(..., parse_float=Decimal, parse_int=int)`; validate each item before returning any page data. Required fields are positive integer ID, aware date, finite Decimal amount and ref type. Preserve supported optional ESI fields without classifying them here.

- [ ] **Step 4: Add failing retry and failure tests**

Inject a recording sleeper and deterministic jitter source. Assert 420/429 honors numeric or HTTP-date `Retry-After` capped at 60 seconds, with invalid values falling back to exponential delay; network errors and 5xx use at most three jittered attempts; 401/403 is not retried and maps to `EsiAuthorizationError`; a malformed later page raises without returning the earlier page.

- [ ] **Step 5: Implement bounded retries and verify ESI GREEN**

Run: `python -m pytest tests/test_esi.py -v`
Expected: all ESI tests PASS without real sleeping or network calls.

- [ ] **Step 6: Write failing per-character sync tests**

Use the real temporary repositories with fake SSO/ESI services. For a character whose `tracking_started_at` is `2026-09-27T10:00:00Z`, assert a `09:59:59Z` entry is discarded and a `10:00:00Z` entry is stored. Assert refresh-token rotation updates encrypted token but preserves the start; repeated sync returns zero new rows; unknown ref types are stored. A page/data failure must leave that role's existing journal and `last_synced_at` unchanged.

- [ ] **Step 7: Implement `sync_character`**

When no initial access token is supplied, decrypt and refresh first. If SSO rotates the refresh token, encrypt and persist it immediately while preserving `tracking_started_at`, even if the later ESI fetch fails. Fetch and validate every page, apply the exact UTC cutoff, then call `commit_sync_batch` so rows and `last_synced_at` change atomically. On 401/403 mark only that role as requiring reauthorization. Return sanitized `SyncResult(character_id, new_count, completed_at, status, error_code)`.

- [ ] **Step 8: Add failing sync-all isolation tests**

Create three enabled characters: one succeeds, one has revoked authorization, one succeeds. Assert calls are sequential, successful characters commit, the revoked role is marked, all three appear in `SyncAllResult`, and no failure contains token text. Assert disabled roles are not called.

- [ ] **Step 9: Implement `sync_enabled`, verify, and commit**

Run: `python -m pytest tests/test_esi.py tests/test_sync.py -v`
Expected before sync implementation: sync tests FAIL; after implementation: all PASS.

Run: `python -m pytest -v`
Expected: full suite PASS.

```bash
git add app/esi.py app/sync.py tests/test_esi.py tests/test_sync.py
git commit -m 'feat: synchronize ESI wallets transactionally'
```

### Task 6: FastAPI sessions, multi-character auth, and JSON API

**Files:**
- Create: `app/web.py`
- Create: `app/main.py`
- Create: `app/templates/index.html` (minimal server-rendered shell)
- Create: `tests/test_web.py`

**Interfaces:**
- Consumes: `Settings`, repositories, `SsoClient`, `SyncService`, reporting functions and injected clock through `AppServices`.
- Produces: `AppServices(characters, journal, cipher: TokenCipher | None, sso: SsoClient | None, sync: SyncService | None)`; `Clock.now_utc() -> datetime`; `create_app(settings: Settings, services: AppServices, clock: Clock) -> FastAPI`; routes from the spec plus `GET /healthz`.

- [ ] **Step 1: Write failing home, health, and login tests**

With `TestClient`, assert `/healthz` returns `{'status': 'ok'}`. With invalid configuration, `/` names missing environment variables without rendering any value or login URL. With valid configuration, `/auth/login` stores a high-entropy state in the signed HttpOnly/SameSite=Lax session and redirects to the exact SSO URL.

- [ ] **Step 2: Run Web tests to verify RED**

Run: `python -m pytest tests/test_web.py -v`
Expected: FAIL because `app.web.create_app` does not exist.

- [ ] **Step 3: Implement app factory and login entry**

Install `SessionMiddleware` with Secure false only for localhost HTTP. Store no secrets in the cookie: only OAuth state, viewer-authenticated flag and CSRF token. Initialize SQLite during application lifespan; mount template/static paths relative to the package, never current working directory. `app.main` always builds storage, but when `configuration_issues()` is non-empty it leaves cipher/SSO/sync unavailable, disables login/sync, and uses a process-random ephemeral session key so the configuration-help home page can still start safely.

- [ ] **Step 4: Add failing callback and multi-character tests**

Assert missing/mismatched state returns 400 without calling token exchange. A valid callback must derive identity only from the verified JWT, capture the injected current UTC time before auto-sync, encrypt the refresh token, set the viewer session, and redirect home. Repeating with a second character appends it; reauthorizing the first updates its token but preserves `tracking_started_at`. Auto-sync failure still preserves the role/start and redirects with a sanitized status.

- [ ] **Step 5: Implement callback and logout**

Use `secrets.compare_digest` for OAuth state and rotate state after one use. `POST /auth/logout` requires CSRF, clears only browser session state, and does not delete characters, tokens or journal rows.

- [ ] **Step 6: Add failing API authorization, validation, and CSRF tests**

Assert unauthenticated API requests return 401. After login, `GET /api/characters` returns public role fields only; dashboard defaults to enabled roles; repeated `character_id` selects an explicit subset including a disabled role. Unknown role ID, malformed date, start-after-end and 367 days return 400. JSON amounts are decimal strings. Every POST rejects missing/wrong CSRF with 403 and accepts the matching header.

- [ ] **Step 7: Implement read APIs and serialization**

Implement exact routes:

- `GET /api/characters`
- `GET /api/dashboard?from=...&to=...&character_id=...`
- `GET /api/entries?from=...&to=...&character_id=...`

Use Beijing dates, include end day, default to the latest 30 days, and serialize Decimal canonically as strings. Never return encrypted tokens.

- [ ] **Step 8: Add failing mutation and partial-sync tests**

Assert `POST /api/sync` returns HTTP 200 with per-role results even when one role fails, `POST /api/characters/{id}/enabled` changes default aggregation, unknown IDs return 404, and a revoked role is shown as needing reauthorization. Assert old dashboard data remains queryable after sync failure.

- [ ] **Step 9: Implement mutation APIs, exception mapping, and verify**

Accept `CharacterEnabledRequest(enabled: bool)` for the enable endpoint. Map domain/config/auth/rate/data errors to `{'error': {'code': str, 'message': str}}` with stable machine codes and Chinese messages. Do not include raw exception text from SSO/ESI in client responses.

Run: `python -m pytest tests/test_web.py -v`
Expected: all Web tests PASS.

Run: `python -m pytest -v`
Expected: full suite PASS.

```bash
git add app/web.py app/main.py app/templates/index.html tests/test_web.py
git commit -m 'feat: expose secure multi-character dashboard API'
```

### Task 7: Responsive browser dashboard

**Files:**
- Modify: `app/templates/index.html`
- Create: `app/static/app.css`
- Create: `app/static/app.js`
- Create: `tests/test_frontend.py`

**Interfaces:**
- Consumes: the Task 6 JSON API and CSRF token rendered as a non-secret page value.
- Produces: accessible login/configuration state and an authenticated dashboard with role/date filters, summary cards, Canvas trend, breakdowns, daily table and entry details.

- [ ] **Step 1: Write failing rendered-page and asset tests**

Assert the unauthenticated page explains read-only wallet scope, Beijing-day boundaries and no-history-before-authorization behavior. The authenticated shell must contain role filters, date inputs, sync/add-role/reauthorize/enable controls, three summary cards, labelled Canvas fallback content, type/role breakdown regions, daily table and entry detail region. Assert CSS/JS serve locally with correct content types and HTML contains no CDN or remote image/script/style URL.

- [ ] **Step 2: Run frontend tests to verify RED**

Run: `python -m pytest tests/test_frontend.py -v`
Expected: FAIL because the minimal template lacks the dashboard structure and static assets.

- [ ] **Step 3: Implement semantic HTML and responsive visual system**

Use an original dark navy/graphite interface with restrained cyan and amber data accents, strong numeric hierarchy, tabular numerals and subtle grid/noise made only with CSS. Support 360px mobile through desktop, visible keyboard focus, meaningful empty/loading/error states and `prefers-reduced-motion`. Do not copy EVE artwork.

- [ ] **Step 4: Implement dashboard interactions**

`app.js` must:

- default dates to the latest 30 Beijing calendar days;
- load characters, dashboard and entries with selected repeated `character_id` values;
- format visible ISK by grouping the decimal string itself; chart coordinates may use normalized numeric ratios but must never be reused as totals or sent back to the API;
- render a high-DPI responsive Canvas chart with text fallback;
- preserve the currently rendered report if sync fails and show the sanitized message;
- disable duplicate mutations, send the CSRF header, update enable state, and expose reauthorization/add-role links;
- render zero-data days and per-role sync status clearly.

- [ ] **Step 5: Verify page contract and manual resize behavior**

Run: `python -m pytest tests/test_frontend.py tests/test_web.py -v`
Expected: all tests PASS.

Start the app with test configuration and inspect at approximately 360px, 768px and 1440px widths; verify no horizontal page overflow, table/details remain usable, focus order follows visual order, and reduced motion disables decorative transitions.

- [ ] **Step 6: Run full suite and commit**

Run: `python -m pytest -v`
Expected: full suite PASS.

```bash
git add app/templates/index.html app/static/app.css app/static/app.js tests/test_frontend.py
git commit -m 'feat: build EVE income dashboard interface'
```

### Task 8: Setup documentation, smoke test, and release verification

**Files:**
- Create: `.env.example`
- Create: `README.md`
- Create: `scripts/smoke_app.py`
- Create: `tests/test_docs.py`

**Interfaces:**
- Consumes: final application entry point and documented environment variables.
- Produces: reproducible Windows-friendly setup, secret generation instructions, EVE application registration guide and an automated local HTTP smoke check.

- [ ] **Step 1: Write failing documentation contract tests**

Assert `.env.example` contains every setting from the spec, exact callback and compatibility date, but no usable secret. Assert README documents Python 3.11+, venv/install/start commands, EVE Developers Portal registration, only the wallet scope, repeated “添加角色” authorization, combined role statistics, per-role first-authorization cutoff, Beijing dates, manual sync, reauthorization and local-only security.

- [ ] **Step 2: Run docs tests to verify RED**

Run: `python -m pytest tests/test_docs.py -v`
Expected: FAIL because README and example environment do not exist.

- [ ] **Step 3: Write README and environment template**

Include PowerShell commands to generate `APP_SESSION_SECRET` and a Fernet `APP_ENCRYPTION_KEY`, create `.venv`, install pinned requirements, and run `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`. State that each EVE character must be authorized separately and no pre-authorization wallet history is imported.

- [ ] **Step 4: Write and run the local smoke script**

`scripts/smoke_app.py` must create a temporary database, start Uvicorn on `127.0.0.1:8765`, poll with a deadline, assert `/healthz` JSON and `/` HTML, then terminate the exact child process in `finally`.

Run: `python scripts/smoke_app.py`
Expected: `SMOKE_OK healthz=200 home=200` and exit 0.

- [ ] **Step 5: Run fresh release verification**

Run: `python -m ruff format --check app tests scripts`
Expected: exit 0.

Run: `python -m ruff check app tests scripts`
Expected: exit 0.

Run: `python -m pytest -v`
Expected: full suite PASS with zero failures/warnings.

Run: `rg -n '(client_secret|access_token|refresh_token|authorization_code).{0,20}(eyJ|[A-Za-z0-9_-]{30,})' . -g '!docs/**' -g '!.git/**'`
Expected: no committed secret-like values; identifiers and test fixtures may appear only as clearly synthetic values.

- [ ] **Step 6: Commit documentation and verification assets**

```bash
git add .env.example README.md scripts/smoke_app.py tests/test_docs.py
git commit -m 'docs: add local EVE tracker setup and smoke test'
```

- [ ] **Step 7: Request whole-branch review and finish**

Invoke `superpowers:requesting-code-review`, address findings with `superpowers:receiving-code-review`, rerun Step 5 plus the smoke test, then invoke `superpowers:finishing-a-development-branch`. Do not claim real EVE login success without the user's own Client ID/Secret and manual SSO acceptance.
