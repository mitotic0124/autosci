# AutoSci: Daily Paper Workflow (3 Papers + Email Command Publishing)

This project is a frontend-backend separated AI workflow system:

1. Run every day at 06:00, fetch and rank arXiv quant-fin papers, and select 3 papers.
2. Generate structured detailed reading notes for each paper.
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

### Backend

```powershell
cd backend
copy .env.example .env
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Frontend

```powershell
cd frontend
npm install
npm run dev
```

Visit:
- Frontend: `http://localhost:5173`
- Backend: `http://localhost:8000`

## Required Configuration (`backend/.env`)

- Paper workflow: `PAPER_COUNT=3`
- LLM: `LLM_PROVIDER`, `LLM_API_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, prompts
- SMTP: `SMTP_*`, `MAIL_FROM`, `MAIL_TO`
- IMAP: `IMAP_*`
- Command whitelist: `COMMAND_AUTHORIZED_SENDER`
- Zhihu publish: `ZHIHU_*`

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
