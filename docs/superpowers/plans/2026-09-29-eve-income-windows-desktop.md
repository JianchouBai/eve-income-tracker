# EVE Income Tracker Windows Desktop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Ship a Windows x64 installer that opens the existing wallet UI in a native PyWebView window, uses public-client PKCE, and preserves each computer's private database across upgrades.

**Architecture:** FastAPI, SQLite, and the current HTML/CSS/JavaScript stay as the application core. A Windows bootstrap layer resolves LocalAppData paths, loads a DPAPI-protected key, prepares/imports the database, starts one loopback Uvicorn server, establishes a one-use desktop session, and starts Edge Chromium PyWebView. PyInstaller onedir freezes it and Inno Setup installs it per user.

**Tech Stack:** Python 3.12 Windows build (application floor 3.11), FastAPI 0.141.1, Uvicorn 0.54.0, SQLite, cryptography 50.0.1, PyWebView 6.2.1, PyInstaller 6.22.3, ctypes Win32 APIs, Inno Setup, pytest 9.1.1, Ruff 0.16.9.

**Spec:** docs/superpowers/specs/2026-09-29-eve-income-windows-desktop-design.md

## Global Constraints

- Work only in .worktrees/windows-desktop-app on feature/windows-desktop-app.
- Installed use requires no Python and shows no console. Development Uvicorn mode remains available.
- Desktop server is one worker on both 127.0.0.1:8000 and [::1]:8000 with access logging off.
- Installed data lives only below %LOCALAPPDATA%\EVE Income Tracker; installation files are read-only.
- Every computer owns an independent database, key, auth state, logs, and backups.
- Remove Client Secret everywhere; use PKCE S256 and the public Client ID.
- Preserve wallet-only scope, strict JWT checks, exact Decimal, Beijing dates, signed sessions, CSRF, group/report behavior, and sanitized errors.
- Auth attempts and launch tokens are memory-only, high-entropy, expiring, one-use, and session-scoped.
- Git ignores real release config, DB/backups/keys/logs, WebView2 download, build, frozen, and installer output.
- Upgrade never touches personal data; uninstall preserves it unless exact deletion is explicitly confirmed.
- app/version.py is the only version source.
- PyWebView 6.2.1 and PyInstaller 6.22.3 change only with official evidence and a ledgered ruling.

## Review Focus

- Spaced/non-ASCII paths, unwritable roots, and two simulated Windows users remain isolated.
- Locked, corrupt, future, or failed-migration DBs preserve original bytes; newest three backups survive pruning.
- Unknown/expired/replayed/cross-session OAuth or launch tokens never exchange and never leak secrets.
- Duplicate launch, occupied port, no WebView2, health timeout, and close release only owned resources.
- Frozen/installed output includes templates/static/tzdata/EdgeChromium/public config/icon/version, and excludes secrets/Qt/CEF/.env/data/tests.

## File Map

- app/version.py and app/paths.py: version, resource roots, and LocalAppData layout.
- app/key_store.py and app/logging_setup.py: DPAPI key and redacted rotating log.
- app/database_lifecycle.py: backups, restore, pruning, and legacy-v2 import.
- app/auth_attempts.py and app/desktop_session.py: PKCE attempts and desktop launch tokens.
- app/windows_runtime.py, app/desktop_server.py, app/desktop_bridge.py, app/desktop.py: Windows bootstrap.
- app/config.py, app/main.py, app/sso.py, app/web.py and current frontend: integration.
- packaging/, scripts/build_windows.ps1, assets/, and build-support scripts: frozen app/installer.
- tests/test_paths.py, test_key_store.py, test_database_lifecycle.py, test_auth_attempts.py, test_desktop_session.py, test_windows_runtime.py, test_desktop_server.py, test_desktop_bridge.py, test_desktop.py, test_packaging.py, test_windows_build.py: new gates.

---

### Task 1: Version, Paths, and Public Release Config

**Files:** Create app/version.py, app/paths.py, release-config.example.json, tests/test_paths.py, tests/test_version.py. Modify app/__init__.py, app/config.py, pyproject.toml, .gitignore, tests/test_config.py.

