# Backend Architecture

> FastAPI + Python 3.13+ | Postgres via Supabase (SQLAlchemy 2.0 async + asyncpg) | LiteLLM multi-provider

## Directory Structure

```
apps/backend/app/
├── main.py              # FastAPI entry point (lifespan: legacy-key fold-in)
├── config.py            # Pydantic settings; encrypted API-key read/write
├── crypto.py            # Fernet encrypt/decrypt for API keys at rest
├── auth.py              # Supabase bearer-token verification (get_current_user_id)
├── database.py          # Async SQLAlchemy/Postgres facade (returns plain dicts)
├── models.py            # SQLAlchemy declarative Base + ORM models
├── db_engine.py         # Postgres engine/session factories (async + sync)
├── llm.py               # LiteLLM multi-provider
├── pdf.py               # Playwright PDF rendering
├── routers/             # API endpoints (health, config, resumes, jobs, applications, enrichment, auth)
├── services/            # parser.py, improver.py, cover_letter.py
├── schemas/             # Pydantic models (models.py, applications.py)
├── scripts/             # migrate_sqlite_to_supabase.py (one-time per-account backfill)
└── prompts/templates.py # LLM prompts
```

## API Endpoints

### Health & Status
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/health` | Liveness probe (no LLM call) |
| GET | `/api/v1/status` | Full system status (LLM probe + DB stats, each isolated → 200 with degraded state on partial failure) |

### Configuration
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET/PUT | `/api/v1/config/llm-api-key` | LLM config (no longer persists a key) |
| POST | `/api/v1/config/llm-test` | Test connection |
| GET/POST/DELETE | `/api/v1/config/api-keys` | Per-provider encrypted API keys |

### Resumes
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/resumes/upload` | Upload PDF/DOCX |
| GET | `/resumes?resume_id=` | Fetch resume |
| GET | `/resumes/list` | List all |
| POST | `/resumes/improve` | Tailor for job (LLM) |
| PATCH | `/resumes/{id}` | Update |
| GET | `/resumes/{id}/pdf` | Download PDF |
| DELETE | `/resumes/{id}` | Delete |

### Jobs
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/jobs/upload` | Store job description |
| GET | `/jobs/{id}` | Fetch job |

### Applications (Kanban tracker)
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/applications` | All cards grouped by column (7 status keys) |
| POST | `/applications` | Manual add (creates job + card) |
| GET | `/applications/{id}` | Card + JD + resume (resume null if deleted) |
| PATCH | `/applications/{id}` | Update status/position/notes/company/role |
| PATCH | `/applications/bulk` | Move many cards to one column |
| DELETE | `/applications/{id}` | Delete one card |
| POST | `/applications/bulk-delete` | Delete many cards |

## Database (`database.py`, `models.py`, `db_engine.py`)

Postgres via Supabase, per-account Row-Level Security. For the engine/session plumbing,
tables, invariants, encrypted-key storage, and legacy-data migration, see
[`backend-guide.md`](backend-guide.md#database-operations) (day-to-day facade usage) and
[`supabase-auth-migration.md`](../features/supabase-auth-migration.md) (the authoritative
RLS/auth architecture) — not duplicated here to avoid drift.

## LLM Integration (`llm.py`)

**Providers:** OpenAI, Anthropic, Gemini, DeepSeek, OpenRouter, Ollama

```python
await check_llm_health(config)     # 30s timeout
await complete(prompt, ...)        # 120s timeout
await complete_json(prompt, ...)   # 180s timeout, JSON mode + retries
```

**Key Features:**
- API keys passed directly (avoids os.environ race conditions)
- Auto JSON mode for supported providers
- 2 retries with lower temperature
- Bracket-matching JSON extraction

## Services

### Parser (`services/parser.py`)
```python
await parse_document(content, filename) → str  # PDF/DOCX → Markdown
await parse_resume_to_json(markdown) → dict    # LLM call
```

### Improver (`services/improver.py`)
```python
await extract_job_keywords(job_desc) → dict    # LLM call
await improve_resume(original, job, keywords)  # LLM call
```

### Cover Letter (`services/cover_letter.py`)
```python
await generate_cover_letter(resume, job) → str    # LLM call
await generate_outreach_message(resume, job) → str # LLM call
```

## PDF Rendering (`pdf.py`)

Uses Playwright headless Chromium:

```python
await render_resume_pdf(url, page_size, selector=".resume-print")
```

**Critical:** CSS must whitelist print classes in `globals.css`:
```css
@media print {
  body * { visibility: hidden !important; }
  .resume-print, .resume-print * { visibility: visible !important; }
}
```

## Configuration

```bash
LLM_PROVIDER=openai|anthropic|gemini|deepseek|openrouter|ollama
LLM_MODEL=gpt-5-nano-2025-08-07
LLM_API_KEY=sk-...
FRONTEND_BASE_URL=http://localhost:3000
```

Non-secret config (provider/model/base/features) stored in `data/config.json`, takes
precedence over env vars. **API keys are never written to `config.json`** — they live
encrypted in the Postgres `api_keys` table (per-provider, per-account) and are injected
into the config dict only at read time. Set them via `POST /config/api-keys`; `PUT
/config/llm-api-key` no longer persists a key.

## Error Handling

Log detailed errors server-side, return generic messages to clients:
```python
except Exception as e:
    logger.error(f"Failed: {e}")
    raise HTTPException(500, "Operation failed. Please try again.")
```
