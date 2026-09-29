# EVE Character Groups and Player Donation Income Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add persistent single-membership character groups, group-scoped reports and synchronization, and external `player_donation` income while excluding transfers between members of the same group.

**Architecture:** Store groups in a new SQLite table and a nullable `characters.group_id` foreign key, with repository methods as the only mutation boundary. Pass the selected group's complete member-ID set into reporting so donation classification is stable even when the visible character subset changes; expose group CRUD, membership, reporting, and sync through authenticated FastAPI routes, then adapt the existing vanilla HTML/CSS/JS dashboard.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, SQLite, Decimal, Jinja2, vanilla JavaScript/CSS, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-29-eve-character-groups-player-donations-design.md`

## Global Constraints

- Work only in `.worktrees/character-groups-player-donations` on branch `feature/character-groups-player-donations`; do not modify the source `main` checkout.
- Keep the only OAuth scope as `esi-wallet.read_character_wallet.v1`; add no EVE write capability and no dependency.
- A character has zero or one group; newly authorized characters start ungrouped, and reauthorization preserves an existing assignment.
- Manual sync, dashboard data, and included-entry data require one explicit group; no cross-group aggregate is added.
- Count only positive `player_donation` with an identifiable counterpart outside the complete group; ignore internal, non-positive, missing-counterparty, and `player_trading` rows.
- Display external donations as `玩家转账` in type and entry details while including them in the existing combined income, daily, character, and net totals.
- Preserve every existing character, encrypted token, tracking start, enabled state, sync timestamp, and wallet row during schema version 1 to 2 migration.
- Keep exact Decimal arithmetic, Beijing calendar-day boundaries, the 366-day report cap, authentication, CSRF, and sanitized error behavior.
- Vendor the upstream UI skill and Apache 2.0 license at `.codex/skills/frontend-design/` without rewriting its instructions.
- Preserve the current EVE control-console visual language, keyboard focus, 320px minimum viewport support, and reduced-motion behavior.

## Review Focus

- A database with `PRAGMA user_version > 2` must be rejected without schema or data mutation (Task 2 test).
- Group names that differ only by surrounding whitespace or Latin case must conflict after normalization, while Chinese names remain supported (Task 2 test).
- A positive donation with missing counterpart IDs, or multiple party IDs including any internal member, must never be misclassified as external income (Task 3 test).
- Missing, deleted, or mismatched group and character IDs must produce stable safe errors and never leak SQLite or private wallet content (Task 5 test).
- Deleting the currently selected group, having no groups, and having an empty group must leave the UI in a usable deterministic state (Task 6 test).

## File Structure

- `.codex/skills/frontend-design/{SKILL.md,LICENSE.txt}` — repository-scoped upstream UI guidance and license.
- `app/models.py` — group record plus character group membership in domain models.
- `app/storage.py` — schema v2 migration, group repository, and character membership persistence.
- `app/reporting.py` — group-aware transfer classification and combined/separate totals.
- `app/sync.py` — group-scoped enabled-character synchronization.
- `app/main.py` — repository and service composition.
- `app/web.py` — group request models, CRUD/membership routes, group-scoped report parsing, and sync endpoint.
- `app/templates/index.html` — semantic group controls and group/ungrouped roster regions.
- `app/static/app.js` — group workspace state, API calls, roster actions, empty states, and group-aware report/sync requests.
- `app/static/app.css` — roster-rail layout and responsive/focus styling using existing tokens.
- `README.md` — user-facing group and transfer-income operating rules.
- `tests/test_{storage,reporting,sync,web,frontend,docs}.py` — focused regression coverage at each boundary.

## Frontend Design Direction

- **Color:** reuse `--void #071018`, `--hull #101d28`, `--rule #29404d`, `--ice #72d6cf`, `--amber #e9a95f`, and `--fog #e6eef2`; do not introduce gradients or a second accent system.
- **Type:** keep Bahnschrift/DIN-style display text for the instrument-console identity and the existing Microsoft YaHei/PingFang/Noto body stack for Chinese legibility.
- **Layout:** make the group the top-level scope in the existing command rail; render membership as one cyan-edged roster rail rather than a new grid of rounded cards.

