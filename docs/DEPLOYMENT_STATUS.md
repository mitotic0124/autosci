# Deployment status

## 2026-11-20: Pending queue recovery and unattended retry fix

Run 111 produced a verified article, but the first automatic browser attempt
hit a transient Zhihu MathJax/editor-render metric mismatch. The old safety
gate then treated that pre-publish failure as a full-day account interaction,
so later retries were recorded as `PENDING` instead of recovering
automatically.

Deployed fixes:

- A failed pre-publish validation no longer consumes the whole local day.
  The account cooldown remains 30 minutes, and the separate daily public-post
  ledger still prevents duplicate or over-quota submissions.
- The retry worker checks the cooldown before consuming retry budget, permits
  same-day recovery, and caps exponential backoff at two hours.
- Zhihu's lazy MathJax rendering is driven node-by-node. When live editor
  metrics lag, a serialized DOM proof is required before accepting the render;
  the same proof is used during official published-record verification.
- Formula-rendering failures are classified as retryable automatic failures.

After deployment, R106-01 retried without a user message. The authenticated
official article API returned `state=published`, matching ID/title/body,
21 formulas, 2 images, topic `投资`, and column `量化研究速递`; the task was
reconciled to `PUBLISHED` at
`https://zhuanlan.zhihu.com/p/2106284777515111821`. The remaining queue is
still ordered and will be processed automatically in the next daily window;
no confirmation email is required.

## 2026-11-19: Authenticated topic API selection added

The publisher now resolves topics through Zhihu's authenticated
`/api/autocomplete/topics` endpoint instead of assuming that a configured
literal tag is still searchable. Candidates are ranked against the article
title and source context, filtered by a configurable relevance threshold, and
limited to the configured maximum count. The selected topic tags are still
verified in the real editor before the draft is saved or submitted; API
failure falls back to already-selected valid tags and never silently submits
an untagged article.

Runtime defaults are enabled with `投资` as the stable configured topic,
`ZHIHU_TOPIC_MAX_COUNT=2`, and `ZHIHU_TOPIC_MIN_RELEVANCE=0.45`. The API
request was verified on the production login session (HTTP 200, returning
`量化`, `量化交易`, `量化研究`, and `量化宽松`). The backend was restarted
with the change and the full **176-test** suite passes locally and on the
server. No manual message or confirmation is required for the next scheduled
workflow.

## 2026-11-19: Root cause of two-day pending queue found and fixed

The server was logged in correctly; this was not a login failure. The actual
blocker was a Zhihu editor UI/configuration mismatch:

- The account's current topic picker kept the existing `投资` tag, while the
  stored required tags were the no-longer-searchable exact names
  `量化研究,论文`. Publication stopped before the publish request with
  `知乎话题搜索未找到：量化研究`.
- The current editor's custom column radio could report a successful Playwright
  `check()` without changing React state. The page therefore remained on
  `不发布到专栏`, even though the column dropdown was visible.

Fixes deployed and verified against the saved R106 draft:

- Required topic changed to the account's currently available `投资` topic.
- Column selection now clicks the associated label/input, waits for the real
  checked state, and fails explicitly if `发布到专栏` is not enabled.
- R106 draft verification now reports `selectedTags=[投资]`,
  `columnOn=true`, and `columnText=量化研究速递` without clicking Publish.
- Server login remains valid (`session_error=""`); the failure was not an
  expired session.

The queued tasks remain safe and ordered (`R106-01` first). The next fresh
daily interaction will use the corrected topic and column flow; no manual
confirmation is needed.

## 2026-11-17: Run 108 login-session recovery and next-window readiness

Run 108 completed article verification but its oldest queued publication was
blocked because the server Zhihu session had expired. This was an actual
authentication failure, not a publish success or a public-page read restriction.
The local Firefox profile's Zhihu-domain session was explicitly authorized for
one recovery, transferred through a temporary file, verified against the writer
(`HTTP 200`, signed in, real editor), persisted to the private server profile,
and removed from both local and remote temporary locations.

Current readiness:

- `/health` reports `session_error=""`, `zhihu_rate_limit.active=false`, and
  automatic publication enabled.
- The automatic queue is exactly `R106-01`, `R107-01`, `R108-01`, in that
  order; all are `PENDING` with no public URL. The next daily window will try
  `R106-01` first.
- Today's interaction slot is already consumed, so no extra same-day browser
  request is forced. The next eligible automatic window is 2026-11-18 07:00
  Asia/Shanghai, with the refreshed session and stale-draft protection active.

