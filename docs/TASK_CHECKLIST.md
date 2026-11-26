# Task Checklist

## 2026-11-20 Pending queue recovery (deployed)

- [x] Identified today's PENDING cause as a transient MathJax/editor metric
  mismatch followed by an over-aggressive full-day interaction gate.
- [x] Kept a real account cooldown and daily publish-attempt ledger while
  allowing unattended recovery after pre-publish failures.
- [x] Added cooldown preflight so retries do not consume task retry budgets
  while the account is cooling down; capped retry backoff at two hours.
- [x] Added serialized DOM render evidence for both draft and official-record
  formula verification.
- [x] Automatically retried and verified R106-01 through the official API and
  reconciled it to `PUBLISHED` without duplicate submission.

## 2026-11-19 Automatic topic selection (deployed)

- [x] Added authenticated Zhihu topic-autocomplete API requests to the
  publication workflow.
- [x] Ranked topic candidates by exact match, article-context inclusion, and
  token overlap; rejected candidates below the configured relevance threshold.
- [x] Added runtime settings for enable/disable, maximum topic count, and
  minimum relevance; defaults are enabled with `投资`, max 2, threshold 0.45.
- [x] Kept UI-state verification for selected topics and the target column,
  with an explicit failure instead of publishing when neither API nor current
  valid tags can satisfy the threshold.
- [x] Verified the live production endpoint returns topic candidates with the
  current Zhihu session; deployed backend restart completed and 176 tests pass.

## 2026-11-19 Two-day pending publication root cause (fixed)

- [x] Verified the server Zhihu session is valid; login was not the current blocker.
- [x] Reproduced the actual pre-publish failure: stale exact topic `量化研究` was not searchable in the current picker.
- [x] Reproduced the second UI issue: column radio remained `不发布到专栏` after a nominal Playwright `check()`.
- [x] Switched required topic to currently available `投资` and added real-state radio verification/fallback clicks.
- [x] Verified R106 without publishing: selected topic `投资`, `columnOn=true`, column `量化研究速递`.
- [x] Kept the queue safe; no duplicate or unverified submission was made.

## 2026-11-17 Run 108 session expiry (recovered)

- [x] Confirmed Run 108 had no public URL and was blocked by an expired Zhihu login session.
- [x] Restored only Zhihu-domain cookies from the authorized local Firefox profile; server writer returned HTTP 200 and signed-in editor.
- [x] Deleted local and remote temporary session files after verification.
- [x] Confirmed automatic queue order: R106-01, R107-01, R108-01; next window will retry R106-01 first.
- [x] Kept today's consumed interaction slot intact; no forced same-day retry or duplicate submission.

## 2026-11-16 Run 106/107 publication-state bug (fixed)

- [x] Confirmed R107-01 was `PENDING` with no public URL; the email did not correspond to a confirmed publication.
- [x] Found stale-draft identity risk: arbitrary `/p/{id}` links on the writer page could be mistaken for the new draft.
- [x] Removed that fallback and added response-header ID extraction; missing identity now fails closed.
- [x] Changed automatic digest subjects/body to distinguish verified `PUBLISHED` from queued/pending work.
- [x] Kept R106-01/R107-01 queued without forcing another same-day Zhihu interaction; 174 server tests pass.

## 2026-11-14 Run 105 arXiv/Kimi outage (diagnosed)

- [x] Confirmed Run 105 failed before article generation because arXiv returned HTTP 429/timeout.
- [x] Added the arXiv client `User-Agent`/Atom `Accept` headers and verified a live 40-candidate read.
- [x] Confirmed the independent provider blocker: Kimi Code weekly quota returns HTTP 429 `exceeded_current_quota_error`.
- [x] Kept the run failed and unpublished; no unverified or duplicate article was submitted.
- [x] Deployed the fix; backend and 07:00 Asia/Shanghai timer remain active/enabled; 172 backend tests pass.

## 2026-11-11 run 98 provider-quota incident (completed)

- [x] Read the console/API error and matched it to the production log: every model call returned Kimi HTTP 429 "5-hour usage limit" (not an article quality defect).
- [x] Fixed terminal handling for HTTP 429 and repeated transport failures: stop the run, mark it FAILED, keep the bounded daily retry, and never synthesize a quality rejection from an empty provider-degraded draft.
- [x] Added `provider_error.json` and excluded `llm_provider_unavailable` reports from rework backfill.
- [x] Fixed maintenance-worker starvation: bounded lock wait plus staggered scheduler start offsets so publication/revision/mail workers run when the one-minute catch-up probe holds the lock.
- [x] Allowed explicit operator checkpoint resumes (`manual=True, run_id=...`) to bypass automatic daily-capacity gates without bypassing verified-quality publication gates.
- [x] Added a bounded image-count settle retry for the official published-record check; content/formula/topic/column/image-identity checks stay strict.
- [x] Deployed the changes, restarted the backend, and passed 172 backend tests locally and on the server.
- [x] Repaired publication worker published the verified backlog R097-01; the first read was held `PUBLISH_UNCERTAIN` without resubmission.
- [x] Read-only owner-authenticated verification passed (exact published ID/title, prose and 48 formulas, 2 loaded figures with original identities, topics and column) and the task was reconciled to `PUBLISHED`.
- [x] Public URL confirmed: https://zhuanlan.zhihu.com/p/2103909683891279810.
- [x] Run 98 paper `2611.12389v1` retained in the rework queue with an honest HTTP 429 provider error; checkpoint resume is retained for a later quota window, and nothing unverified was published.