```text
角色组  [ 当前组 ▾ ]
        [ 组名____________ ] [创建] [重命名] [删除]

当前组 xxx · 2 名
│  ☑ 角色 A    已同步       [停用] [移出]
│  ☑ 角色 B    需重新授权   [启用] [移出]

未分组
   角色 C                         [加入当前组]
```

- **Principles:** the memorable element is the single connected roster rail; everything else remains quiet, aligned, and operational. Labels describe user actions in sentence case; empty and failure states state the next action. On mobile, controls stack in reading order without horizontal roster scrolling.
- **Uniqueness review:** this extends the existing EVE navigation-console metaphor with one structural device tied to actual group membership. It avoids the generic SaaS-card grid, decorative gradients, tracked all-caps labels, and repeated hover animation defaults called out by the vendored skill.

---

### Task 1: Vendor the Repository UI Skill

**Files:**
- Create: `.codex/skills/frontend-design/SKILL.md`
- Create: `.codex/skills/frontend-design/LICENSE.txt`
- Modify: `tests/test_docs.py:15-89`

**Interfaces:**
- Consumes: upstream `https://raw.githubusercontent.com/anthropics/skills/main/skills/frontend-design/SKILL.md` and `LICENSE.txt`.
- Produces: a repo-scoped `frontend-design` skill discoverable by future Codex sessions; Task 6 reads this file completely before UI edits.

- [ ] **Step 1: Add a failing repository-skill test**

Add `test_repository_frontend_design_skill_is_complete()` asserting both files exist, `SKILL.md` starts with YAML containing `name: frontend-design`, its declared license is `Complete terms in LICENSE.txt`, and `LICENSE.txt` contains `Apache License` and `Version 2.0`.

- [ ] **Step 2: Run the focused test and verify the files are missing**

Run: `py -3.12 -m pytest tests/test_docs.py::test_repository_frontend_design_skill_is_complete -v`  
Expected: FAIL because `.codex/skills/frontend-design/SKILL.md` does not exist.

- [ ] **Step 3: Add the two upstream files without modifying their contents**

Fetch or copy the raw upstream `SKILL.md` and sibling `LICENSE.txt` into the exact paths above. Inspect them before writing; do not add scripts, dependencies, or project-specific instructions to the upstream bundle.

- [ ] **Step 4: Run the focused test**

Run: `py -3.12 -m pytest tests/test_docs.py::test_repository_frontend_design_skill_is_complete -v`  
Expected: PASS.

- [ ] **Step 5: Commit the skill bundle**

```bash
git add .codex/skills/frontend-design tests/test_docs.py
git commit -m 'chore: add repository frontend design skill'
```

### Task 2: Add Schema Version 2 and Character Group Persistence

**Files:**
- Modify: `app/models.py:16-109`
- Modify: `app/storage.py:32-263`
- Modify: `tests/test_storage.py:19-210`

**Interfaces:**
- Consumes: existing `Database.connection()`, UTC helpers, and `CharacterRecord` persistence.
- Produces: `CharacterGroupRecord(group_id: int, name: str, created_at: datetime)`; trailing `CharacterRecord.group_id: int | None = None` for backward-compatible construction; `CharacterGroupRepository.create/list_all/get/rename/delete`; `CharacterRepository.set_group()` and `list_for_group()`; `GroupNameConflictError`, `UnknownGroupError`, and `UnknownCharacterError`.

- [ ] **Step 1: Write failing schema migration tests**

Add `test_initialize_creates_version_two_group_schema()` and `test_initialize_migrates_version_one_without_data_loss()`. Assert `PRAGMA user_version == 2`, `PRAGMA foreign_keys == 1`, the new table/index/column exist, all migrated characters have `group_id is None`, and pre-existing character and journal fields remain byte-for-byte equivalent through repository reads.

- [ ] **Step 2: Add and run the future-version review-focus test**

Add `test_initialize_rejects_future_schema_without_mutation()` using a database at version 3 with a sentinel table; assert `Database.initialize()` raises `RuntimeError` and the version and sentinel row remain unchanged.  
Run: `py -3.12 -m pytest tests/test_storage.py -k 'version_two or migrates_version_one or future_schema' -v`  
Expected: FAIL because schema version 2 and migration handling do not exist.