**Interfaces:** APP_VERSION; AppPaths(resource_root, data_dir, config_dir, logs_dir, backups_dir, database_path, key_path, webview_storage_dir); resolve_app_paths(frozen, local_app_data=None, bundle_root=None); ReleaseConfig.from_json(path) with only eve_client_id, eve_callback_url, eve_user_agent, esi_compatibility_date.

- [ ] Write failing tests for development/frozen resources, exact documented LocalAppData layout, Chinese/spaced roots, two-user isolation, and resolution with no writes.
- [ ] Write failing release-config tests: four public fields load; blanks/missing fields and secret/private unknown keys fail without echoing values.
- [ ] Write failing version test: app.__version__ equals strict MAJOR.MINOR.PATCH APP_VERSION and project metadata uses it dynamically.
- [ ] Run py -3.12 -m pytest tests/test_paths.py tests/test_version.py tests/test_config.py -v. Expected: missing interfaces fail.
- [ ] Implement immutable paths/config; development keeps repository defaults, desktop uses injected LocalAppData/bundle roots only.
- [ ] Export version, add dynamic project metadata and placeholder example; expand ignores for all private/build artifacts.
- [ ] Run focused tests, full pytest, Ruff. Expected: all pass.
- [ ] Commit: feat: add desktop runtime path boundaries.

### Task 2: DPAPI Key and Safe Logging

**Files:** Create app/key_store.py, app/logging_setup.py, tests/test_key_store.py, tests/test_logging_setup.py. Modify app/security.py.

**Interfaces:** DataProtector.protect/unprotect; WindowsDpapiProtector; FernetKeyStore.load_or_create() -> bytes; KeyStoreError(code); configure_desktop_logging(path).

- [ ] Write failing key lifecycle test using fake protector: valid key, protected-only file, reuse, parent creation, independent roots.
- [ ] Add corrupt/unreadable/unwritable tests; existing bad key is never silently replaced and errors reveal no bytes/path contents.
- [ ] Write log test covering code/state/verifier/token, Authorization/Cookie, callback query, key, and wallet-description sentinels.
- [ ] Run py -3.12 -m pytest tests/test_key_store.py tests/test_logging_setup.py tests/test_security.py -v. Expected: missing modules fail.
- [ ] Implement current-user DPAPI with UI forbidden, LocalFree, same-directory temp+flush+atomic first create.
- [ ] Implement one UTF-8 rotating handler and redaction; callers log stage/code, not raw exceptions.
- [ ] Run focused/full tests and Ruff. Commit: feat: protect desktop secrets with DPAPI.

### Task 3: Database Backup, Restore, and Legacy Import

**Files:** Create app/database_lifecycle.py, tests/test_database_lifecycle.py. Modify app/storage.py, tests/test_storage.py.

**Interfaces:** Database.inspect_version(); DatabaseLifecycle.prepare(); DatabaseLifecycle.import_legacy(source) -> ImportResult; DatabaseLifecycleError(code); backup name eve_income-v{version}-{UTC}.sqlite3 and newest-three retention.

- [ ] Write failing v1-to-v2 test proving byte-identical pre-migration backup and newest-three retention.
- [ ] Write failure tests: mutation then migration error restores bytes; future/corrupt/locked DB and unwritable backup never truncate/delete.
- [ ] Write v2 import test preserving groups, enabled state, tracking start, sync time, and wallet rows while clearing old encrypted tokens and marking reauthorization.
- [ ] Add refusal tests for existing target, same source/target, non-SQLite, missing schema, and future version; source unchanged, no partial target.
- [ ] Run py -3.12 -m pytest tests/test_database_lifecycle.py tests/test_storage.py -v. Expected: lifecycle tests fail.
- [ ] Implement read-only inspection, copy+flush before migration, transactional initialize, integrity/version check, exact restore, prune after success only.
- [ ] Import by validated copy-to-temp, transactional credential invalidation, and atomic install while target is absent; never read/copy .env.
- [ ] Run focused/full tests and Ruff. Commit: feat: add durable desktop database lifecycle.