## Current unattended-operation acceptance (2026-11-10)

- [x] Reproduce and fix a busy daily job being silently lost.
- [x] Add date-based catchup, bounded daily retries, and startup recovery.
- [x] Give due daily generation priority over maintenance workers.
- [x] Fix duplicate publication codes for multiple revisions in one run.
- [x] Expose automation blockers and attempt email alerts for terminal failures.
- [x] Add and enable a 07:00 Asia/Shanghai systemd wakeup timer.
- [x] Pass 161 backend tests locally/on the server and the frontend production build.
- [x] Restore SSH access and inspect live server logs/settings.
- [x] Deploy and enable the new timer and matching backend/frontend code.
- [x] Migrate all four roles to Kimi K3.1 Pro and test text/JSON/image input.
- [x] Restore the server Zhihu editor login and retain refreshed sessions.
- [x] Generate a new Kimi article and pass strict content/math/figure verification.
- [x] Replace stale SMTP authorization, validate SSL delivery, and send the real attention email.
- [x] Reconcile R096-01 using the owner's confirmation and the official published record, without resubmission.
- [x] Fix the false account-wide breaker caused only by public HTML read restrictions.
- [x] Independently verify the published body, formula sequence, actual rendering, image lineage/load, topics and column.
- [x] Observe the real 2026-11-10 07:00 scheduler starting run 97 without any manual trigger or duplicate run.

The older completed items below describe historical checks, not current server
acceptance. The new server-timed acceptance run must complete its own checks.

## Done

- [x] Default paper count changed to 3.
- [x] Reading-note structure upgraded with detailed sections:
  - methodology
  - data description
  - empirical conclusions
- [x] LLM runtime config added (provider/base_url/key/model/prompts).
- [x] Frontend settings page updated to include all LLM fields.
- [x] Frontend parameter explanation section added for every configurable key.
- [x] Backend settings API updated to persist new LLM parameters.
- [x] Replaced one-shot note generation with `PaperIR -> EvidenceLedger -> ArticlePlan -> ArticleDocument`.
- [x] Added safe arXiv source extraction and TeX equation recovery.
- [x] Added real multimodal figure analysis, semantic selection, duplicate suppression, and claim anchors.
- [x] Added claim-level and paragraph-level independent verification.
- [x] Added deterministic numeric, provenance, formula, language, future-work, and asset gates.
- [x] Added targeted paragraph repair; extractive fallback remains diagnostic and cannot publish.
- [x] Added per-stage artifacts, latency metrics, frontend QA status, and single-paper validation CLI.
- [x] Preserved draft-only Zhihu defaults and existing draft readback validation.
- [x] Added whole-article consistency review and a publication gate requiring that review.
- [x] Prevented repeated cross-section claim/equation assignments and expanded inline TeX macros.
- [x] Retained draft URLs after failures and verified actual public article content after publishing.
- [x] Switched generation and independent review to `deepseek-ai/DeepSeek-V4-Flash`.
- [x] Added original TeX proof context, explicit proof ownership and final discourse review.
- [x] Preserved failed checkpoints and known findings across automatic repair rounds.
- [x] Verified real multi-image drafts, public article media, topics, and the target column.
- [x] Enabled the persistent daily workflow and publication retry scheduler on the server.
- [x] Confirmed three public articles through an independent public-page readback.

## Validation Steps

1. Start backend and frontend.
2. Open settings page and fill LLM endpoint/key/model/prompts.
3. Save settings.
4. Trigger workflow manually.
5. Check each publishable paper shows verified quality with both `article_context_verified=true` and `article_global_verified=true`; fallback or failed articles must remain blocked.
6. Inspect `qa_report.json`, `evidence_ledger.json`, and `article_document.json` in the paper artifact directory.
7. Confirm selected figures are adjacent to the claim they explain and appear exactly once.
8. Save a Zhihu draft and confirm text, formulas, image order, and image loading pass readback validation.
9. Confirm topic tags and the target column survive a draft reload.
10. After public publishing is enabled, verify the actual public body and media, not just its URL or HTTP status.
