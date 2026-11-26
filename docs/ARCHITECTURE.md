# Architecture

## Overview

```text
React Frontend
  -> FastAPI Backend
       -> SQLite
       -> Artifact Storage (PDF/images/notes)
       -> APScheduler
            - configurable daily paper job (deployed at 07:00 Asia/Shanghai)
            - minute-level, date-guarded missed-run recovery
            - periodic IMAP polling
            - automatic publication retries every 15 minutes
            - persisted quality revision retries every 15 minutes
       -> Services
            - arXiv fetch/rank
            - PDF + arXiv TeX structural parsing
            - multimodal figure understanding
            - evidence extraction + independent verification
            - claim-constrained planning and section drafting
            - deterministic Markdown rendering + QA gates
            - SMTP code digest
            - IMAP command parser
            - Zhihu draft save/readback or explicit publish
```

## Workflow

1. Scheduler triggers daily job.
2. Fetch arXiv candidates and rank them. The client sends a stable `User-Agent`, keeps a minimum request interval, and rotates to the official HTTPS endpoint if the configured export endpoint fails.
3. Select top 3 papers.
4. Parse the PDF and optional arXiv source into `PaperIR`.
5. Analyze captioned figures with the configured vision model.
6. Extract claims in balanced section batches and independently verify each claim against exact source blocks.
7. Translate non-Chinese verified claims and verify the translations again.
8. Build `ArticlePlan`; claims and equations have one owning section, while figures may only anchor to directly related, page-local claims.
9. Draft sections from restricted evidence packets and independently verify every paragraph.
10. Review the complete article against source passages for inconsistent assumptions, asymptotic regimes, direction of effects, simulation/empirical distinctions, proof attribution, and repetition. Repairs receive the current section and neighboring prose for context. Repair and review again; unresolved issues block publication.
11. Apply deterministic number, provenance, formula, language, future-work, and asset gates, then render the verified `ArticleDocument` into Markdown. Expand source-defined TeX macros in both inline and display math.
12. Create publication codes and send the digest email.
13. If `workflow_auto_publish_after_run` is enabled, process qualified publication tasks automatically; otherwise wait for a console action or an authorized mailbox command.
14. Save selected notes as verified Zhihu drafts. Resolve topic names through Zhihu's authenticated topic-autocomplete API, rank candidates against the article title/context, select only candidates above the relevance threshold, and verify the selected tags and column after the UI state changes. Reload the saved draft and verify rendered math, loaded images, topic tags, and the selected column.
15. When `zhihu_auto_publish` is also enabled, publish the retained draft. Require a successful publication response and a real public article with matching title and body. A non-edit URL or HTTP 200 alone is insufficient; Zhihu can return a missing-article page with HTTP 200. A new draft identity must come from the current edit route or its save response; unrelated writer-page article links are never accepted.
16. A rejected article is first redrafted from its verified evidence checkpoint. The
    quality revision worker retries persisted checkpoints every 15 minutes and never
    creates a publication task until the strict gate passes.
17. Retry eligible automatic publication failures only before a confirmed publish
    response. Respect the persisted platform cooldown and account interaction interval;
    a rate limit or uncertain result does not trigger repeated publish clicks.

Production publication requires `mode=verified`, `passed=true`,
`metrics.article_context_verified=true`, `metrics.article_global_verified=true`,
and verified captions for all selected figures. Extractive fallback artifacts are
useful for diagnosis but cannot enter the publication queue. `QUALITY_BLOCKED`
and `SUPERSEDED` tasks cannot be released by disabling the verification setting.
Failed browser operations retain the saved draft URL for a later retry.

The verifier preserves original TeX tables, theorem statements, proof environments,
and their section titles, including optional short titles and nested formatting.
Every review round saves an explicitly failed checkpoint before model calls.
Known findings persist until the paragraph changes and passes complete review;
an unchanged repair stops the loop. Provider failures preserve both the repaired
document and unresolved findings.
Proof review distinguishes finite and infinite horizons, generator truncation and
horizon limits, and the indices held fixed in each construction. Neighboring
proofs with similar symbols cannot be combined into an unsupported causal chain.
The global verdict requires explicit results for attribution, scope, numerics,
conclusions, proof attribution, and discourse. Missing checks or contradictory
flags fail closed. Repairs produce final reader prose and must not refer to
deleted text or narrate earlier review findings.
A separate reader-continuity pass checks the final article without the long
source context. Its failures enter the same repair loop and publication gate.

## Configurable LLM

Frontend allows runtime configuration of:
- provider
- API endpoint
- API key
- model
- specialized article, vision, and verifier models
- request timeout, output limit, and Responses API capability
- source/claim/figure limits and quality threshold
- note system prompt
- scoring system prompt

Backend uses OpenAI-compatible client, so it can connect to different providers via endpoint/model settings.
When a model explicitly rejects JSON mode, its requests omit `response_format` while retaining strict JSON parsing and completeness checks. Authentication, connection, and unrelated API errors do not trigger that capability fallback.
All four deployed model roles use Kimi Code `k3.1-pro`, including image input.
K3 requests omit fixed sampling parameters, use `max_completion_tokens`, and
select `llm_reasoning_effort` (default `high`). The output cap is 16384 tokens
with a 300-second request timeout. The SiliconFlow-specific bounded-reasoning
adapter is retained only for explicitly selected legacy providers. Saved runtime
settings override deployment defaults.

The OS-level `autosci-daily.timer` independently wakes the backend at 07:00
Asia/Shanghai. Both that wakeup and APScheduler use persisted run history: a busy
job is still due, and interrupted work is recovered on startup. The deployment
has exactly one backend process; only that process owns workflow execution.
The quality and publication workers have staggered schedules to avoid repeatedly
losing the same lock race. Uncertain public posts are never submitted again
without reconciliation. Refreshed Zhihu cookies stay outside the public artifact
tree, in a private profile snapshot containing only Zhihu-domain cookies.

## Artifact Contract

Every successfully processed paper stores versioned JSON artifacts beside the PDF. Markdown is an output format, not the internal source of truth. The database schema remains backward compatible and stores final Markdown plus selected image URLs, while provenance and QA details remain in the artifact directory and run summary.

The old `NoteService` and size-based `PaperImageService` remain only for compatibility tests. `WorkflowService` does not instantiate or call them.
