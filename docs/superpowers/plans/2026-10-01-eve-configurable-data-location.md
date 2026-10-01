# EVE Income Tracker v1.0.0 可配置数据目录实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 发布 v1.0.0，让安装版用户在首次启动和设置页安全选择、查看及迁移完整应用数据目录，同时保证失败回滚、升级保留和卸载不误删。

**Architecture:** 使用 HKCU 注册表保存当前数据根及待处理迁移状态，数据根内用随机 ID 安全标记证明目录所有权。桌面启动器在日志、数据库和 WebView 打开前运行位置控制器及事务式迁移；Web 设置页只负责展示、估算和安排任务，真正迁移在持有单实例锁的桌面重启阶段完成。

**Tech Stack:** Python 3.11+、FastAPI、SQLite、PyWebView、Windows `winreg`/Win32 API、PyInstaller、Inno Setup、pytest、Ruff

**Spec:** `docs/superpowers/specs/2026-09-30-eve-configurable-data-location-design.md`

## Global Constraints

- 正式版本统一为 `1.0.0`；界面可显示 `V1.0`，“关于”信息显示 `V1.0.0`。
- 数据库、DPAPI 密钥、日志、备份和本应用 WebView 用户数据必须共享同一个可配置数据根。
- 数据根不可用时必须失败关闭；禁止回退到 `%LOCALAPPDATA%` 或自动创建第二套数据库。
- 开发源码模式和打包烟雾测试不得读写真实用户的数据位置注册表。
- 跨盘迁移只在复制、SQLite、密钥、标记和目标重新打开全部验证后提交；提交前不得修改关键原数据。
- 目标非空时禁止覆盖或合并；已有数据根只能通过“定位已有数据”重新连接。
- 删除必须同时匹配规范化路径、安全标记和随机数据根 ID，且拒绝磁盘根、用户根、系统目录、安装目录和意外重解析点。
- 静默卸载与默认交互式卸载必须保留个人数据和位置注册表。
- 数据库、密钥、日志、恢复副本、WebView 数据、构建输出和安装包不得进入 Git。
- 所有新增行为遵循 TDD；每项实现提交前运行对应测试和 Ruff。

## Review Focus

- 已配置盘符离线、路径改名或权限丢失时，只能重试、定位有效数据根或退出，绝不能创建空库；Task 5、Task 6 固化测试。
- 大小写、尾部分隔符、`..`、磁盘根、系统目录、符号链接和 Windows 重解析点必须规范化或拒绝；Task 1、Task 4 固化测试。
- 复制后、注册表提交时及提交后目标重新打开失败时，当前指针和原数据必须保持或恢复；Task 4 固化故障注入测试。
- 空间不足、目标非空、恢复副本失败和原目录被占用应产生可恢复结果，不能半迁移或误删；Task 3、Task 4 固化测试。
- 卸载器遇到缺失、陈旧或伪造的注册表/标记时必须保留数据；Task 8 固化安装脚本测试。

## File Structure

- `app/data_roots.py`：数据根引用、安全标记、路径验证与受控删除。
- `app/data_location_store.py`：HKCU 注册表协议和可替换后端。
- `app/migration_recovery.py`：短期压缩恢复副本及保留计数。
- `app/data_migration.py`：迁移估算、暂存复制、校验、提交、回滚和延迟清理。
- `app/data_location_controller.py`：首次启动、重新连接、设置页状态和待处理任务编排。
- `app/paths.py`：从已选数据根生成 `AppPaths`，保留开发和烟雾隔离行为。
- `app/windows_runtime.py`：目录选择、三选一启动处理和原生迁移进度窗口。
- `app/desktop_control.py`：无循环依赖的桌面内部重启信号。
- `app/desktop.py`：启动前位置解析、内部安全重启和生命周期顺序。
- `app/desktop_bridge.py`：受限目录选择、打开当前数据目录和请求内部重启。
- `app/web.py`、`app/templates/index.html`、`app/static/app.js`、`app/static/app.css`：桌面数据设置 API 与 UI。
- `packaging/eve-income-tracker.iss`：读取注册表后的安全卸载行为。
- `README.md`、`CHANGELOG.md`、`docs/windows-release-checklist.md`：v1.0.0 使用与发布说明。

