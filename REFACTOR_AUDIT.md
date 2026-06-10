# J.A.R.V.I.S Trim-Down Audit

> Multi-agent audit (66 agents, graph-grounded). 59 findings, each independently
> verified; 41 confirmed. Read-only — no code changed. Items reach "Quick wins"
> only if `confirmed AND safe_to_remove AND confidence >= 0.7`.

## Executive summary

Applying the gate, only a minority qualify as genuine quick-win trims; the majority
are real but require behavior-preserving refactors, are efficiency fixes (net-zero or
net-positive LOC), or are unconfirmed/inflated.

**Rough addressable reduction (confirmed + safe_to_remove, conf >= 0.7):**

| Bucket | Items | ~LOC saved |
|---|---|---|
| Python dead code / dead scripts | services/research, sync `llm_call_with_fallback`, `try_fallback_endpoint`, 5 unused Pydantic models, `run_script_argv`/`has_bash`, `find_admin_user`, `get_wsl_windows_user_profile`, `fix_paths.py`, FAISS migration script, `demo_email/` | ~1,790 |
| Python refactor-trims (safe, small) | TOOL_SECTIONS extract, `action_summarize_emails`/`action_draft_email_replies`, `_q` dedupe | ~240 (net far less; mostly relocation) |
| JS | none qualify as safe trims | 0 |
| Assets / hygiene (bytes, not LOC) | `qrcode.min.js` delete, `graphify-out/` gitignore | ~0 LOC / ~29MB + 24KB |

- **Python vs JS split:** Essentially all confirmed-safe *line* reductions are Python (~1,790 dead-code LOC plus small refactors). **No JS source trim is confirmed-safe** — every JS finding (html2pdf, xlsx, docx, mammoth) is a heavy *asset* whose removal would break a live feature; those are byte-weight reductions gated behind real rewrites.

**5 highest-leverage opportunities (by impact, not just LOC):**
1. **Delete `services/research/` dead duplicate** (~620 LOC) — highest single confirmed-safe trim; production uses `src.research_handler` exclusively.
2. **Delete `scripts/demo_email/`** (~553 LOC) and **FAISS migration script** (~200 LOC) — large, orphaned, confirmed-safe dev tooling.
3. **Email-stack consolidation into a dependency-light `src/email_core.py`** — the single largest *structural* win (~80–175 real LOC across many duplicated helpers), but a careful refactor, not a delete.
4. **Extract `TOOL_SECTIONS` from `agent_loop.py`** (~210–300 LOC relocated out of a 3,067-line god-file) — confirmed-safe with a re-export shim.
5. **Cache the prompt-assembly / settings / prefs hot paths** — efficiency, not size: `_build_base_prompt` re-runs full assembly on every cache hit; `SkillsManager` re-parses every SKILL.md per turn; `prefs._load()` reads up to 6x per `resolve_endpoint`.

---

## Quick wins (low risk, confirmed + safe_to_remove, confidence >= 0.7)

Ordered by ~LOC saved. **Every dead-code item that has a test must delete the test in the same change**, and `services/__init__.py` re-export edits must be atomic.