- [ ] **Step 3: Implement the versioned schema lifecycle**

Keep `Database.initialize() -> None`. Add a latest-version constant of `2`, create `character_groups`, add nullable `characters.group_id REFERENCES character_groups(group_id) ON DELETE SET NULL`, create a group-membership index, migrate version 1 transactionally, create version 2 directly for version 0, and reject versions above 2 before DDL.

- [ ] **Step 4: Run the migration tests**

Run: `py -3.12 -m pytest tests/test_storage.py -k 'initialize' -v`  
Expected: all initialization and migration tests PASS.

- [ ] **Step 5: Write failing group repository and membership tests**

Add tests for create/list/get/rename/delete, UTC `created_at`, single-value reassignment, returning to ungrouped, reauthorization preserving `group_id`, delete returning moved member IDs, and unknown character/group exceptions. Add the review-focus case asserting names `' Alpha '` and `'alpha'` conflict while `'采矿组'` succeeds.

- [ ] **Step 6: Implement the domain records and repositories**

Use these exact signatures:

```python
class CharacterGroupRepository:
    def create(self, name: str, created_at: datetime) -> CharacterGroupRecord: ...
    def list_all(self) -> list[CharacterGroupRecord]: ...
    def get(self, group_id: int) -> CharacterGroupRecord | None: ...
    def rename(self, group_id: int, name: str) -> CharacterGroupRecord: ...
    def delete(self, group_id: int) -> tuple[int, ...]: ...

class CharacterRepository:
    def set_group(self, character_id: int, group_id: int | None) -> CharacterRecord: ...
    def list_for_group(self, group_id: int | None) -> list[CharacterRecord]: ...
```

Normalize names with `strip()`, require 1–50 characters, translate duplicate `sqlite3.IntegrityError` into `GroupNameConflictError`, and perform membership/delete mutations in one database transaction.

- [ ] **Step 7: Run storage tests and formatting checks**

Run: `py -3.12 -m pytest tests/test_storage.py -v`  
Expected: PASS.  
Run: `py -3.12 -m ruff check app/models.py app/storage.py tests/test_storage.py`  
Expected: PASS.

- [ ] **Step 8: Commit persistence**

```bash
git add app/models.py app/storage.py tests/test_storage.py
git commit -m 'feat: persist character groups'
```

### Task 3: Classify External Player Donations

**Files:**
- Modify: `app/reporting.py:21-133`
- Modify: `tests/test_reporting.py:12-238`

**Interfaces:**
- Consumes: `JournalEntry.first_party_id`, `second_party_id`, and the complete group member-ID set supplied by the caller.
- Produces: `classify_entry(entry: JournalEntry, group_member_ids: AbstractSet[int]) -> ClassifiedEntry | None` and `build_dashboard(..., *, group_member_ids: AbstractSet[int]) -> DashboardReport` with `玩家转账` as the fifth category.

- [ ] **Step 1: Extend test helpers and add failing donation classification tests**

Let the `journal()` helper accept both party IDs. Assert an external positive donation is `income/玩家转账`, an internal donation is `None`, a cross-group counterpart remains external, and zero/negative `player_donation` plus `player_trading` remain `None`.

- [ ] **Step 2: Add the malformed/multiple-party review-focus tests**

Assert a positive donation with no counterpart returns `None` and logs only the safe fixed warning; assert a row whose party IDs contain one external and one internal member returns `None`. Ensure `description` and `reason` sentinel secrets never appear in `caplog`.

- [ ] **Step 3: Run focused tests and observe the current failure**

Run: `py -3.12 -m pytest tests/test_reporting.py -k 'donation or counterpart' -v`  
Expected: FAIL because `player_donation` is not classified and the function lacks group context.

- [ ] **Step 4: Implement group-aware classification**

For positive `player_donation`, derive non-null party IDs other than `entry.character_id`; reject an empty counterpart set or any intersection with `group_member_ids`, otherwise return `ClassifiedEntry(entry, 'income', '玩家转账', entry.amount)`. Add the category after the four existing income categories and pass the same member set through dashboard aggregation.

- [ ] **Step 5: Add a failing combined/separate totals test, then make it pass**