The server backend and daily timer remain active/enabled; the 174-test suite
passed on the deployed code.

## 2026-11-16: Run 106/107 false-publication notification and stale-draft fix

Run 107 generated and quality-verified an article, but `R107-01` remained
`PENDING`; no confirmed public URL existed. The automatic digest was sent as a
queue/status message, but its previous subject and wording were too easy to
read as a publication confirmation. The actual publication queue also exposed
a safety bug: when Zhihu's writer page had links to an older article, the draft
resolver could select the first unrelated `/p/{id}` link as the new draft
identity. That caused the publisher to stop on the wrong old editor instead of
publishing the new task.

Deployed fixes:

- A new draft URL is accepted only from the current edit route or the draft
  save response (including `Location`/ID response headers). Arbitrary writer
  page links are never used as a new article identity.
- Missing draft identity now fails closed instead of risking an old article
  update or a duplicate publish.
- Automatic digest subjects and body now distinguish `PUBLISHED` with a
  verified URL from `PENDING`, `FAILED`, or queued tasks. A pending digest says
  explicitly that it is not a publication confirmation.
- R106/R107 remain safely queued; no new interaction is forced inside the same
  Zhihu daily interaction window. The next eligible window retries the oldest
  task first.

The server backend is active and the full **174-test** regression suite passes.

## 2026-11-14: Run 105 arXiv rate-limit diagnosis and fix

Run 105 did not reach article generation or publication. The first failure was
the arXiv API returning HTTP 429/timeout after three requests. The deployed
client now identifies itself with `User-Agent: AutoSci/1.0` and
`Accept: application/atom+xml`, preserves the official HTTPS fallback, and has
been verified against the live server with a single read-only query returning
40 candidates. A recent 72-hour candidate cache is also used when all API
attempts fail. The arXiv-specific regression suite and the full 172-test
backend suite pass on the server.

The current daily retry budget is intentionally exhausted after three bounded
attempts. A separate blocker is also present: Kimi Code is returning HTTP 429
with `exceeded_current_quota_error` (weekly usage limit). This is an account
quota condition, not an article-quality or Zhihu publication failure. The
workflow remains stopped rather than generating or publishing unverified text;
after the Kimi quota window resets or the account is upgraded, the next daily
window will retry automatically. The 07:00 Asia/Shanghai timer and backend are
active/enabled, and no publication was submitted for Run 105.

## 2026-11-11: run 98 provider-quota outage; publication worker starvation fixed; R097-01 published and verified

Run 98 (scheduled 2026-11-11 07:00 Asia/Shanghai) failed without publishing. The
console error was misleading: it showed article quality failures (0 paragraphs,
0 sections, 0 characters, non-Chinese title), but the production log shows every
model call returned HTTP 429 from Kimi:

```text
You've reached your 5-hour usage limit. Your quota will reset when the current
5-hour window ends. (type=exceeded_current_quota_error)
```

The old model adapter only treated 401/402/403 as terminal. The run therefore
kept issuing hundreds of failing calls, degraded to empty evidence, and was
recorded as a quality rejection in the persistent rework queue instead of a
provider outage. A separate defect kept publication retries from running at
all: the every-minute APScheduler catch-up probe and the maintenance workers
shared the same trigger second, and the non-blocking workflow lock made the
publication worker lose every time. The verified article R097-01 therefore
stayed `PENDING` for a day.

Fixes deployed and tested (172 backend tests pass locally and on the server):

- HTTP 429 (quota/rate limit) and three consecutive transport/5xx failures now
  raise a terminal `LLMProviderUnavailable`, stop all model requests in the run,
  and mark the run `FAILED` (no fabricated quality rejection, no false rework
  item). The bounded daily catch-up retry applies again.
- Provider-outage artifacts are written as `provider_error.json`; failed
  `qa_report.json` files carrying `llm_provider_unavailable` are excluded from
  rework backfill, so a quota window cannot become a permanent queue item.
- Worker locks use a bounded wait, and the maintenance jobs have staggered
  start offsets instead of firing on the catch-up probe's second. Publication
  retries and mail polling now execute when the daily workflow is idle.
- An explicit operator checkpoint resume (`retry_quality_revisions(manual=True,
  run_id=...)`) now bypasses automatic daily-capacity gates while still
  enforcing verified-article quality before any publication is queued.
