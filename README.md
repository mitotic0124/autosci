# AutoSci: Daily Paper Workflow (3 Papers + Email Command Publishing)

This project is a frontend-backend separated AI workflow system:

1. Run every day at 07:00 Asia/Shanghai, fetch and rank arXiv quant-fin papers, and select 3 papers.
2. Build evidence-grounded Chinese interpretations with PDF/TeX parsing, multimodal figure review, claim provenance, independent verification, and deterministic asset placement.
3. Send paper codes by email.
4. Parse reply command `发布Rxxx-xx,Rxxx-xx` and publish selected notes to Zhihu column.

## Project Structure

```text
backend/    FastAPI + Scheduler + arXiv + LLM + SMTP/IMAP + Zhihu Publisher
frontend/   React + TypeScript control panel + workflow graph + settings help
infra/      Docker Compose
docs/       Architecture and task checklist
scripts/    Helper scripts
```

## Quick Start

### One-time setup

```powershell
Set-Location D:\autosci\backend
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
python -m pip install -r requirements.txt
python -m playwright install firefox
Set-Location D:\autosci\frontend
npm install
```

### Start the backend (PowerShell window 1)

```powershell
Set-Location D:\autosci\backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

### Start the frontend (PowerShell window 2)

```powershell
Set-Location D:\autosci\frontend
npm run dev
```

Visit:
- Frontend: `http://localhost:5173`
- Backend: `http://localhost:8000`

If Windows reports `[WinError 10013]`, first check whether an old backend still owns port 8000:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  Select-Object LocalAddress, LocalPort, OwningProcess
```

Stop only the confirmed stale process, then start the backend again:

```powershell
Stop-Process -Id <OwningProcess> -Force
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

For Zhihu session reuse, list this machine's Firefox profiles with:

```powershell
Get-ChildItem "$env:APPDATA\Mozilla\Firefox\Profiles" -Directory |
  Select-Object FullName
```

Set the selected signed-in profile in `backend/.env`, for example:

```dotenv
ZHIHU_FIREFOX_PROFILE_PATH=C:\Users\mitotic\AppData\Roaming\Mozilla\Firefox\Profiles\fnqvjplc.default-release
ZHIHU_HEADLESS=true
ZHIHU_AUTO_PUBLISH=false
```

## Required Configuration (`backend/.env`)

- Paper workflow: `PAPER_COUNT=3`
- LLM: `LLM_PROVIDER`, `LLM_API_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`
- Article pipeline: `LLM_ARTICLE_MODEL`, `LLM_VISION_MODEL`, `LLM_VERIFIER_MODEL`, `ARTICLE_*`
- SMTP: `SMTP_*`, `MAIL_FROM`, `MAIL_TO`
- IMAP: `IMAP_*`
- Command whitelist: `COMMAND_AUTHORIZED_SENDER`
- Zhihu publish: `ZHIHU_*`

As verified on 2026-11-10, the default model for ranking, article generation,
independent verification, and figure review is Kimi K3.1 Pro (`k3.1-pro`) through
`https://api.kimi.com/coding/v1`. This endpoint requires a Kimi Code key, not an
Open Platform key. Official model IDs and parameters are documented at
https://www.kimi.com/code/docs/kimi-code/models.html. K3 fixes its sampling
parameters; the adapter omits temperature and uses `max_completion_tokens` and
`LLM_REASONING_EFFORT=high`. The output budget is 16384 tokens with a 300-second
request timeout. The legacy DeepSeek adapter remains available for explicitly
configured providers but is not used by the deployed workflow.
Values saved in the control panel override `.env` and code defaults.

To validate a Kimi key and update all four roles, run from `backend/`:

```powershell
python ..\scripts\configure_kimi.py --key-file D:\autosci\kimikey.txt
```

The helper tests the API before updating `.env` and persisted settings, keeps
private backups outside the public artifacts directory, and never prints the
key. `--production` additionally enables 07:00 Asia/Shanghai public automation;
it does not install the server units or restore a missing Zhihu login.

## Main APIs

- `POST /api/workflow/trigger` trigger paper pipeline
- `GET /api/workflow/runs` list runs
- `GET /api/workflow/runs/{id}` run detail
- `GET /api/settings` / `PUT /api/settings` settings read/write
- `POST /api/publications/poll-mail` poll reply commands now
- `POST /api/publications/publish` publish by paper codes
- `GET /api/publications` list publication tasks

## Reply Command

- `发布R012-01,R012-03`
- `发布 R012-01，R012-03`

The system parses codes and publishes corresponding papers to your Zhihu column.

## Evidence-Grounded Article Pipeline

The production workflow no longer uses one-shot Markdown generation or size-only image selection. Each paper produces:

- `paper_ir.json`: sections, page-bound paragraphs, captioned figures, and TeX equations.
- `evidence_ledger.json`: independently verified claims with exact source block/page provenance.
- `article_plan.json`: claim-constrained sections and figure anchors.
- `article_document.json`: structured article AST used by the deterministic renderer.
- `qa_report.json`: gate status, metrics, repair warnings, and per-stage latency.
- `note.md`: final rendered article consumed by the preview and Zhihu draft adapter.

