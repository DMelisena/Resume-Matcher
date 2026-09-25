"""One-time, operator-run backfill: legacy per-install SQLite -> Supabase Postgres.

The pre-Supabase-Auth app was single-tenant: one SQLite file (``data/
resume_matcher.db``) held every resume/job/improvement/application/api_key
with no owner. Supabase Auth introduces per-account ownership, so this
migration cannot run automatically — an operator must decide which Supabase
Auth account owns that installation's pre-existing data, then run this once:

    uv run python -m app.scripts.migrate_sqlite_to_supabase \\
        --sqlite-path data/resume_matcher.db \\
        --owner-user-id <supabase-auth-user-uuid>

It is idempotent per target database: rows are inserted with their original
primary keys, so re-running after a partial failure skips rows that already
exist (ON CONFLICT DO NOTHING) rather than duplicating them. It does NOT
delete the source SQLite file — verify the data landed correctly before
removing it yourself.

Requires DATABASE_URL (the app's normal Postgres connection, see
app/config.py) to already point at the target Supabase project with the RLS
migration applied (supabase/migrations/..._user_owned_tables_and_rls.sql).
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.config import settings
from app.db_engine import make_sync_engine

logger = logging.getLogger(__name__)


def _rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(f"SELECT * FROM {table}")  # noqa: S608 - fixed allowlisted table names
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in cursor.fetchall()]


def migrate(sqlite_path: Path, owner_user_id: str, database_url: str | None = None) -> dict[str, int]:
    """Copy every row from the legacy SQLite file into Postgres, owned by ``owner_user_id``."""
    UUID(owner_user_id)  # fail fast on a malformed id rather than a cryptic FK error

    src = sqlite3.connect(str(sqlite_path))
    counts = {"resumes": 0, "jobs": 0, "improvements": 0, "applications": 0, "api_keys": 0}

    engine = make_sync_engine(database_url or settings.sync_database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL ROLE authenticated"))
            conn.execute(
                text("SELECT set_config('request.jwt.claims', :claims, true)"),
                {"claims": json.dumps({"sub": owner_user_id, "role": "authenticated"})},
            )

            for row in _rows(src, "resumes"):
                conn.execute(
                    text(
                        "INSERT INTO resumes (resume_id, user_id, content, content_type, "
                        "filename, is_master, parent_id, processed_data, processing_status, "
                        "cover_letter, outreach_message, interview_prep, title, "
                        "original_markdown, created_at, updated_at) VALUES "
                        "(:resume_id, :user_id, :content, :content_type, :filename, "
                        ":is_master, :parent_id, :processed_data, :processing_status, "
                        ":cover_letter, :outreach_message, :interview_prep, :title, "
                        ":original_markdown, :created_at, :updated_at) "
                        "ON CONFLICT (resume_id) DO NOTHING"
                    ),
                    {
                        "resume_id": row["resume_id"],
                        "user_id": owner_user_id,
                        "content": row["content"],
                        "content_type": row.get("content_type") or "md",
                        "filename": row.get("filename"),
                        "is_master": bool(row.get("is_master")),
                        "parent_id": row.get("parent_id"),
                        "processed_data": row.get("processed_data"),
                        "processing_status": row.get("processing_status") or "pending",
                        "cover_letter": row.get("cover_letter"),
                        "outreach_message": row.get("outreach_message"),
                        "interview_prep": row.get("interview_prep"),
                        "title": row.get("title"),
                        "original_markdown": row.get("original_markdown"),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    },
                )
                counts["resumes"] += 1

            for row in _rows(src, "jobs"):
                conn.execute(
                    text(
                        "INSERT INTO jobs (job_id, user_id, content, resume_id, created_at, "
                        "metadata_json) VALUES (:job_id, :user_id, :content, :resume_id, "
                        ":created_at, :metadata_json) ON CONFLICT (job_id) DO NOTHING"
                    ),
                    {
                        "job_id": row["job_id"],
                        "user_id": owner_user_id,
                        "content": row["content"],
                        "resume_id": row.get("resume_id"),
                        "created_at": row["created_at"],
                        "metadata_json": row.get("metadata_json") or "{}",
                    },
                )
                counts["jobs"] += 1

            for row in _rows(src, "improvements"):
                conn.execute(
                    text(
                        "INSERT INTO improvements (request_id, user_id, original_resume_id, "
                        "tailored_resume_id, job_id, improvements, created_at) VALUES "
                        "(:request_id, :user_id, :original_resume_id, :tailored_resume_id, "
                        ":job_id, :improvements, :created_at) ON CONFLICT (request_id) DO NOTHING"
                    ),
                    {
                        "request_id": row["request_id"],
                        "user_id": owner_user_id,
                        "original_resume_id": row["original_resume_id"],
                        "tailored_resume_id": row["tailored_resume_id"],
                        "job_id": row["job_id"],
                        "improvements": row.get("improvements") or "[]",
                        "created_at": row["created_at"],
                    },
                )
                counts["improvements"] += 1

            for row in _rows(src, "applications"):
                conn.execute(
                    text(
                        "INSERT INTO applications (application_id, user_id, job_id, resume_id, "
                        "master_resume_id, status, company, role, applied_at, notes, position, "
                        "created_at, updated_at) VALUES (:application_id, :user_id, :job_id, "
                        ":resume_id, :master_resume_id, :status, :company, :role, :applied_at, "
                        ":notes, :position, :created_at, :updated_at) "
                        "ON CONFLICT (application_id) DO NOTHING"
                    ),
                    {
                        "application_id": row["application_id"],
                        "user_id": owner_user_id,
                        "job_id": row["job_id"],
                        "resume_id": row["resume_id"],
                        "master_resume_id": row.get("master_resume_id"),
                        "status": row.get("status") or "applied",
                        "company": row.get("company"),
                        "role": row.get("role"),
                        "applied_at": row.get("applied_at"),
                        "notes": row.get("notes"),
                        "position": row.get("position") or 0,
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    },
                )
                counts["applications"] += 1

            for row in _rows(src, "api_keys"):
                conn.execute(
                    text(
                        "INSERT INTO api_keys (id, user_id, provider, ciphertext, updated_at) "
                        "VALUES (gen_random_uuid()::text, :user_id, :provider, :ciphertext, "
                        ":updated_at) ON CONFLICT (user_id, provider) DO NOTHING"
                    ),
                    {
                        "user_id": owner_user_id,
                        "provider": row["provider"],
                        "ciphertext": row["ciphertext"],
                        "updated_at": row["updated_at"],
                    },
                )
                counts["api_keys"] += 1
    finally:
        src.close()
        engine.dispose()

    logger.info("Backfilled legacy SQLite data for owner %s: %s", owner_user_id, counts)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-path", type=Path, required=True)
    parser.add_argument("--owner-user-id", type=str, required=True)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    if not args.sqlite_path.exists():
        raise SystemExit(f"No such file: {args.sqlite_path}")
    counts = migrate(args.sqlite_path, args.owner_user_id)
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