---

### Task 1: 数据根、路径解析与安全标记

**Files:**
- Create: `app/data_roots.py`
- Modify: `app/paths.py`
- Modify: `tests/test_paths.py`
- Create: `tests/test_data_roots.py`

**Interfaces:**
- Produces: `DataRootReference(root: Path, root_id: str, schema: int)`、`DataRootMarker(reference, created_at, state)`、`DataRootValidationError(code)`。
- Produces: `data_root_from_parent(parent: Path) -> Path`、`write_data_root_marker(root, marker) -> None`、`read_data_root_marker(root) -> DataRootMarker`、`validate_existing_data_root(reference, *, resource_root, require_writable=True) -> AppPaths`、`validate_new_data_root(parent, *, source_root, install_root) -> Path`、`remove_owned_data_root(reference) -> None`。
- Changes: `AppPaths` 新增 `data_root: Path`；`resolve_app_paths(..., data_root: Path | None = None)` 在安装模式优先使用显式数据根，开发模式保持项目根。

- [ ] **Step 1: Write failing path and marker tests**

  覆盖父目录自动追加 `EVE Income Tracker`、规范化绝对路径、canonical UTF-8 JSON 标记、随机 ID/状态匹配、数据根与 `AppPaths` 全部子路径，以及旧调用兼容。

- [ ] **Step 2: Write failing destructive-boundary tests**

  参数化验证磁盘根、用户根、Windows/Program Files、安装目录、源目录本身、`..` 逃逸、非空目标、符号链接或重解析点均被拒绝；错误 ID 的标记不能删除任何文件。

- [ ] **Step 3: Run the focused tests and verify failure**

  Run: `py -3.12 -m pytest tests/test_paths.py tests/test_data_roots.py -v`
  Expected: FAIL because the new module, `data_root` field and validation functions do not exist.

- [ ] **Step 4: Implement the minimal data-root and path APIs**

  标记固定使用 `.eve-income-tracker-data.json`，应用 ID 固定为 Inno `AppId` `59D95F12-7D3B-4C8A-A57D-98F836769441`，模式版本为 `1`，状态仅允许 `incomplete` 或 `ready`；写入采用同目录临时文件加 `os.replace`。

- [ ] **Step 5: Run the focused tests**

  Run: `py -3.12 -m pytest tests/test_paths.py tests/test_data_roots.py -v`
  Expected: PASS.

- [ ] **Step 6: Run focused lint**

  Run: `py -3.12 -m ruff check app/data_roots.py app/paths.py tests/test_data_roots.py tests/test_paths.py`
  Expected: `All checks passed!`

- [ ] **Step 7: Commit**

  `git commit -m "feat: add owned data root boundaries"`

### Task 2: 当前用户注册表位置协议

**Files:**
- Create: `app/data_location_store.py`
- Create: `tests/test_data_location_store.py`

**Interfaces:**
- Consumes: `DataRootReference` from Task 1.
- Produces: `PendingMigration(target_parent: Path, create_recovery: bool)`、`PendingCleanup(reference: DataRootReference)`、`DataLocationStoreError(code)`。
- Produces: `DataLocationStore` protocol with `read_current()`, `begin_current_switch(reference)`, `finalize_current_switch()`, `rollback_current_switch()`, `read/write/clear_pending_migration()` and `read/write/clear_pending_cleanup()`.
- Produces: `WindowsDataLocationStore(backend: RegistryBackend | None = None)` and `MemoryDataLocationStore` for smoke/tests.

- [ ] **Step 1: Write failing registry round-trip and corruption tests**

  用内存 `RegistryBackend` 验证路径、`DataRootId`、schema、恢复副本选择、待清理路径/ID；完全缺失返回 `None`，部分值、错误类型、NUL、相对路径或未知 schema 抛出固定 `registry_invalid`，错误不得回显值。

- [ ] **Step 2: Write failing commit-order tests**

  断言 `begin_current_switch` 先保存完整 `PreviousDataRoot`/ID/schema，再写目标 schema 和 ID，最后写 `DataRoot`；目标验证后 `finalize_current_switch` 才清除 previous 值。模拟每个写入点崩溃时，`read_current` 必须恢复 previous 引用；`rollback_current_switch` 恢复旧值且幂等。