Add `test_dashboard_combines_external_donations_but_keeps_category_separate()` with bounty `100`, external donation `25`, internal donation `40`, and tax `-10`; assert total income `125`, tax `10`, net `115`, the `赏金` row is `100`, and the `玩家转账` row is `25/0/25`. Include a disabled or unselected internal sender in `group_member_ids` to pin stable classification.

- [ ] **Step 6: Run reporting tests and Ruff**

Run: `py -3.12 -m pytest tests/test_reporting.py -v`  
Expected: PASS.  
Run: `py -3.12 -m ruff check app/reporting.py tests/test_reporting.py`  
Expected: PASS.

- [ ] **Step 7: Commit reporting behavior**

```bash
git add app/reporting.py tests/test_reporting.py
git commit -m 'feat: classify external player donations'
```

### Task 4: Synchronize One Character Group at a Time

**Files:**
- Modify: `app/sync.py:27-102`
- Modify: `tests/test_sync.py:51-158`

**Interfaces:**
- Consumes: `CharacterRepository.list_for_group(group_id)` from Task 2 and existing `sync_character()` failure isolation.
- Produces: `SyncService.sync_group(group_id: int) -> SyncAllResult`; the global `sync_enabled()` entry point is removed from callers.

- [ ] **Step 1: Update the sync fixture and write a failing group-scope test**

Create two groups; assign enabled characters 1 and 2 to the target, character 3 to the other group, and leave character 4 ungrouped. Assert `sync_group(target_id)` calls ESI only for enabled target members in repository order and still isolates a failure for character 2.

- [ ] **Step 2: Run the focused sync test**

Run: `py -3.12 -m pytest tests/test_sync.py -k 'sync_group' -v`  
Expected: FAIL because `sync_group` does not exist.

- [ ] **Step 3: Implement `sync_group(group_id: int) -> SyncAllResult`**

Iterate `self._characters.list_for_group(group_id)`, skip disabled records, await `sync_character()` sequentially, and return the ordered tuple. Do not sync other groups or ungrouped roles.

- [ ] **Step 4: Update the old global-sync test and run all sync tests**