Figures are sent as actual pixels to `LLM_VISION_MODEL`. A figure is insertable only when it passes visual quality scoring, is not redundant, and is linked to a verified claim that explains the figure. The planner cannot place a figure after an unrelated claim. Reader-facing Chinese captions are translated from the original captions and independently verified; visual-model inferences are not used as final captions. Crops preserve long captions while excluding neighboring figures.

Generated paragraphs are checked against their bound claims and complete source excerpts by `LLM_VERIFIER_MODEL`, then reviewed in the context of the full article. References to tables and theorems include the same-page evidence. Original TeX table rows and theorem/assumption environments are preserved separately to resolve PDF reading-order errors. Repairs are reverified. With `ARTICLE_REQUIRE_VERIFIED=true`, missing verdicts, unresolved contradictions, non-Chinese output, or failed structural gates reject the article before publication. Rejected documents and quality reports remain in the run artifacts. Display equations are inserted only after the paragraph that explains them; bare inline subscripts and compound norms are normalized into renderable math before verification.

Strict verification saves the current document and a failed quality checkpoint before
each review round. Interrupted runs retain completed repairs for
`scripts/retry_article_verification.py`. A repair that leaves every affected
paragraph unchanged stops the run instead of repeating the same paid reviews.
Known unresolved findings survive retries and cannot disappear through an unchanged
paragraph receiving a later optimistic verdict.

The daily worker does not silently replace a rejected article with a different paper.
Each quality rejection first triggers up to `ARTICLE_MAX_QUALITY_RETRIES` complete
redrafts of the same paper. If those attempts still fail, the evidence checkpoint is
stored in `article_revision_tasks` and the dedicated 15-minute revision worker retries
it from the verified evidence before the next paper is considered. Only a verified
article creates a publication task; a failed checkpoint remains visible and cannot be
published by lowering the verification switch.

The configured Zhihu default remains draft-only:

```dotenv
ZHIHU_AUTO_PUBLISH=false
WORKFLOW_AUTO_PUBLISH_AFTER_RUN=false
```

Validate one stored paper without changing the database or creating a publication task:

```powershell
cd backend
python -B ..\scripts\validate_article_pipeline.py --arxiv-id 2607.09121v1
```

Use `--offline --no-source` to exercise the safe extractive fallback without network or LLM calls.

## VPS Deployment

The production layout uses Nginx on server loopback port `8080` and a single
FastAPI worker on loopback port `8000`. Only SSH needs to be reachable from the
Internet; use an SSH tunnel to access the unauthenticated control plane.

Open the tunnel from a local PowerShell window and keep it running:

```powershell
ssh -N `
  -o ExitOnForwardFailure=yes `
  -o ServerAliveInterval=30 `
  -L 8080:127.0.0.1:8080 `
  ubuntu@43.142.37.25
```

Then open `http://127.0.0.1:8080/` locally. If local port `8080` is already in
use, change only the first port, for example `-L 18080:127.0.0.1:8080`, and open
`http://127.0.0.1:18080/`.

Useful server operations:

```bash
sudo systemctl status autosci-backend nginx
sudo systemctl restart autosci-backend
sudo journalctl -u autosci-backend -n 100 --no-pager
curl http://127.0.0.1:8080/health
```

Deployment files are tracked in `infra/systemd/autosci-backend.service` and
`infra/nginx/autosci.conf`. The deployed application root is `/opt/autosci`.

The backend owns the recurring workflow. It does not require an open local browser,
an SSH tunnel, or an active Codex session to execute. Enable the systemd services
with `sudo systemctl enable --now autosci-backend nginx`; the backend unit restarts
after a process failure. The schedule and timezone saved in the control panel
determine each daily run. `/health` reports scheduler status and each next run time.

### Reliable daily execution (07:00 Beijing time)

The daily cron and a one-minute recovery check use the same durable run history.
Work that finds the workflow busy remains due, and a restart after 07:00 catches
up today's missed run. Completed generation runs are not repeated that day;
failed runs retry after 30 minutes, at most three attempts per local calendar
day. Partial quality failures stay in the dedicated revision queue. Maintenance
workers yield to a due daily run. Startup recovers interrupted generation and
revision jobs, but marks interrupted publication as uncertain rather than
resubmitting a possibly committed post. Keep exactly one backend worker.

For an independent OS-level 07:00 wakeup, install the new systemd units **after
deploying the matching backend code**:

```bash
sudo install -m 644 /opt/autosci/infra/systemd/autosci-daily.service /etc/systemd/system/
sudo install -m 644 /opt/autosci/infra/systemd/autosci-daily.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now autosci-backend autosci-daily.timer
sudo systemctl restart autosci-backend
systemctl list-timers autosci-daily.timer --no-pager
curl --fail http://127.0.0.1:8000/health
```