- [ ] **Step 3: Run tests to verify failure**

  Run: `py -3.12 -m pytest tests/test_data_location_store.py -v`
  Expected: FAIL because `app.data_location_store` does not exist.

- [ ] **Step 4: Implement the registry adapter and stores**

  注册表键固定为 `HKCU\Software\JianchouBai\EVE Income Tracker`；生产后端仅在 Windows 导入 `winreg`，内存后端不访问系统注册表。

- [ ] **Step 5: Run the focused tests**

  Run: `py -3.12 -m pytest tests/test_data_location_store.py -v`
  Expected: PASS.

- [ ] **Step 6: Run focused lint**

  Run: `py -3.12 -m ruff check app/data_location_store.py tests/test_data_location_store.py`
  Expected: `All checks passed!`

- [ ] **Step 7: Commit**

  `git commit -m "feat: persist per-user data location"`

### Task 3: 短期迁移恢复副本

**Files:**
- Create: `app/migration_recovery.py`
- Create: `tests/test_migration_recovery.py`

**Interfaces:**
- Consumes: source and destination `AppPaths` from Task 1.
- Produces: `RecoverySnapshotInfo(archive_path, created_at, size_bytes, successful_starts)` and `RecoverySnapshotError(code)`.
- Produces: `RecoverySnapshotService.create(source, destination) -> RecoverySnapshotInfo`、`inspect(paths) -> RecoverySnapshotInfo | None`、`record_success_and_prune(paths) -> RecoverySnapshotInfo | None`、`delete(paths) -> bool`.

- [ ] **Step 1: Write failing archive-content and atomicity tests**

  断言 ZIP 只含一致的 `data/eve_income.sqlite3`、`config/local.key` 和恢复清单，不含日志、WebView 或普通备份；新归档验证成功前不能删除旧归档，最终最多一份。

- [ ] **Step 2: Write failing retention tests**

  使用注入时钟断言少于三次成功启动或不足七天均保留；同时满足两项才自动删除；设置页手动删除幂等并报告实际字节数。

- [ ] **Step 3: Run tests to verify failure**

  Run: `py -3.12 -m pytest tests/test_migration_recovery.py -v`
  Expected: FAIL because the recovery service does not exist.

- [ ] **Step 4: Implement compressed snapshot and metadata**

  数据库使用 SQLite backup API 创建一致快照，归档和元数据先写临时文件再原子替换；元数据记录 UTC 创建时间和成功启动计数。

- [ ] **Step 5: Run the focused tests**

  Run: `py -3.12 -m pytest tests/test_migration_recovery.py -v`
  Expected: PASS.

- [ ] **Step 6: Run focused lint**

  Run: `py -3.12 -m ruff check app/migration_recovery.py tests/test_migration_recovery.py`
  Expected: `All checks passed!`

- [ ] **Step 7: Commit**

  `git commit -m "feat: add bounded migration recovery snapshot"`

### Task 4: 事务式数据迁移引擎

**Files:**
- Create: `app/data_migration.py`
- Create: `tests/test_data_migration.py`

**Interfaces:**
- Consumes: Task 1 validation/removal, Task 2 `DataLocationStore`, Task 3 `RecoverySnapshotService`.
- Produces: `MigrationEstimate(target_root, copy_bytes, recovery_bytes, free_bytes)`、`MigrationResult(reference, original_removed, pending_cleanup)`、`DataMigrationError(code)`.
- Produces: `DataMigrationService.estimate(source, target_parent, *, create_recovery) -> MigrationEstimate`、`migrate(source, target_parent, *, create_recovery) -> MigrationResult`、`retry_pending_cleanup() -> bool`.
- Constructor injects `destination_validator: Callable[[AppPaths], None]` so desktop code validates DPAPI key, supported SQLite version and reopenability before final cleanup.

- [ ] **Step 1: Write failing estimate and successful C-to-D/D-to-E tests**

  使用临时“卷”目录证明算法不写死盘符；断言估算分别报告完整复制、恢复副本和空闲空间，正式目标使用同卷临时目录重命名，复制日志/备份/WebView，数据库使用一致快照，旧恢复归档不被重复搬运。