- Published-record verification retries only the transient image-count lag seen
  when Zhihu's official article record has not yet attached all uploaded images;
  prose, formula, topic, column, and image-identity checks remain strict, and a
  persistent mismatch is still reported as `PUBLISH_UNCERTAIN` without
  resubmission.

Recovery of the verified backlog: R097-01 (run 97, `2611.04555v1`) was published
by the repaired worker. The first post-publish read observed an image-count lag
and the task was correctly held as `PUBLISH_UNCERTAIN` without a second submit.
A read-only owner-authenticated verification then passed completely:

- Official record: `state=published`, exact article ID and title, complete prose
  and 48-formula sequence, 2 loaded and decodable figures with original image
  identities, required topics `量化研究` / `论文` and column `量化研究速递`.
- Verified URL: `https://zhuanlan.zhihu.com/p/2103909683891279810`.
- The task was reconciled to `PUBLISHED`; no publish request was repeated.

Run 98's paper `2611.12389v1` remains in the persistent rework queue with an
honest provider error (task 30, attempts 1). The manual checkpoint resume was
attempted after publication; Kimi's quota window was still exhausted (HTTP 429),
and the attempt was retained for retry rather than converted into a quality
failure. A later unattended opportunity (a day without a verified article
queued) or an operator resume when quota is available will redraft it from
source; nothing unverified was published.

Deployment backup: `/opt/autosci/backups/run98-20261111/`. Production
acceptance evidence: `backend/artifacts/deployment-acceptance-20261111/`
(including `task97.json` and `published-r097/verification.json`).

## 2026-11-10: Kimi migration deployed; published article verified and false breaker fixed

After the supplied SSH key was bound to the instance, access to
`ubuntu@43.142.37.25` succeeded. Live history proved that daily 07:00 scheduling
was already firing: recent runs stopped with SiliconFlow HTTP 402, and the
publication queue separately contained expired Zhihu logins and duplicate-code
integrity failures. A running scheduler alone was not evidence of publication.

Current deployment:

- All four model roles use Kimi Code `k3.1-pro` at
  `https://api.kimi.com/coding/v1`. The provided key was validated against this
  endpoint; Open Platform endpoints do not accept that key. Text, structured
  JSON, and actual image-input calls passed. K3-specific fixed-temperature and
  reasoning parameters are handled by the adapter.
- Zhihu session recovery transferred only Zhihu-domain cookies from the
  explicitly configured local profile. The server editor returned HTTP 200 and
  a real contenteditable editor. Validated/refreshed cookies are retained in a
  private profile-side snapshot rather than discarded with temporary browsers.
- Production generates one verified article per day, with a one-public-post cap.
  Strict content, math, figure-caption, saved-draft, and public-page checks remain
  enabled. Work older than 30 days cannot jump ahead of a new article merely
  because a new workflow is creating publication tasks.
- `autosci-backend`, `nginx`, and `autosci-daily.timer` are running and enabled.
  The explicit OS timer is 07:00 Asia/Shanghai; systemd verified the next fire as
  2026-11-10 07:00 and the following fire as 2026-11-11 07:00.
  Observed live: run 97 was created by `scheduler` at 2026-11-10 07:00:00
  Beijing time, with exactly one daily attempt. The independent systemd wakeup
  also fired at 07:00 and correctly declined a duplicate because the scheduler
  had already started the workflow. No manual trigger was sent for this run.
- Date-guarded catchup, bounded retries, startup recovery, failure alerts, and
  maintenance priority are deployed. The quality worker is staggered from the
  publication worker and does not begin overnight work before the daily window.
- 161 backend tests passed locally and on the server. The production frontend
  built successfully; desktop/mobile checks found no JavaScript errors or mobile
  overflow. `/health` reports no automation configuration blockers.
- SMTP had a stale environment authorization while IMAP held a newer working
  authorization for the same mailbox. SMTP SSL authentication was validated
  with the current mailbox authorization, persisted in the database/environment,
  and an actual run-96 attention email was accepted by SMTP. Port 465 now uses
  implicit SSL directly instead of first timing out on a plaintext connection.

A one-shot systemd acceptance timer started run 96 at 06:00:46 on 2026-11-10.
It uses the ordinary trigger API (therefore its recorded source is `manual`).
Generation used Kimi throughout, without agent-written article content. The
first quality pass exposed an ineffective numeric repair: the model kept
percentages outside the exact evidence whitelist. The repair prompt now receives
the same whitelist used by the deterministic gate. After a controlled checkpoint
resume, R096-01 passed all production content, context, global, math and image
gates at 06:19:53, without lowering any quality switch. It contains about 5000
Chinese body characters, two figures, and six display equations.