The timer explicitly uses `Asia/Shanghai` regardless of the server's system
timezone. `Persistent=true` wakes the service after downtime; the backend's
date guard prevents duplicate runs from the timer, cron, or recovery check.
`POST /api/workflow/scheduled` uses this date guard, unlike the manual trigger.
`/health.daily_workflow` reports today's attempt count, last run, and deferral
reason. The dashboard warns about disabled publication or missing configuration;
the service attempts an email alert when all quality candidates fail or all
three scheduled attempts are exhausted. Delivery status is stored in the run
summary; email failure never bypasses quality or publication safeguards.

The example environment remains draft-only intentionally. Production settings
must include `schedule_hour=7`, `schedule_minute=0`, `timezone=Asia/Shanghai`,
`workflow_auto_publish_after_run=true`, `zhihu_auto_publish=true`, and
`zhihu_publish_enabled=true`, plus valid model credentials and a signed-in
Firefox profile **on the server**. Database settings override `.env` values.
Passing local tests does not prove these production prerequisites or actual
public posting: check the live run, final `PUBLISHED` task, and public article
page after deployment.

Set both `zhihu_auto_publish=true` and `workflow_auto_publish_after_run=true`
after accepting automatically generated drafts. Every subsequent run performs
source parsing, evidence extraction, Chinese writing, independent verification,
draft save and reload checks, topic/column selection, and public-page verification.
The production draft gate compares every prose segment and the exact equation
sequence to the verified source, checks image counts and loaded media, and verifies
the saved title and publication settings. A transient media loading failure allows
one reload; missing or changed content remains blocked.
New-draft column controls can load only after the first saved-page reload; the
workflow performs that recovery once and then requires the selected column.
The public-page check repeats content, media, topic, and column validation after
publication. MathJax's red unknown-command nodes count as rendering failures.
If only the public HTML route restricts automated reads, the adapter can verify
the official owner-authenticated article record instead. This is not a skip:
the exact ID must be `published`, the complete prose and formula sequence must
match, and published topics/column, rendered owner content, loaded images, and
original image identities are all checked. Platform JPEG transformations are
matched using `data-original-token`, not by assuming identical image URLs.
A failed read alone no longer labels a successfully published account as banned.
Actual writer/publish API restrictions and uncertain submissions still stop work.
An empty set of new papers is a no-op; the history filter never republishes old
papers just to meet the configured count. Partial failures are reported as `PARTIAL`.

Every 15 minutes the backend considers both content revisions and automatically
requested publications. Quality revisions run first and use their persisted evidence
checkpoint; only after a verified revision is available can it enter the publication
queue. Publication retries are limited to pre-publish transient failures, respect the
persisted platform cooldown, and never touch `PUBLISH_UNCERTAIN` tasks. A persisted
account interaction interval (default 30 minutes) prevents repeated browser sessions
after a failed request, while the daily public-post cap remains one article.

For an acceptance run using stored original PDFs and TeX, without manual article
content, public posting, or email, run:

```bash
cd /opt/autosci/backend
/opt/autosci/venv/bin/python /opt/autosci/scripts/regenerate_articles.py --source-run 15
```

The command creates a new run and reruns the production article pipeline from
source. Its output run ID can then be passed to `save_validation_drafts.py`.

The final quality gate also audits attribution to prior work, scope of results,
conflicting numerical sources, and consistency of conclusions. Both
`article_context_verified` and `article_global_verified` must be true before a
publication task can enter the browser. An HTTP 401, 402, 403 or 429 (quota/rate
limit) from the model provider stops further model requests in that run, marks
the run `FAILED`, and reports the account/quota error; three consecutive
transport/5xx failures do the same. A provider outage is never recorded as an
article quality rejection or turned into a rework checkpoint. Replenish the
provider account or update the API configuration, then rerun the workflow;
account errors never trigger unverified fallback publishing.

Articles with figures additionally require `figure_captions_verified=true`.
Mathematical source checks reject JSON escape damage such as `ext{...}` or
`frac12` before rendering. The same gate runs at the Zhihu boundary, including
when reusing a previously saved draft. Original TeX tables and theorem statements
are supplied to drafting and final verification to preserve row/column and
assumption/result associations that can be lost in PDF text extraction.
Final figure positions are chosen against the actual article paragraphs and
verified captions. Page proximity cannot override a semantic figure association.
An unresolved paragraph defect persists across review rounds until the paragraph
changes and passes verification; an unchanged paragraph cannot pass merely because
a later model response overlooks the earlier defect.
Use `--refresh-figures` with the checkpoint retry helper after changing PDF crop
boundaries. `check_deployment_ui.py --expect-public` validates the live switches,
scheduler, desktop/mobile console, and browser errors without changing settings.

Automatic generation saves `pipeline_state.json` alongside the source, evidence,
draft and rejection report. `retry_article_verification.py` can create a new
verification run from a complete checkpoint without editing article content:

```bash
python /opt/autosci/scripts/retry_article_verification.py \
  --run-id CHECKPOINT_RUN --source-run METADATA_RUN --arxiv-id ARXIV_ID
```

If the checkpoint does not prove evidence extraction and drafting completed,
regenerate from the original PDF instead. All retries rerun the full production
quality gates and save only drafts until the publication settings are enabled.