- [ ] **Step 2: Write failing pre-commit fault matrix**

  参数化注入空间不足、复制、哈希、SQLite integrity、密钥、标记、恢复副本、重命名和注册表写入故障；每种情况都断言原引用仍有效、原关键数据字节未变、没有 ready 目标且没有误删。

- [ ] **Step 3: Write failing post-commit rollback and cleanup tests**

  目标重新打开失败必须恢复原注册表引用并隔离失败目标；原目录占用只记录 `PendingCleanup(root, root_id)`，新位置继续有效；后续重试只有 ID 与标记都匹配才删除。

- [ ] **Step 4: Write failing target and reparse-point tests**

  非空目标、已有数据根、同一路径、危险父目录和源树中的意外重解析点均在复制前拒绝。

- [ ] **Step 5: Run tests to verify failure**

  Run: `py -3.12 -m pytest tests/test_data_migration.py -v`
  Expected: FAIL because the migration engine does not exist.

- [ ] **Step 6: Implement staged copy, validation, commit and rollback**

  临时目录命名为 `.eve-income-tracker-migrating-<uuid>`；先写 `incomplete` 标记，完成文件与数据库校验后写 `ready`，重命名后调用 `begin_current_switch`。目标重新打开成功后调用 `finalize_current_switch`；失败时调用 `rollback_current_switch`，再保留失败目标用于诊断。

- [ ] **Step 7: Run the focused tests**

  Run: `py -3.12 -m pytest tests/test_data_migration.py tests/test_migration_recovery.py -v`
  Expected: PASS.

- [ ] **Step 8: Run focused lint**

  Run: `py -3.12 -m ruff check app/data_migration.py app/migration_recovery.py tests/test_data_migration.py tests/test_migration_recovery.py`
  Expected: `All checks passed!`

- [ ] **Step 9: Commit**

  `git commit -m "feat: migrate application data transactionally"`

### Task 5: 首次启动控制器与原生目录交互