At 06:21:15, the ordinary publisher had saved and reloaded the draft, then the
post-publish page returned Zhihu error 40362 (temporary access restriction).
The task was correctly held as `PUBLISH_UNCERTAIN` rather than submitted again.
The account owner subsequently confirmed the article was public. A read-only
server check established that the writer is accessible (HTTP 200 with a real
editor) and the official article API independently reports `state=published`.
Only automated reads of the public HTML route return HTTP 403. This was not an
account-wide publishing ban.

- Candidate/draft ID: `2103371452506436993`.
- Draft: `https://zhuanlan.zhihu.com/p/2103371452506436993/edit`.
- Confirmed public URL: `https://zhuanlan.zhihu.com/p/2103371452506436993`.
- Title: `arxiv论文速递|神经已实现测度：面向波动率预测的预测监督日内聚合`.

The task has been reconciled to `PUBLISHED`, without a second publication request.
The account-wide breaker was cleared only after the writer and full published
record checks passed. The account is not represented as globally "unblocked":
the public HTML read restriction still exists, and genuine write/publish API
rejections still trip the breaker.

The publisher now distinguishes a public-HTML read failure from a rejected
submission. In `published_api` mode it requires the exact observed article ID,
`state=published`, title and complete published prose/equation sequence, topics,
and column to match. It reloads the already-published owner editor to verify
actual math rendering and loaded media. Published JPEGs are independently loaded
and decoded, and their platform-provided `data-original-token` values must match
the owner editor's original uploaded image identities in the same order. This
handles Zhihu's PNG-to-JPEG URL changes without substituting mere image counts.

R096-01 passed this read-only check: 34 total inline/display formulas, zero broken
or unrendered formulas, two loaded figures, and the required topics and column.
`/health` now reports `zhihu_rate_limit.active=false` and
`public_readback_mode=published_api`. The console explains this verification mode;
desktop/mobile QA passed with no browser errors or overflow. No proxy, forged
identity, repeated publish attempt, or weakened content gate was used.

Reproduce the bound article check (read-only, from `backend/`):

```bash
PLAYWRIGHT_BROWSERS_PATH=/opt/autosci/ms-playwright /opt/autosci/venv/bin/python \
  ../scripts/verify_publication_result.py --official-record \
  --task-file artifacts/deployment-acceptance-20261110/task96.json \
  --output artifacts/deployment-acceptance-20261110/published-api
```

Backups and deployment evidence:

- `/opt/autosci/backups/automation-20261110/`: original code, consistent SQLite
  snapshot, environment, profile, units, and regression output (private directory).
- `/opt/autosci/backend/backups/kimi-config-20261109T215837Z/`: configuration
  migration backup (private directory).
- `/opt/autosci/backend/artifacts/deployment-acceptance-20261110/`: console
  screenshots and non-secret runtime verification.
  `seven-am.json` records the live 07:00 scheduler trigger. Its observed next
  daily run is 2026-11-11 07:00 Asia/Shanghai.

Private keys and the provided Kimi key file are excluded from Git. No private
key, API key, or cookie value was printed in the investigation output.

## Historical deployment report: 2026-10-09

The persistent workflow is deployed at `/opt/autosci` on `43.142.37.25`.
`autosci-backend` and `nginx` are active and enabled at boot. The daily workflow
runs at 07:00 Asia/Shanghai; the next run is 2026-10-10 07:00. The server does not
depend on a local browser, an SSH tunnel, or Codex remaining open.

## Active configuration

- Generation, article writing, and independent verification:
  `deepseek-ai/DeepSeek-V4-Flash`.
