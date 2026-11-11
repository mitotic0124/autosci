# Task Checklist

## 2026-11-11 run 98 provider-quota incident (completed)

- [x] Read the console/API error and matched it to the production log: every model call returned Kimi HTTP 429 "5-hour usage limit" (not an article quality defect).
- [x] Fixed terminal handling for HTTP 429 and repeated transport failures: stop the run, mark it FAILED, keep the bounded daily retry, and never synthesize a quality rejection from an empty provider-degraded draft.
- [x] Added `provider_error.json` and excluded `llm_provider_unavailable` reports from rework backfill.
- [x] Fixed maintenance-worker starvation: bounded lock wait plus staggered scheduler start offsets so publication/revision/mail workers run when the one-minute catch-up probe holds the lock.
- [x] Allowed explicit operator checkpoint resumes (`manual=True, run_id=...`) to bypass automatic daily-capacity gates without bypassing verified-quality publication gates.
- [x] Added a bounded image-count settle retry for the official published-record check; content/formula/topic/column/image-identity checks stay strict.
- [x] Deployed the changes, restarted the backend, and passed 170 backend tests locally and on the server.
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