| Item | Locations | ~LOC saved | Why safe |
|---|---|---|---|
| Delete dead `services/research/` package | `services/research/research_handler.py`, `service.py`, `__init__.py`; re-export lines in `services/__init__.py:13,27-29`; delete `tests/test_research_service.py`, `tests/test_services_research_low_quality_sources.py` | ~620 | conf 0.9. Production wires `ResearchHandler` only from `src.research_handler` (`src/app_initializer.py:22,83`, `src/task_scheduler.py:1675`). `ResearchService` referenced only by the re-export + 2 tests. The low-quality gate the tests cover already exists in live `src/research_handler.py`. **Atomicity:** remove the `from .research import ...` line + `__all__` entries in the same commit or every `services.*` import breaks at startup. |
| Delete `scripts/demo_email/` | `scripts/demo_email/demo_account.py`, `manage.sh`, `seed_demo_emails.py` | ~553 | conf 0.9. Zero refs outside the dir + graphify artifacts; not in CI, compose, README. Scripts import *from* the app, nothing imports them. Capability loss only (dev seeding), no runtime path. |
| Delete FAISS→Chroma migration + test | `scripts/migrate_faiss_to_chroma.py`, `tests/test_migrate_faiss_to_chroma.py` | ~200 | conf 0.85. FAISS fully removed (0 `import faiss`); store is ChromaDB. Script reads a legacy layout no live code produces; only its own test importlib-loads it. Note `MEMORY_VECTORS_DIR` (`src/constants.py:65`) is still live — do not touch the directory path, only the script. Gate behind a release note. |
| Remove `try_fallback_endpoint()` + its lone test | `routes/chat_helpers.py:201-288`; `tests/test_resolve_session_auth_chatgpt.py:168-215` | ~90 | conf 0.9. Zero production in-degree. Production fallbacks are different symbols (`webhook_routes._select_api_chat_fallback_endpoint`, `stream_llm_with_fallback`). Confirm the sibling `resolve_session_auth` security tests before deleting the test. |
| Delete 5 unused Pydantic models | `src/request_models.py:29,50,106,112,130` (`SessionCreateRequest`, `MemoryUpdateRequest`, `ErrorResponse`, `UploadResponse`, `MemoryResponse`) | ~40 | conf 0.95. No importer; no `response_model=` / star-import / dynamic dispatch. |
| Delete `get_wsl_windows_user_profile()` + 3 tests | `core/platform_compat.py:336-357`; `tests/test_platform_compat.py:127-170` | ~22 src + ~44 test | conf 0.9. Test-only; no production caller; not in `core/__init__.py __all__`. |
| Remove `find_admin_user()` + its test | `companion/pairing.py:63-81`; `tests/test_companion_pairing.py:118-127` | ~19 + test | conf 0.92. `companion/routes.py` never calls it; owner resolution is cookie-based. |
| Delete `run_script_argv()` + `has_bash()` | `core/platform_compat.py:256-257, 278-293` | ~18 | conf 0.92. Unreferenced; `has_bash` only mentioned in `run_script_argv`'s docstring. No tests. |
| Delete sync `llm_call_with_fallback()` | `src/llm_core.py:1204-1224` | ~22 | conf 0.96. All 8 call sites use the async variant. Update the async docstring (line 1228). |
| Delete `scripts/fix_paths.py` | `scripts/fix_paths.py:1-9` | 9 | conf 0.95. No-op: patches a `BASE_DIR =` line that no longer exists; referenced nowhere. |
| Delete vendored `qrcode.min.js` | `static/lib/qrcode.min.js` | ~0 LOC / 24,861 bytes | conf 0.97. Zero JS/HTML loaders; 2FA QR is server-side (`routes/auth_routes.py:204-210`). Drop the `ACKNOWLEDGMENTS.md` credit line too. |
| Add `graphify-out/` to `.gitignore` | `.gitignore` | 0 (prevents a 29MB commit) | conf 0.92. Untracked generated tooling output; also holds the stale `odysseus` rebrand artifacts. Regenerate after ignoring. |

**Quick-win subtotal: ~1,800 LOC of source/test deletions** (Python) plus the two asset/hygiene items.

---

## Larger refactors (duplication consolidation, module splits)

Real but **not behavior-preserving deletes** — verifiers flagged `safe_to_remove=false` or `confirmed=false`. Do as deliberate refactors with tests.

### A. Email stack consolidation (dominant duplication target)
`mcp_servers/email_server.py`, `routes/email_helpers.py`, `routes/email_routes.py` carry parallel IMAP/SMTP/config/MIME stacks. **Critical constraint:** `email_server.py` is a standalone stdio MCP subprocess that must NOT import `routes/email_helpers.py` at module scope (drags in FastAPI + a DB-migration side-effect). Target a **new dependency-light `src/email_core.py`** both sides import.
- **Tier 1 (do first, ~80 real LOC):** shared MIME/quote helpers — `_decode_header`, `_extract_text`, `_detect_sent_folder`, `_q`. Keep module-level names resolvable (3 tests pin them).
- **Trivial folder helpers (~23 LOC):** `_folder_name_from_list_line`/`_folder_role_from_name`/`_smtp_ready`. Take the **superset** candidate table.
- **Do NOT naively merge:** `_list_attachments_from_msg`/`_extract_attachment_to_disk`/`_imap_connect` (divergent return types/fields/scoping); config resolvers + SMTP send + list/read/search (`_get_email_config` vs `_load_config` are **not** equivalent — owner-scoping, dict-shape, fuzzy selection, persistent-vs-one-shot SMTP). High risk; separate test-heavy project.