### Task 4: Public-Client PKCE and Auth Attempts

**Files:** Create app/auth_attempts.py, tests/test_auth_attempts.py. Modify app/config.py, app/sso.py, .env.example, tests/test_config.py, tests/test_sso.py, tests/test_sync.py.

**Interfaces:** PkcePair.generate(); SsoClient.build_authorization_url(state, challenge); exchange_code(code, verifier); refresh sends client_id form/no Basic; AuthorizationAttemptStore.start, claim_callback, succeed, fail, consume_status.

- [ ] Rewrite request tests for unpadded S256, verifier/client_id exchange, client_id refresh, no Authorization/Client Secret, no redirects.
- [ ] Add entropy test: verifier decodes to 32 bytes and independent hand-derived base64url SHA-256 equals challenge; malformed inputs fail before network.
- [ ] Add attempt tests: unguessable IDs/state, secret-safe repr, exact 10-minute expiry, cleanup, claim once, owner-only terminal consume once.
- [ ] Cover unknown/expired/replay/cross-session. Fresh system browser may have no cookie; a present different session is rejected; polling always requires owner.
- [ ] Run py -3.12 -m pytest tests/test_auth_attempts.py tests/test_sso.py tests/test_config.py tests/test_sync.py -v. Expected: old confidential flow fails.
- [ ] Delete eve_client_secret from settings/examples/tests/requests; implement PKCE while retaining discovery allow-list and all JWT validation.
- [ ] Implement private in-memory state machine, claiming before token exchange and exposing safe status only.
- [ ] Run focused/full tests and Ruff. Commit: feat: use public-client PKCE authorization.

### Task 5: FastAPI Desktop Session and Authorization Polling

**Files:** Create app/desktop_session.py, tests/test_desktop_session.py. Modify app/main.py, app/web.py, app/templates/index.html, app/static/app.js, app/static/app.css, tests/test_web.py, tests/test_frontend.py.

**Interfaces:** Produces DesktopLaunchTokenStore.issue/consume with 60-second TTL, RuntimeContext(mode, attempts, launch_tokens), POST /api/authorization-attempts, GET /api/authorization-attempts/{id}, and the JavaScript-side contract open_authorization_url(url) that Task 6 implements.

- [ ] Write launch-token tests for 32+ random bytes, expiry, cleanup, constant-time comparison, and replay rejection.
- [ ] Add Web test: valid query token sets viewer/desktop_session_id/CSRF then immediately redirects to clean /; invalid/replayed is safe 401; development still requires login.
- [ ] Rewrite OAuth Web tests for browser redirect, desktop JSON start, stored verifier, strict ownership/replay, safe callback page, owner poll, first sync, and reauthorization history.
- [ ] Add frontend test: desktop button creates with CSRF, calls only window.pywebview.api.open_authorization_url, polls boundedly, reloads on success, gives safe retry text; browser keeps /auth/login.
- [ ] Run py -3.12 -m pytest tests/test_desktop_session.py tests/test_web.py tests/test_frontend.py -v. Expected: missing desktop flows fail.
- [ ] Add RuntimeContext with one attempt store per app process; leave repositories/services injectable.
- [ ] Consume launch token before render, establish signed HttpOnly/SameSite/CSRF state, redirect; never accept token by header/bridge/API.
- [ ] Implement browser/desktop PKCE delivery. Claim before exchange, verify/save/sync, then redirect browser or show safe close-tab page while desktop polls.
- [ ] Adapt authorization UI only; preserve report/group DOM and design; no provider text or secrets reach JS.
- [ ] Run focused/full tests, Ruff, and py -3.12 scripts/smoke_app.py. Commit: feat: add secure desktop web sessions.

### Task 6: Win32, Bridge, and Loopback Server Primitives

**Files:** Create app/windows_runtime.py, app/desktop_server.py, app/desktop_bridge.py and matching three test files.