Replace `test_sync_enabled_is_sequential_isolated_and_skips_disabled` with the group-scoped assertions while retaining authorization, token, ordering, and cached-row checks.  
Run: `py -3.12 -m pytest tests/test_sync.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit group sync**

```bash
git add app/sync.py tests/test_sync.py
git commit -m 'feat: sync income data by character group'
```

### Task 5: Expose Group CRUD and Group-Scoped Report APIs

**Files:**
- Modify: `app/main.py:15-34`
- Modify: `app/web.py:60-445`
- Modify: `tests/test_web.py:23-817`
- Modify: `tests/test_frontend.py:10-18`

**Interfaces:**
- Consumes: Task 2 repositories/exceptions, Task 3 reporting signatures, and Task 4 `sync_group()`.
- Produces: `AppServices.groups`; strict `CharacterGroupNameRequest` and `CharacterGroupAssignmentRequest`; `/api/groups`, `/api/groups/{group_id}`, `/api/characters/{character_id}/group`, and required `group_id` on dashboard/entries/sync.

- [ ] **Step 1: Add `CharacterGroupRepository` to service composition and test fixtures**

Update `build_app()`, `AppServices`, the `context` fixture, and the frontend test client to construct one shared group repository. Update `FakeSync` to expose `sync_group(group_id)` and record the received ID.

- [ ] **Step 2: Write failing authenticated group CRUD and membership API tests**

Assert `GET /api/groups` is creation-ordered and returns each member count plus `ungrouped_count`; create returns `201` with a normalized group, duplicate name returns `400/group_name_conflict`, rename returns `200` with the updated group, assigning character 101 sets public `group_id`, moving it to a second group replaces the first assignment, and deletion returns `200` with the moved ID while preserving the character and its wallet rows.

- [ ] **Step 3: Add auth, CSRF, validation, and safe-error cases**

Extend read-auth tests with `/api/groups`; extend mutation matrices with create, rename, delete, and assignment. Assert empty/over-50/non-string names return `400/invalid_request`, and unknown group/character IDs return `404` without SQLite messages or private sentinels.

- [ ] **Step 4: Run the group API tests and observe failure**

Run: `py -3.12 -m pytest tests/test_web.py -k 'group and not dashboard' -v`  
Expected: FAIL because the routes and service field do not exist.

- [ ] **Step 5: Implement group request models, serializers, and routes**

Keep strict `extra='forbid'` Pydantic models. Map repository exceptions to stable Chinese `WebError` responses, add `group_id` to `_public_character()`, return ISO `created_at`, return group member counts and the ungrouped count without exposing private character data, and protect every mutation with `_require_csrf`.

- [ ] **Step 6: Rewrite report fixtures and add failing group-scope tests**

Make `seed_reports()` create a group, assign 101 and 202, and return its ID. Require that ID in existing dashboard/entries test URLs. Add assertions for missing/unknown group, a selected character from another group, a deleted group, default enabled members, explicitly selected disabled members, external donation detail category, and internal donation exclusion.

- [ ] **Step 7: Implement group-scoped `report_input()` and report payloads**

Resolve the group first, load its complete members, validate all explicit selected IDs are members, default to enabled members, query only selected wallet rows, and pass the complete member-ID set to both `build_dashboard()` and `classify_entry()`. Include `group_id` and `group_name` in dashboard and entries responses.

- [ ] **Step 8: Add and implement group-scoped sync API behavior**

Change `POST /api/sync` to require a positive `group_id`, reject unknown groups, call `services.sync.sync_group(group_id)`, and retain sanitized per-character results. Update existing sync tests to assert the exact group ID passed to `FakeSync`.

- [ ] **Step 9: Pin reauthorization and ungrouped initial-sync behavior**

Extend the existing reauthorization test to assign a group before reauthorizing and assert the assignment survives. Add a test that a newly authorized, initially auto-synced ungrouped character is absent from an unrelated group report until explicitly assigned.

- [ ] **Step 10: Run Web and adjacent suites**

Run: `py -3.12 -m pytest tests/test_web.py tests/test_frontend.py tests/test_reporting.py -v`  
Expected: PASS.  
Run: `py -3.12 -m ruff check app/main.py app/web.py tests/test_web.py tests/test_frontend.py`  
Expected: PASS.

- [ ] **Step 11: Commit the backend API**

```bash
git add app/main.py app/web.py tests/test_web.py tests/test_frontend.py
git commit -m 'feat: add character group APIs'
```

### Task 6: Build the Character Group Workspace UI

**Files:**
- Modify: `app/templates/index.html:42-164`
- Modify: `app/static/app.js:1-304`
- Modify: `app/static/app.css:1-968`
- Modify: `tests/test_frontend.py:31-158`

**Interfaces:**
- Consumes: `.codex/skills/frontend-design/SKILL.md` from Task 1, the approved frontend direction above, and Task 5 JSON APIs.
- Produces: current-group state and accessible controls identified by `group-select`, `group-name`, `create-group-button`, `rename-group-button`, `delete-group-button`, `active-group-name`, `enable-region`, and `ungrouped-region`.

- [ ] **Step 1: Read the vendored UI skill completely and compare the design direction against the live CSS tokens**

Confirm the plan still uses the current product's palette, type, layout rhythm, and roster-rail concept; if implementation evidence forces a change, document the narrow change in the commit body rather than inventing a second visual system.

- [ ] **Step 2: Write failing semantic and script-contract tests**

Assert the new IDs and plain-language labels exist; JS calls all group/membership routes, appends `group_id` to report and sync requests, uses `textContent` rather than `innerHTML`, confirms deletion with the preservation message, selects the first remaining group after deletion, and renders no-group/empty-group directions.

- [ ] **Step 3: Run the frontend tests and observe failure**

Run: `py -3.12 -m pytest tests/test_frontend.py -v`  
Expected: FAIL on missing group controls and client behavior.

- [ ] **Step 4: Add the semantic HTML structure**

Place group scope before character/date controls, add a labeled select and name field with `maxlength='50'`, group action buttons, current-group heading, roster region, and ungrouped region. Keep the existing report, chart, and sync-result DOM IDs stable.

- [ ] **Step 5: Implement deterministic group workspace state in JavaScript**

Maintain `groups`, `charactersById`, and `selectedGroupId`; load groups and characters together; choose the first group when none is selected or the selected group was deleted; do not request reports without a group. Implement create/rename/delete/assign actions, preserve API error messages, refresh roster/report after mutations, and keep all inserted content on safe DOM text nodes.

- [ ] **Step 6: Scope existing report and sync actions**

Add the selected group ID to dashboard/entries queries and `/api/sync?group_id=...`; render only current members as report checkboxes, render ungrouped roles with “加入当前组”, and render current members with “移回未分组”. Internal-transfer exclusion remains server-owned.

- [ ] **Step 7: Implement roster-rail styling and responsive states**

Use only existing color/type tokens. Add one cyan-edged membership rail, aligned compact action controls, visible hover/focus/disabled states, and stacked controls below 720px; ensure 320px layouts do not require horizontal scrolling and retain the existing reduced-motion block.

- [ ] **Step 8: Add the selected-group deletion and empty-state review-focus assertions**

Test script contracts for: after delete choose the first remaining group; with zero groups hide/disable report and sync while directing the user to create one; with an empty group direct the user to add an ungrouped character.

- [ ] **Step 9: Run frontend, Web, and smoke checks**

Run: `py -3.12 -m pytest tests/test_frontend.py tests/test_web.py -v`  
Expected: PASS.  
Run: `python scripts/smoke_app.py`  
Expected: `SMOKE_OK healthz=200 home=200`.  
Run: `py -3.12 -m ruff check tests/test_frontend.py`  
Expected: PASS.

- [ ] **Step 10: Perform the frontend self-critique**

Inspect desktop, 720px, 430px, keyboard-focus, disabled, no-group, and empty-group states with available browser rendering tools. Confirm no text overlap, horizontal roster scroll, generic card proliferation, decorative motion, or inaccessible action naming; fix any observed issue and rerun Step 9.

- [ ] **Step 11: Commit the group UI**

```bash
git add app/templates/index.html app/static/app.js app/static/app.css tests/test_frontend.py
git commit -m 'feat: add character group workspace'
```

### Task 7: Document the New Operating Model and Verify the Branch

**Files:**
- Modify: `README.md:1-101`
- Modify: `tests/test_docs.py:51-88`

**Interfaces:**
- Consumes: the completed group, reporting, sync, API, and UI behavior from Tasks 1–6.
- Produces: user-facing setup/operation guidance and final verification evidence.

- [ ] **Step 1: Add failing README contract assertions**

Require the README to state: create a named group; new roles begin ungrouped; one role belongs to at most one group; manual sync is per group; external `player_donation` is included as `玩家转账`; same-group transfers are excluded; `player_trading` and market income remain out of scope.

- [ ] **Step 2: Run the focused docs test**

Run: `py -3.12 -m pytest tests/test_docs.py -k 'readme' -v`  
Expected: FAIL because the current README says player transfers are excluded and lacks groups.

- [ ] **Step 3: Update README behavior and privacy documentation**

Replace the old blanket player-transfer exclusion, document group creation/assignment/deletion, explain group-scoped sync and totals versus `玩家转账` detail, and state that deleting a group preserves roles, authorization, and wallet history.

- [ ] **Step 4: Run documentation tests**

Run: `py -3.12 -m pytest tests/test_docs.py -v`  
Expected: PASS.

- [ ] **Step 5: Run complete verification**

Run: `py -3.12 -m pytest -v`  
Expected: all tests PASS.  
Run: `py -3.12 -m ruff check app tests`  
Expected: PASS.  
Run: `git diff --check`  
Expected: no output.  
Run: `git status --short`  
Expected: only the intended README/docs-test changes before the final commit.

- [ ] **Step 6: Commit documentation**

```bash
git add README.md tests/test_docs.py
git commit -m 'docs: explain grouped income tracking'
```

- [ ] **Step 7: Verify worktree isolation and commit history**

In the feature worktree, run `git status --short` and expect no output; run `git log --oneline main..HEAD` and confirm one focused commit per task. In `D:\Vscode\eve-income-tracker`, run `git status --short` and confirm the source `main` checkout remains clean.
