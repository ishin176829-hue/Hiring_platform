# Shared implementation contract

## Browser Python interface

`BrowserAdapter(state_dir: Path, *, headless: bool = False)`; all methods async.

- `open_login() -> dict`: open or reuse the dedicated browser at BOSS login/search, return status.
- `status() -> dict`: `{state: closed|login_required|verification_required|ready|unknown, url, message}`. Conservative: do not return ready based only on absence of a login popup.
- `screenshot() -> bytes`: PNG of current page; raise BrowserBlocked when closed.
- `collect(cities: list[str], keywords: list[str], limit: int) -> list[dict]`: no more than limit globally. Each returned job fields: `job_key, url, title, company, city, salary, experience, education, description, recruiter, tags` (tags array of strings). Never assign requested city to records without page evidence. Only Hangzhou/Shanghai. May raise BrowserBlocked.
- `check(job: dict, action: str, resume_name: str) -> dict`: read-only preflight using fresh detail content, returns `{ok: bool, reason: str}`; require `job_fingerprint(fresh) == job_fingerprint(job)` and actual matching recruiter/city; greet existing contact => ok false.
- `perform(job: dict, action: str, message: str, resume_name: str) -> dict`: repeat preflight then unique DOM action, return `{status: greeted|resume_sent|skipped|uncertain|blocked, detail: str}`. Never retry a send. Successful status requires visible new outgoing message/attachment. Must not automatically send a default greeting via immediate-chat navigation if site sends on click; treat chat-entry click as a side effect and verify resulting outgoing message. If action cannot safely send the exact approved message, return blocked before clicking.
- `close() -> None`.

`BrowserBlocked(RuntimeError)` with human-readable message. `job_fingerprint(job: dict) -> str` provided by store.py (available shortly); keys hashed: job_key,url,title,company,city,salary,experience,education,description,recruiter. Strip outer whitespace and collapse internal whitespace; description text changes are relevant; tags and collection times ignored.

Additional explicit action `start_chat`: user authorizes the BOSS account's currently configured default greeting (not custom message content). Preview must say clicking 立即沟通 may send that configured greeting; user should review it in BOSS before confirming. For this action only, a unique immediate-chat control on a freshly verified job may be clicked. A new visible outgoing message is still required for greeted status, record actual content; otherwise uncertain. Existing-contact jobs are skipped. `start_chat` and `greet` share the same persistent duplicate reservation category. `greet` must never fall back to platform default.

Dispatch safety: BrowserAdapter exposes optional synchronous `before_send` callback, invoked immediately before actual send clicks. A rejected guard returns `{status:paused,dispatched:false}`. All known pre-click blocked/skipped results carry `dispatched:false`; only those may release a persisted intent. Post-click uncertain/success never claim undispatched. First-contact success additionally needs an observed empty matching thread followed by a new outgoing message and a fresh explicit platform send receipt; existing history or receipt is insufficient. Worker pauses are sticky for the current run even if global pause is cleared; restart the remaining approved batch explicitly. Global attempt spacing is persisted across batches/restarts, and current filters/quota are rechecked at reservation and final dispatch.

Installed Mac runtime: `~/Library/Application Support/BOSS-Hiring/.venv`; state: sibling `state/`; deployment metadata: sibling `deployment.json`. Source CLI discovers this state when installed; MCP config explicitly supplies it and port 8771. The launch agent never reads the Documents source tree.

## API

All `/api/*` endpoints require `Authorization: Bearer TOKEN` (UI TOKEN injected into `__BOSS_API_TOKEN__` placeholder in index.html as JSON string, so `const API_TOKEN = __BOSS_API_TOKEN__;`). Response errors `{detail: string}`. JSON bodies required on POST/PUT. GET `/` serves the UI. JS/CSS at `/static/app.js` and `/static/style.css`.

- GET `/api/status` => `{paused: bool, active_task: object|null, browser: object, counts: object, daily_used: int, settings: object}`.
- GET `/api/settings` => settings. PUT `/api/settings` body `{cities, keywords, exclude_companies, daily_limit, interval_seconds, collect_limit}`; defaults cities 杭州、上海; keywords AI应用开发,大模型应用,Agent开发,智能体开发; daily_limit 20; interval_seconds 30; collect_limit 20. Allowed cities subset nonempty; keywords nonempty; reject unknown fields.
- POST `/api/browser/open` body `{}` => browser status.
- GET `/api/browser/screenshot` => image/png; authenticate with fetch and Blob URL, no token query param.
- POST `/api/collect` body `{}` => `{task_id: string}`; GET `/api/tasks` => array of `{id,kind,status,detail,created_at}` newest first.
- GET `/api/jobs?city=&q=` => array of stored jobs, fields above + `id: int,collected_at,eligible:bool,filter_reason:str,contact_status:str`.
- POST `/api/jobs/import` body `{jobs: [job dict]}` => `{imported: int}`; validates same rules as browser capture, useful for manual intake; not default seed data.
- POST `/api/batches` body `{job_ids:[int], action:start_chat|greet|resume, message:str, resume_name:str, mode:dry_run|live}` => full batch `{id:int,digest:str,status:draft,action,message,resume_name,mode,items:[{id,job_id,job:snapshot,status,detail}],created_at}`. For greet require nonempty exact approved message; for resume require exact existing attachment name. For start_chat use explicit platform-greeting behavior instead of promising custom text. IDs must be unique, nonempty, at most 100, and eligible.
- GET `/api/batches` => array full batches newest first. GET `/api/batches/{id}` => full batch.
- POST `/api/batches/{id}/approve` body `{digest:str, acknowledge_live:bool}` => updated batch. Live requires explicit acknowledgement. Dry run still requires review confirmation.
- POST `/api/batches/{id}/run` body `{}` => `{task_id:string}`. Only approved/paused batches; interrupted uncertain items are never repeated.
- POST `/api/pause` body `{}` / POST `/api/resume` body `{}` => `{paused: bool}`. Resume clears global pause only; run approved/paused batch separately.
- GET `/api/events?limit=100` => array `{id,created_at,kind,detail}`.

UI must clearly say keyword filtering is not résumé semantic matching. No fabricated résumé, fit score, job seed rows or success data. Empty states provide next action. Approval must show snapshot recipient/company/role/action/message/resume/mode, and require checkbox in live mode. Draft approval and run may be chained once user clicks the final confirmation button.
