# Architecture

## Overview

```text
React Frontend
  -> FastAPI Backend
       -> SQLite
       -> Artifact Storage (PDF/images/notes)
       -> APScheduler
            - daily paper job at 06:00
            - periodic IMAP polling
       -> Services
            - arXiv fetch/rank
            - LLM detailed note generation
            - SMTP code digest
            - IMAP command parser
            - Zhihu auto publish
```

## Workflow

1. Scheduler triggers daily job.
2. Fetch arXiv candidates and rank them.
3. Select top 3 papers.
4. Generate detailed reading notes with strict sections:
   - methodology
   - data description
   - empirical conclusions
5. Create publication codes and send digest email.
6. Poll mailbox for reply command `发布...`.
7. Publish selected notes to Zhihu and update status.

## Configurable LLM

Frontend allows runtime configuration of:
- provider
- API endpoint
- API key
- model
- note system prompt
- scoring system prompt

Backend uses OpenAI-compatible client, so it can connect to different providers via endpoint/model settings.