- Figure analysis: `Qwen/Qwen3-VL-32B-Instruct`.
- Live model requests returned HTTP 200 after the provider balance was replenished.
- `workflow_auto_publish_after_run=true`.
- `zhihu_auto_publish=true`.
- `article_require_verified=true`.
- Required topics: `量化研究`, `论文` (the account's current topic picker does not return `论文解读`).
- Required column: `量化研究速递`.
- Model request timeout: 300 seconds. A request may retry once.
- Independent content repair: at most four repairs, with complete review after
  each repair. An unchanged repair does not erase an earlier finding.
- A quality rejection first receives two same-paper full redrafts before fallback
  selection. Remaining checkpoints are persisted in `article_revision_tasks` and
  retried by a dedicated worker every 15 minutes.
- Automatic publication retries run every 15 minutes, respect the persisted
  platform cooldown, and the account-level interaction interval is 1800 seconds.
- The Zhihu breaker was cleared only after read-only checks returned HTTP 200 for
  both `/write` and the previously uncertain article edit page. Current `/health`
  reports `zhihu_rate_limit.active=false`.

The two public-posting switches were persisted and read back through `/health`.
The backend was restarted after deploying the source parser, mathematical
conversion, figure insertion, and proof-attribution review fixes.

## Confirmed publications

These automatically generated articles passed content verification, saved-draft
reload checks, publication through the running service, and an independent public
page inspection. No article text was manually rewritten for this acceptance.

| Code | Article | Body characters | Equations | Images | Public page |
| --- | --- | ---: | ---: | ---: | --- |
| R024-01 | 高维因子模型中的主成分误差 | 4201 | 70 | 3 | https://zhuanlan.zhihu.com/p/2084489848891629645 |
| R035-01 | 利用预测市场对冲加密货币波动性 | 4577 | 12 | 3 | https://zhuanlan.zhihu.com/p/2084650338057991148 |
| R038-01 | 无模型被动执行：基于订单级影子跟踪的基准方法 | 5502 | 32 | 6 | https://zhuanlan.zhihu.com/p/2084665663126239212 |

All three public pages contain the required topics and link to the selected
column. The inspections found zero unrendered equations, broken images, or
missing prose. Equation identity and ordering are compared with the verified
Markdown, not only counted. The figure counts on the public pages match the
selected, caption-verified source figures.

The theoretical G-BSDE article is undergoing a separate full verification with
original TeX proof context. Its previous versions R039-01 and R040-01 have been
blocked or superseded; they are not part of the accepted publication batch.
Newly generated work must independently pass all gates before publishing.

## Verification and changes

- 118 backend regression tests passed locally and on the server.
- TypeScript checking and the Vite production build passed.
- The deployed desktop and 390-pixel mobile console loaded without JavaScript
  errors or horizontal overflow; the mobile workflow shows every step.
- The console displays the running scheduler, enabled automatic publication,
  next daily run, runtime model settings, and per-paper quality results.
- Formula conversion handles whitespace before an inline math closing delimiter
  and normalizes unsupported text commands. JSON escape damage and MathJax
  error nodes block publication.
- Figure insertion synchronizes the Draft.js selection before inserting the
  next image. The six-image article was saved, reloaded, and checked successfully.
- Original TeX parsing retains optional and nested section headings and proof
  environments, allowing audits to separate neighboring theorem constructions.
- Global review checks source attribution, assumptions, numerical discrepancies,
  proof ownership, and conclusions. Repairs receive section and neighboring
  paragraph context, and are always reverified.
- A separate reader-continuity pass receives only the final article so that
  stale references and correction commentary cannot hide in long source context.
- Unresolved content defects, incomplete model responses, or provider account
  errors cannot fall back to unverified publishing.

Evidence on the server:

- `backend/artifacts/deployment-regression-20260919.log`
- `backend/artifacts/publication-acceptance-flash.log`
- `backend/artifacts/publication-pages-flash.log`
- `backend/artifacts/zhihu_acceptance/R024-01-public.json` (also R035 and R038)
- `backend/artifacts/deployment-acceptance/verification.json`
- Per-paper `qa_report.json`, original PDF/TeX, evidence ledger, article plan,
  and final article document under `backend/artifacts/runs/`.

Downloaded acceptance evidence is in
`D:\autosci\artifacts\acceptance-recharged-20260919`.

## Local console and operations

Run this in PowerShell and keep the window open while using the console:

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 18080:127.0.0.1:8080 ubuntu@43.142.37.25
```

Open `http://127.0.0.1:18080/`. If a tunnel is already running on that port, reuse
it. Closing the tunnel closes local access only; the server workflow continues.

Service status and live logs from another PowerShell window:

```powershell
ssh ubuntu@43.142.37.25 "systemctl is-active autosci-backend nginx; systemctl is-enabled autosci-backend nginx"
ssh -t ubuntu@43.142.37.25 "sudo journalctl -u autosci-backend -f"
```

Restart after a deployment:

```powershell
ssh -t ubuntu@43.142.37.25 "sudo systemctl restart autosci-backend"
```

Nginx listens on server loopback `8080`; the single FastAPI worker listens on
loopback `8000`. Runtime settings are stored in the database and take precedence
over deployment defaults.