**Files:**
- Create: `app/data_location_controller.py`
- Modify: `app/windows_runtime.py`
- Create: `tests/test_data_location_controller.py`
- Modify: `tests/test_windows_runtime.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: `DataLocationStatus(current_path, volume, usage, recovery)` and `StartupInteraction` protocol.
- Produces: `DataLocationController.resolve_startup(legacy_root, interaction) -> DataRootReference`、`execute_pending_migration() -> MigrationResult | None`、`reconnect_existing(root) -> DataRootReference`、`estimate(...)`、`schedule_migration(...)`、`status()`、`delete_recovery()`、`record_success()`.
- Produces native `choose_data_parent()`, `choose_existing_data_root()`, `ask_retry_locate_exit()` and `MigrationProgress` (`show(stage)`, `close()`) abstractions with injectable dialog adapters.

- [ ] **Step 1: Write failing fresh-install and legacy-discovery tests**

  覆盖新安装选择默认/自定义父目录、取消不产生半目录、精确旧 `%LOCALAPPDATA%` 布局补写标记、保留原位、选择迁移，以及旧布局无数据库时不误识别。

- [ ] **Step 2: Write failing unavailable-location tests**

  当前盘离线或标记无效时，分别测试重试、定位带有效标记的已有根、取消退出；所有分支断言未调用数据库创建。定位空目录或错误 ID 必须拒绝。

- [ ] **Step 3: Write failing Win32 adapter tests**

  验证目录选择取消/错误、固定标题、三选一映射和进度阶段；非 Windows 或 API 失败只返回稳定错误码，不泄露路径外内容。

- [ ] **Step 4: Run tests to verify failure**

  Run: `py -3.12 -m pytest tests/test_data_location_controller.py tests/test_windows_runtime.py -v`
  Expected: FAIL for missing controller and directory-dialog APIs.

- [ ] **Step 5: Implement the controller and native adapters**

  旧版默认目录是唯一允许在迁移前补写安全标记的无标记源；设置页选择始终是父目录，重新定位选择的是带标记的数据根本身。

- [ ] **Step 6: Run the focused tests**

  Run: `py -3.12 -m pytest tests/test_data_location_controller.py tests/test_windows_runtime.py -v`
  Expected: PASS.

- [ ] **Step 7: Run focused lint**

  Run: `py -3.12 -m ruff check app/data_location_controller.py app/windows_runtime.py tests/test_data_location_controller.py tests/test_windows_runtime.py`
  Expected: `All checks passed!`

- [ ] **Step 8: Commit**

  `git commit -m "feat: guide startup data location selection"`

### Task 6: 桌面启动顺序与内部安全重启

**Files:**
- Create: `app/desktop_control.py`
- Modify: `app/desktop.py`
- Modify: `app/web.py`
- Modify: `scripts/smoke_desktop.py`
- Modify: `tests/test_desktop.py`
- Modify: `tests/test_web.py`
- Modify: `tests/test_packaging.py`

**Interfaces:**
- Consumes: `DataLocationController` and native progress from Task 5.
- Produces in `app/desktop_control.py`: `DesktopControl.request_restart()`/`consume_restart()` shared by the bridge and desktop loop without importing `app.desktop` from `app.web`.
- Changes: `RuntimeContext` gains `data_location: DataLocationController | None` and `desktop_control: DesktopControl | None`.

- [ ] **Step 1: Rewrite bootstrap-order tests to fail**

  断言顺序为互斥量 → 位置解析/待迁移/待清理 → 日志 → WebView 检查 → 可选旧库导入 → 密钥/数据库 → 服务 → 窗口；迁移阶段数据库和 WebView 尚未打开。

- [ ] **Step 2: Write failing internal-restart tests**

  设置页请求重启后，同一进程继续持有互斥量，依次关闭窗口/服务器/日志，执行迁移并用新路径重建一次会话；普通关窗不重启，失败只显示固定中文错误。

- [ ] **Step 3: Write failing no-fallback and smoke-isolation tests**

  注册表路径不可用时不调用 `Database`；烟雾模式只使用临时 `MemoryDataLocationStore` 和 `--local-app-data`，不读取 HKCU；迁移恢复副本只在一次真实进程启动成功后增加计数。

- [ ] **Step 4: Update first-run import contract tests**

  把提示改为“导入旧版数据库（可选）”，保留跳过创建新库；只有数据根已经确定且目标数据库不存在时才调用文件选择器。

- [ ] **Step 5: Run tests to verify failure**

  Run: `py -3.12 -m pytest tests/test_desktop.py tests/test_web.py tests/test_packaging.py -v`
  Expected: FAIL against the old fixed-LocalAppData bootstrap.

- [ ] **Step 6: Refactor `DesktopApplication.run` into a mutex-owned session loop**

  路径前错误使用原生对话框；路径后错误同时记录脱敏日志。内部重启必须复用进程，不启动第二个 EXE，也不释放互斥量。

- [ ] **Step 7: Run the focused tests**

  Run: `py -3.12 -m pytest tests/test_desktop.py tests/test_web.py tests/test_packaging.py -v`
  Expected: PASS.

- [ ] **Step 8: Run focused lint**

  Run: `py -3.12 -m ruff check app/desktop_control.py app/desktop.py app/web.py scripts/smoke_desktop.py tests/test_desktop.py tests/test_web.py tests/test_packaging.py`
  Expected: `All checks passed!`

- [ ] **Step 9: Commit**

  `git commit -m "feat: resolve data location before desktop startup"`

### Task 7: 桌面设置 API、桥和界面

**Files:**
- Modify: `app/desktop_bridge.py`
- Modify: `app/web.py`
- Modify: `app/templates/index.html`
- Modify: `app/static/app.js`
- Modify: `app/static/app.css`
- Modify: `tests/test_desktop_bridge.py`
- Modify: `tests/test_web.py`
- Modify: `tests/test_frontend.py`

**Interfaces:**
- Consumes: `RuntimeContext.data_location`, `DesktopControl`, `MigrationEstimate`.
- Produces desktop-only endpoints: `GET /api/desktop/data-location`、`POST /api/desktop/data-location/estimate`、`POST /api/desktop/data-location/migrations`、`DELETE /api/desktop/data-location/recovery`.
- Extends `DesktopBridge` with argument-free `choose_data_parent()`, `open_data_directory()` and `restart_for_migration()`; only the picker returns a selected parent string, while open/restart use injected current state and never accept arbitrary paths or commands.

- [ ] **Step 1: Write failing API authentication and payload tests**

  GET requires authenticated desktop session；POST/DELETE additionally require matching CSRF。development mode returns 404。测试正常状态/占用、路径估算、空间不足、安排任务和删除恢复副本的固定 JSON；API 不返回密钥、注册表细节或文件内容。

- [ ] **Step 2: Write failing bridge allowlist tests**

  目录选择取消安全返回；打开目录只调用当前已验证数据根；只有确有 pending migration 时允许内部重启；现有 EVE 授权 URL 白名单测试继续通过。

- [ ] **Step 3: Write failing frontend contract tests**

  页面仅在 desktop mode 显示“设置”；设置面板显示路径、卷、数据库/日志/备份/WebView/总占用及恢复副本。选择父目录后先请求估算，再显示目标、迁移量、恢复量、可用空间和原目录清理说明，确认后安排并重启。

- [ ] **Step 4: Run tests to verify failure**

  Run: `py -3.12 -m pytest tests/test_desktop_bridge.py tests/test_web.py tests/test_frontend.py -v`
  Expected: FAIL because settings APIs, bridge methods and controls do not exist.

- [ ] **Step 5: Implement APIs and accessible settings UI**

  所有写操作复用现有 `request()` CSRF 逻辑；按钮在进行中禁用，错误保持可见，键盘焦点在打开/关闭设置面板时正确恢复。模板从 `APP_VERSION` 动态显示 `V1.0` 和完整关于版本。

- [ ] **Step 6: Run the focused tests**

  Run: `py -3.12 -m pytest tests/test_desktop_bridge.py tests/test_web.py tests/test_frontend.py -v`
  Expected: PASS.

- [ ] **Step 7: Run focused lint**

  Run: `py -3.12 -m ruff check app/desktop_bridge.py app/web.py tests/test_desktop_bridge.py tests/test_web.py tests/test_frontend.py`
  Expected: `All checks passed!`

- [ ] **Step 8: Commit**

  `git commit -m "feat: add desktop data storage settings"`

### Task 8: 安装升级与安全卸载

**Files:**
- Modify: `packaging/eve-income-tracker.iss`
- Modify: `tests/test_windows_build.py`
- Modify: `tests/test_packaging.py`

**Interfaces:**
- Consumes: registry key/value names and canonical marker format from Tasks 1–2.
- Produces: Inno helper functions that resolve current data root, compare `DataRootId`, reject dangerous paths and delete the location registry only after verified data deletion.

- [ ] **Step 1: Write failing installer static-contract tests**

  断言安装器读取 HKCU `DataRoot`/`DataRootId`/schema，默认与静默卸载保留数据和注册表，删除提示显示规范化完整路径，成功删除后才删注册表。

- [ ] **Step 2: Write failing malicious/stale-state tests**

  用脚本契约覆盖缺失标记、ID 不匹配、未知 schema、半完成 current/previous 切换、磁盘根/系统目录/用户根/安装目录和损坏注册表时拒绝 `DelTree`；没有新注册表时仅允许现有精确 `%LOCALAPPDATA%\EVE Income Tracker` 旧版兼容路径。

- [ ] **Step 3: Run tests to verify failure**

  Run: `py -3.12 -m pytest tests/test_windows_build.py tests/test_packaging.py -v`
  Expected: FAIL because the installer still assumes one LocalAppData directory.

- [ ] **Step 4: Implement registry-aware uninstall logic**

  marker 由 Python 端 canonical JSON 序列化，Inno 只接受固定应用 ID、schema 和与注册表完全一致的 root ID；删除失败保留注册表并显示遗留路径。覆盖升级不得写默认 `DataRoot`。

- [ ] **Step 5: Run installer contract tests**

  Run: `py -3.12 -m pytest tests/test_windows_build.py tests/test_packaging.py -v`
  Expected: PASS.

- [ ] **Step 6: Run focused lint**

  Run: `py -3.12 -m ruff check tests/test_windows_build.py tests/test_packaging.py`
  Expected: `All checks passed!`

- [ ] **Step 7: Commit**

  `git commit -m "fix: preserve and safely remove custom data roots"`

### Task 9: v1.0.0 版本、文档与发布契约

**Files:**
- Modify: `app/version.py`
- Modify: `app/desktop.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/windows-release-checklist.md`
- Modify: `tests/test_version.py`
- Modify: `tests/test_docs.py`
- Modify: `tests/test_frontend.py`

**Interfaces:**
- Consumes: completed behavior from Tasks 1–8.
- Produces: single version `1.0.0`, UI shorthand `V1.0`, full about version `V1.0.0`, installer filename `EVE-Income-Tracker-1.0.0-x64-Setup.exe`.

- [ ] **Step 1: Write failing version and documentation tests**

  断言 `APP_VERSION == "1.0.0"`，桌面 smoke User-Agent 从同一常量生成，README 不再声称数据固定在 LocalAppData，且明确首次选择、设置迁移、离线盘行为、恢复副本、重装/卸载和 Git 边界。

- [ ] **Step 2: Run tests to verify failure**

  Run: `py -3.12 -m pytest tests/test_version.py tests/test_docs.py tests/test_frontend.py -v`
  Expected: FAIL while the repository still reports `0.1.0` and old fixed paths.

- [ ] **Step 3: Update the version and user documentation**

  将 CHANGELOG 的计划项转为 `1.0.0（2026-10-01）` 已实现内容；保留 `0.1.0` 历史。发布清单加入 C→D、D→测试卷、盘离线、默认保留、明确删除和重装接回验收。

- [ ] **Step 4: Run the focused tests**

  Run: `py -3.12 -m pytest tests/test_version.py tests/test_docs.py tests/test_frontend.py -v`
  Expected: PASS.

- [ ] **Step 5: Run focused lint**

  Run: `py -3.12 -m ruff check app/desktop.py app/version.py tests/test_version.py tests/test_docs.py tests/test_frontend.py`
  Expected: `All checks passed!`

- [ ] **Step 6: Commit**

  `git commit -m "docs: prepare configurable storage release v1.0.0"`

### Task 10: 全量回归、打包与交付检查点

**Files:**
- Modify only if verification exposes a defect; return to the owning task's tests before fixing.
- Generated/ignored: `build/`, `dist/`, `installer-output/EVE-Income-Tracker-1.0.0-x64-Setup.exe`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: verified v1.0.0 release candidate, absolute installer path and SHA-256; does not push, tag or create a GitHub Release without explicit user approval.

- [ ] **Step 1: Run the complete automated test suite**

  Run: `py -3.12 -m pytest -v`
  Expected: all tests PASS.

- [ ] **Step 2: Run complete lint**

  Run: `py -3.12 -m ruff check app scripts tests`
  Expected: `All checks passed!`

- [ ] **Step 3: Check the Git diff**

  Run: `git diff --check`
  Expected: no output.

- [ ] **Step 4: Run the source web smoke test**

  Run: `py -3.12 scripts/smoke_app.py`
  Expected: `SMOKE_OK healthz=200 home=200`.

- [ ] **Step 5: Run the source desktop smoke test**

  Run: `py -3.12 -m app.desktop --smoke-test`
  Expected: `DESKTOP_SMOKE_OK healthz=200 home=200 resources=ok database=ok` and no real HKCU data-location writes.

- [ ] **Step 6: Build and verify the Windows installer**

  Run: `.\scripts\build_windows.ps1 -ReleaseBase 68093af`
  Expected: versioned `1.0.0` installer, successful frozen smoke test and printed SHA-256. The committed release base is the pre-implementation checkpoint pushed before this plan.

- [ ] **Step 7: Execute the manual release matrix**

  在隔离测试数据上覆盖 0.1.0，验证 C→D、D→另一测试卷、迁移失败回滚、盘离线拒绝空库、设置占用、恢复副本删除、默认卸载保留、重装接回和明确删除。不得用真实钱包数据库测试破坏性分支。

- [ ] **Step 8: Record final evidence and request integration decision**

  报告测试数量、Ruff、两个 smoke 结果、安装包绝对路径、SHA-256、工作区状态和相对 `68093af` 的提交列表。等待用户决定是否推送实现、创建 `v1.0.0` 标签及 GitHub Release。