### B. Extract `TOOL_SECTIONS` from `src/agent_loop.py` (god-file split)
Move `TOOL_SECTIONS` (`src/agent_loop.py:392-601`, ~210 lines) → new `src/tool_prompts.py` with a **re-export shim** (imported by name in `routes/skills_routes.py:1158,1183,1208`, `src/tool_policy.py:159`, `tests/test_research_report_read.py:22`). conf 0.9, low risk. Relocation, net repo LOC ~flat.

### C. Collapse `action_summarize_emails`/`action_draft_email_replies`
`src/builtin_actions.py:504,517` — one parameterized helper, two thin registry entries. **Preserve** the draft path's `days_back=7` + `progress_cb`. ~8–12 LOC. conf 0.85.

### D. Merge the two `youtube_handler` modules
`services/youtube/youtube_handler.py` vs `src/youtube_handler.py` — ~99% identical forks. **Union all four hardening deltas** (don't just keep one), re-point 4 imports (`app.py:440`, `routes/diagnostics_routes.py:8`, `src/chat_handler.py:19`, `src/chat_processor.py:9`), reconcile 6 tests. **~270 LOC genuinely deleted** — strongest real duplication win. conf 0.9, medium risk.

### E. Module splits — flagged, but NOT size reductions
`setup_cookbook_routes()` (2,900-line closure), `setup_email_routes()` (41 handlers + mutable cache/pool state), `_auto_summarize_pass_single()`, the `do_manage_*` dispatchers, cookbook cluster in `tool_implementations.py`. De-nesting **adds** lines and risks captured mutable state. Do for readability with tests — not for the size goal.

### F. `~197 SessionLocal()` → `get_db_session()` sweep
`core/database.py:1986` exists; ~197 manual `try/finally` sites bypass it. **Not mechanical** — `get_db_session()` auto-commits/rolls-back, so converting read-only or `HTTPException`-raising blocks changes behavior. Per-file, per-site classification, incremental, test-backed.

---

## Efficiency improvements (net-zero/positive LOC; runtime value, not size)

| Hot path | Problem | Fix | Caveats |
|---|---|---|---|
| `_build_system_prompt` cache-hit (`agent_loop.py:962-971`) | Re-runs full `_build_base_prompt` every cache hit, discards all but the skill-index block | Extract `_build_skill_index_block(...)` (lines 1398-1425), call only it on cache-hit. conf 0.85 | Preserve `owner=None` semantics + soft-fail try/except; keep skill index out of trusted role |
| `SkillsManager.load_all()` (`services/memory/skills.py:217`) | Re-reads + parses every SKILL.md + usage JSON every turn | **Module-level** mtime cache folding per-file + `_usage.json` mtimes. conf 0.85 | Dir-mtime alone misses in-place edits (breaks "next-turn" contract) |
| `resolve_endpoint()` (`src/endpoint_resolver.py:240-272`) | Reads prefs file ~6x/call via uncached `_load()`; ~30 hot sites | mtime/TTL cache, or load once + resolve 6 keys locally. conf 0.85 | Preserve per-user-vs-global merge |
| `load_integrations()` (`src/integrations.py:204`) | Re-reads + decrypts every request | mtime cache — **return copies**, **invalidate in `save_integrations`** (secrets!), keep plaintext-migration write. conf 0.8 | Correctness-sensitive |
| Duplicate uncached `_load_settings()` (`email_helpers.py:583`, `contacts_routes.py:34`) | Bypass the 2s-TTL `load_settings()` | Delegate **read-only** sites only; **leave** the read-modify-write at `contacts_routes.py:776` raw. conf 0.8 | — |
| MCP email server IMAP (`email_server.py:285`) | Fresh TCP+TLS+LOGIN per tool call, no pooling | Port the route-side `_pooled_connect` pool (single-user, key by account). conf 0.85 | Adds code; updates `tests/test_imap_leak_fixes.py` |

**Contested (apply only with corrections):** `_load_prompt_overrides` getmtime (near-zero gain); `is_setting_overridden` cache (breaks `test_context_budget.py`); Tailscale `resolve_url` (add TTL + `to_thread`); `estimate_tokens` per-message cache **rejected** (messages mutate → corrupts trim budgets).

---

## Investigate further (real but unconfirmed / risky)

- **Heavy vendored JS (byte-weight, all `safe_to_remove=false`):** `html2pdf.bundle.min.js` (906KB, lazy-loaded), `xlsx.full.min.js` (951KB, breaks `.xls`/`.ods` import), `docx.umd.min.js` (762KB, only `.docx` writer), `mammoth.browser.min.js` (642KB, duplicates server `markitdown[docx]` but no binary-upload endpoint exists). All gated behind real rewrites/feature decisions.
- **`email_thread_parser.py` ↔ `emailLibrary.js`** (~550 lines each): confirmed duplication, but JS is the **only live renderer** (server bubbles hard-disabled). Feature decision, not a trim.
- **`_uid` byte/fetch helpers**, **`do_manage_*` dispatch tables**, **`_build_system_prompt` vs `_build_base_prompt`** (a wrapper, not a dup): `confirmed=false`. At most hoist literal alias dicts to constants.
- **`.mjs` test wiring**: `tests/streaming/*.mjs` DO run (via `test_streaming_segmenter_js.py`); the placeholder one encodes a live XSS guard — rename/move, don't delete.
- **TTS setup scripts** (`setup_chatterbox_tts.py` vs `setup_lemonade.py`), **`claim_ownerless.py`**, **one-off maintainer scripts** (`index_documents.py`, `encode_previews.sh`, `add_hwfit_models.py`): regenerate live artifacts — consolidate under `scripts/maintenance/` with a README, don't delete.
- **Dependency hygiene (non-trims):** add explicit `Pillow` (currently transitive via `qrcode[pil]`); split `pytest`/`pytest-asyncio` into `requirements-dev.txt` **only with** a coordinated CI edit (`.github/workflows/ci.yml:89` installs only `requirements.txt`). `markdown`/`nh3` correctly kept (security-relevant).

---

## Recommended execution order

Safest-first; each step independently approvable. Steps 1–4 are pure deletions (run pytest after each); 5+ are refactors/efficiency requiring review.

1. **Asset/hygiene, zero risk:** `.gitignore` `graphify-out/`; delete `static/lib/qrcode.min.js` (+ ACKNOWLEDGMENTS credit); delete `scripts/fix_paths.py`. Regenerate `graphify-out/` (clears stale `odysseus` artifacts).
2. **Standalone dead Python (no test coupling):** sync `llm_call_with_fallback` (+ async docstring); `run_script_argv`/`has_bash`; 5 unused Pydantic models.
3. **Dead code WITH paired tests (delete code + test together, run pytest):** `try_fallback_endpoint`; `get_wsl_windows_user_profile`; `find_admin_user`.
4. **Dead packages/scripts (largest deletions):** `services/research/` (atomic `services/__init__.py` edit) + 2 tests; `scripts/demo_email/`; `scripts/migrate_faiss_to_chroma.py` + test (release note).
5. **Low-risk refactor-trims:** extract `TOOL_SECTIONS` → `src/tool_prompts.py` (shim); collapse the two email actions.
6. **YouTube merge:** union all four guards, re-point 4 imports + 6 tests (~270 LOC deleted).
7. **Efficiency fixes (review-gated):** `_build_skill_index_block`; `SkillsManager` module-level mtime cache; `resolve_endpoint`/prefs cache; read-only `_load_settings` delegation; then `load_integrations` cache + MCP IMAP pool.
8. **Email-core extraction (project):** dependency-light `src/email_core.py`; migrate equivalent helpers with tests; defer config/SMTP/list-read-search.
9. **Maintainability splits (optional, not size):** cookbook/email route de-nesting; `SessionLocal()→get_db_session()` sweep — incremental, test-backed.
10. **Feature-level decisions (not now):** email_thread_parser ↔ emailLibrary.js; heavy JS asset replacements; TTS-script reconciliation; maintainer-script consolidation; `.mjs` wiring; dependency hygiene.