**Interfaces:** Consumes Task 5's JavaScript bridge contract; produces SingleInstanceGuard.acquire/release, webview2_version(), show_native_error(), ask_yes_no(), choose_legacy_database(), LocalServer.start/stop, wait_until_healthy(), and DesktopBridge.open_authorization_url(url).

- [ ] Test mutex and registry adapters: ERROR_ALREADY_EXISTS, owned-handle close, official HKLM/HKCU pv greater than 0.0.0.0.
- [ ] Test fixed Chinese prompts and existing-file/cancel SQLite picker; JS cannot supply a path.
- [ ] Test real temporary-port server: exact health before window stage, port release, early exit, timeout, occupied port, bounded shutdown without killing owner.
- [ ] Test bridge accepts only HTTPS login.eveonline.com, absent/443 port, no userinfo; rejects HTTP/lookalikes/whitespace/other ports/local/file/shell before opener.
- [ ] Run py -3.12 -m pytest tests/test_windows_runtime.py tests/test_desktop_server.py tests/test_desktop_bridge.py -v. Expected: missing modules fail.
- [ ] Implement import-safe ctypes wrappers, user mutex, official registry checks, OPENFILENAMEW FILEMUSTEXIST/PATHMUSTEXIST, and safe logging.
- [ ] Implement one-worker dual-loopback 127.0.0.1:8000 and [::1]:8000 Uvicorn with no access log; health uses trust_env=False, no redirects, short timeout, liveness check.
- [ ] Expose only allow-listed URL opening. Run focused/full tests and Ruff. Commit: feat: add Windows desktop runtime primitives.

### Task 7: First Run and Desktop Bootstrap

**Files:** Create app/desktop.py, tests/test_desktop.py, requirements-windows.txt. Modify app/main.py, README.md.

**Interfaces:** DesktopApplication.run() -> int; main(argv=None) -> int; --smoke-test; --local-app-data PATH; 1180x780 resizable EdgeChromium window.

- [ ] Ordered bootstrap test: mutex, paths, log, WebView2, first-run import before DB creation, key, DB prepare, public config, app, server, health, launch token, window, GUI, shutdown, mutex.
- [ ] First-run tests: Yes+file imports first; cancel/No creates new; later runs do not prompt; failed import alters neither source nor target.
- [ ] Error/cleanup matrix: duplicate, port occupied, no WebView2, unwritable data, key/DPAPI, future/corrupt DB, migration/import, health timeout, window failure, normal close. Assert actionable Chinese message and owned cleanup.
- [ ] Smoke test with temporary LocalAppData/public test config/no EVE: health/home/resources/database, clean stop, no real/repo writes.
- [ ] Run py -3.12 -m pytest tests/test_desktop.py -v. Expected: entry missing.
- [ ] Implement pre-server first run, ephemeral session secret, DPAPI key, public bundled config only; desktop never reads .env.
- [ ] Lazy-import webview after health; pass only DesktopBridge; force edgechromium; disable debug/devtools/downloads/file URLs; store WebView data below app root; finally stop/join/release.
- [ ] requirements-windows.txt includes -r requirements.txt and pywebview==6.2.1, no Qt/CEF.
- [ ] Run focused/full tests, Ruff, and source --smoke-test. Expect DESKTOP_SMOKE_OK healthz=200 home=200 resources=ok database=ok.
- [ ] Commit: feat: add Windows desktop application entry.

### Task 8: PyInstaller Inputs and Frozen Smoke

**Files:** Create assets/app-icon.svg, scripts/generate_windows_icon.py, scripts/build_support.py, scripts/smoke_desktop.py, packaging/eve_income_tracker.spec, requirements-build.txt, tests/test_packaging.py. Modify tests/test_docs.py.

**Interfaces:** validated build/generated/release-config.json; deterministic ICO sizes 16/32/48/256; onedir dist/windows/EVE Income Tracker; run_frozen_smoke(exe, temp_root).

- [ ] Test release validation: real-looking public ID, exact localhost callback, valid date/contact UA, four keys only, no echoed values.
- [ ] Test deterministic ICO and dry packaging manifest: include templates/static/tzdata/EdgeChromium/config/version/icon; exclude Qt/CEF/.env/data/key/log/tests.
- [ ] Test frozen smoke runner with fake exe: isolated LocalAppData, stripped credentials/proxy/worker env, timeout, safe output, exact success contract.
- [ ] Run py -3.12 -m pytest tests/test_packaging.py tests/test_docs.py -v. Expected: inputs absent.
- [ ] Generate config and ICO only into ignored build output from source SVG; smoke deletes only exact owned temp root.
- [ ] Implement onedir/windowed spec for app/desktop.py, console false, explicit datas/hidden imports, version/icon, runtime resource support, excluded backends.
- [ ] requirements-build includes Windows requirements, pyinstaller==6.22.3, and only icon-generation dependency.
- [ ] Run focused/full tests, Ruff app scripts tests, and git diff --check. Commit: build: add Windows frozen application inputs.

### Task 9: Inno Installer and Windows Build Pipeline

**Files:** Create packaging/eve-income-tracker.iss, scripts/build_windows.ps1, tests/test_windows_build.py. Modify .gitignore, README.md.

**Interfaces:** consumes onedir, generated metadata, ignored vendor/MicrosoftEdgeWebview2Setup.exe, ISCC.exe; produces installer-output/EVE-Income-Tracker-{version}-x64-Setup.exe and SHA-256; fixed AppId; lowest privileges; {localappdata}\Programs\EVE Income Tracker.

- [ ] Test preflight and cleanup: Windows x64 Python 3.12, clean release commit, valid public config, exact bootstrapper, ISCC; only resolved project build/windows, dist/windows, installer-output may be recursively cleaned.
- [ ] Test Inno preprocess/temp compile metadata: fixed AppId, lowest privilege, per-user paths, version source, shortcuts, default data preservation, explicit Chinese exact-root deletion confirmation.
- [ ] Test WebView2: official HKLM/HKCU pv, /silent /install only if absent, recheck, safe abort, independent app runtime check.
- [ ] Run py -3.12 -m pytest tests/test_windows_build.py tests/test_packaging.py -v. Expected: pipeline absent.
- [ ] Implement installer: same-AppId upgrades replace program files only; update/install never touch data; uninstall defaults preserve.
- [ ] Implement build script: prerequisites, full pytest/Ruff/diff, generate inputs, verified cleanup, PyInstaller, isolated frozen smoke, Inno, absolute setup path and SHA-256.
- [ ] Run focused/full tests, Ruff, diff check.
- [ ] Run real build with local release config and WebView2 bootstrapper. Expect source gates, onedir, DESKTOP_SMOKE_OK, Inno, path/hash.
- [ ] Search staging/frozen/installer for Client Secret name/value, local config path, sentinels, .env, sqlite, local.key, and real LocalAppData. Expect none except four-field public config.
- [ ] Commit: build: add Windows installer pipeline.

### Task 10: Documentation and Acceptance

**Files:** Modify README.md, tests/test_docs.py. Create docs/windows-release-checklist.md.

- [ ] Add failing docs assertions for install/launch without Python/terminal, per-PC DB, public ID/no secret, system-browser auth, LocalAppData, v2 import/re-auth, upgrade preservation, uninstall choices, manual GitHub Release, unknown publisher, and retained dev workflow.
- [ ] Run py -3.12 -m pytest tests/test_docs.py -v. Expected: current secret-based docs fail.
- [ ] Rewrite README with installed use first; document that data/key/log/backups/local build config/installers never enter Git.
- [ ] Add checklist: clean install/shortcut, duplicate launch, auth/sync, reopen, covering upgrade, v2 import/source preservation, missing WebView2, preserve/delete uninstall, second clean PC install/auth/sync/uninstall.
- [ ] Run full pytest -v, Ruff app scripts tests, diff check, existing smoke, and source desktop smoke. Expect all green.
- [ ] Run clean build, frozen smoke, install setup, locally test checklist, uninstall preserving data. Expect no console/data loss/private build material.
- [ ] Commit: docs: explain Windows desktop operation.
- [ ] Verify feature worktree clean with focused commits and source main checkout clean.
