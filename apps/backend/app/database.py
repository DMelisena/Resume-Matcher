"""SQLAlchemy (Postgres/Supabase) data layer for Resume Matcher.

The ``Database`` facade keeps stable method names/signatures and returns
**plain dicts** (never ORM rows). Every method that touches a user-owned
table now takes ``user_id`` (the Supabase ``auth.users.id`` of the caller,
verified upstream by ``app/auth.py``) and:

1. Sets the Postgres ``request.jwt.claims`` GUC for the transaction, so the
   RLS policies' ``auth.uid()`` calls resolve to this user — the
   **authoritative** isolation boundary (see ``supabase/migrations/``).
2. Also filters/sets ``user_id`` directly in the query — defense-in-depth, so
   a missing or misconfigured policy can never silently leak another
   account's rows.

Two engines back one Postgres database:
- an **async** engine (``asyncpg``) for the document tables and applications;
- a **sync** engine for the encrypted ``api_keys`` table, which is read on the
  synchronous LLM hot path (``get_llm_config`` → ``resolve_api_key``).
"""

import asyncio
import json
import logging
import shutil
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db_engine import init_models_sync, make_async_engine, make_sync_engine
from app.models import ApiKey, Application, Improvement, Job, Resume

logger = logging.getLogger(__name__)

# Columns that are first-class on the jobs table; everything else the pipeline
# attaches dynamically is stored in ``metadata_json``.
_JOB_CORE_FIELDS = frozenset({"job_id", "content", "resume_id", "created_at"})

# Application status columns (stable keys, decoupled from i18n labels).
APPLICATION_STATUSES: tuple[str, ...] = (
    "saved",
    "applied",
    "no_response",
    "response",
    "interview",
    "accepted",
    "rejected",
)


def _now() -> str:
    """Current UTC time as an ISO-8601 string (TinyDB-era format)."""
    return datetime.now(timezone.utc).isoformat()


def _jwt_claims_for(user_id: str) -> str:
    """Build the ``request.jwt.claims`` payload RLS policies' ``auth.uid()`` read."""
    return json.dumps({"sub": str(user_id), "role": "authenticated"})


class Database:
    """Async SQLAlchemy facade for resume matcher data, scoped per account."""

    # Serializes concurrent master-resume promotion *within this process*.
    # Stays the primary mechanism for the single-master-per-user invariant;
    # the partial unique index is a storage-level backstop.
    _master_resume_lock = asyncio.Lock()

    def __init__(self, database_url: str | None = None):
        self.async_database_url = database_url or settings.async_database_url
        self.sync_database_url = (
            settings.sync_database_url if database_url is None else database_url
        )
        self._async_engine = None
        self._async_session_factory: async_sessionmaker[AsyncSession] | None = None
        self._sync_engine = None
        self._sync_session_factory: sessionmaker[Session] | None = None
        self._initialized = False

    # -- engine / session plumbing ------------------------------------------

    def _ensure_initialized(self) -> None:
        """Create engines and tables once (idempotent)."""
        if self._initialized:
            return
        self._sync_engine = make_sync_engine(self.sync_database_url)
        self._sync_session_factory = sessionmaker(self._sync_engine, expire_on_commit=False)
        init_models_sync(self._sync_engine)
        self._async_engine = make_async_engine(self.async_database_url)
        self._async_session_factory = async_sessionmaker(
            self._async_engine, expire_on_commit=False
        )
        self._initialized = True

    @property
    def _session(self) -> async_sessionmaker[AsyncSession]:
        self._ensure_initialized()
        assert self._async_session_factory is not None
        return self._async_session_factory

    @property
    def _sync(self) -> sessionmaker[Session]:
        self._ensure_initialized()
        assert self._sync_session_factory is not None
        return self._sync_session_factory

    @asynccontextmanager
    async def _user_session(self, user_id: str) -> AsyncIterator[AsyncSession]:
        """Open an async session scoped (for this transaction only) to ``user_id``.

        The connection logs in as ``resume_matcher_app`` (see the migration),
        which has no privileges of its own; ``SET LOCAL ROLE authenticated``
        adopts the RLS-subject role only for this transaction, and setting
        ``request.jwt.claims`` is what makes the policies' ``auth.uid()``
        resolve to ``user_id``. Both reset automatically at commit/rollback.
        """
        async with self._session() as session:
            await session.execute(text("SET LOCAL ROLE authenticated"))
            await session.execute(
                text("SELECT set_config('request.jwt.claims', :claims, true)"),
                {"claims": _jwt_claims_for(user_id)},
            )
            yield session

    @contextmanager
    def _user_sync_session(self, user_id: str) -> Iterator[Session]:
        """Open a sync session scoped (for this transaction only) to ``user_id``. See `_user_session`."""
        with self._sync() as session:
            session.execute(text("SET LOCAL ROLE authenticated"))
            session.execute(
                text("SELECT set_config('request.jwt.claims', :claims, true)"),
                {"claims": _jwt_claims_for(user_id)},
            )
            yield session

    async def close(self) -> None:
        """Dispose engines and release connections."""
        if self._async_engine is not None:
            await self._async_engine.dispose()
            self._async_engine = None
            self._async_session_factory = None
        if self._sync_engine is not None:
            self._sync_engine.dispose()
            self._sync_engine = None
            self._sync_session_factory = None
        self._initialized = False

    # -- row -> dict converters ---------------------------------------------

    @staticmethod
    def _resume_to_dict(row: Resume) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "resume_id": row.resume_id,
            "content": row.content,
            "content_type": row.content_type,
            "filename": row.filename,
            "is_master": row.is_master,
            "parent_id": row.parent_id,
            "processed_data": row.processed_data,
            "processing_status": row.processing_status,
            "cover_letter": row.cover_letter,
            "outreach_message": row.outreach_message,
            "interview_prep": row.interview_prep,
            "title": row.title,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        # Preserve TinyDB absence semantics: omit the key entirely when None.
        if row.original_markdown is not None:
            doc["original_markdown"] = row.original_markdown
        return doc

    @staticmethod
    def _job_to_dict(row: Job) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "job_id": row.job_id,
            "content": row.content,
            "resume_id": row.resume_id,
            "created_at": row.created_at,
        }
        meta = row.metadata_json or {}
        if isinstance(meta, dict):
            doc.update(meta)  # flatten dynamic fields to top level
        return doc

    @staticmethod
    def _improvement_to_dict(row: Improvement) -> dict[str, Any]:
        return {
            "request_id": row.request_id,
            "original_resume_id": row.original_resume_id,
            "tailored_resume_id": row.tailored_resume_id,
            "job_id": row.job_id,
            "improvements": row.improvements,
            "created_at": row.created_at,
        }

    @staticmethod
    def _application_to_dict(row: Application) -> dict[str, Any]:
        return {
            "application_id": row.application_id,
            "job_id": row.job_id,
            "resume_id": row.resume_id,
            "master_resume_id": row.master_resume_id,
            "status": row.status,
            "company": row.company,
            "role": row.role,
            "applied_at": row.applied_at,
            "notes": row.notes,
            "position": row.position,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    # -- Resume operations --------------------------------------------------

    async def create_resume(
        self,
        user_id: str,
        content: str,
        content_type: str = "md",
        filename: str | None = None,
        is_master: bool = False,
        parent_id: str | None = None,
        processed_data: dict[str, Any] | None = None,
        processing_status: str = "pending",
        cover_letter: str | None = None,
        outreach_message: str | None = None,
        title: str | None = None,
        original_markdown: str | None = None,
        interview_prep: str | None = None,
    ) -> dict[str, Any]:
        """Create a new resume entry owned by ``user_id``.

        processing_status: "pending", "processing", "ready", "failed"
        """
        resume_id = str(uuid4())
        now = _now()
        async with self._user_session(user_id) as session:
            session.add(
                Resume(
                    resume_id=resume_id,
                    user_id=user_id,
                    content=content,
                    content_type=content_type,
                    filename=filename,
                    is_master=is_master,
                    parent_id=parent_id,
                    processed_data=processed_data,
                    processing_status=processing_status,
                    cover_letter=cover_letter,
                    outreach_message=outreach_message,
                    interview_prep=interview_prep,
                    title=title,
                    original_markdown=original_markdown,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

        doc: dict[str, Any] = {
            "resume_id": resume_id,
            "content": content,
            "content_type": content_type,
            "filename": filename,
            "is_master": is_master,
            "parent_id": parent_id,
            "processed_data": processed_data,
            "processing_status": processing_status,
            "cover_letter": cover_letter,
            "outreach_message": outreach_message,
            "interview_prep": interview_prep,
            "title": title,
            "created_at": now,
            "updated_at": now,
        }
        if original_markdown is not None:
            doc["original_markdown"] = original_markdown
        return doc

    async def create_resume_atomic_master(
        self,
        user_id: str,
        content: str,
        content_type: str = "md",
        filename: str | None = None,
        processed_data: dict[str, Any] | None = None,
        processing_status: str = "pending",
        cover_letter: str | None = None,
        outreach_message: str | None = None,
        original_markdown: str | None = None,
        title: str | None = None,
        interview_prep: str | None = None,
    ) -> dict[str, Any]:
        """Create a new resume with atomic master assignment, scoped to ``user_id``.

        Uses an asyncio.Lock to prevent race conditions when multiple uploads
        happen concurrently and both try to become master. The lock is process
        -wide (not per-user) — cheap and correct, since master promotion is
        infrequent and the actual invariant is enforced per-user by the
        partial unique index.
        """
        async with self._master_resume_lock:
            current_master = await self.get_master_resume(user_id)
            is_master = current_master is None

            # Recovery: if the current master is stuck failed/processing, demote
            # it so this upload can become the new master.
            if current_master and current_master.get("processing_status") in (
                "failed",
                "processing",
            ):
                async with self._user_session(user_id) as session:
                    row = await self._get_owned(session, Resume, "resume_id", current_master["resume_id"], user_id)
                    if row is not None:
                        row.is_master = False
                        await session.commit()
                is_master = True

            return await self.create_resume(
                user_id=user_id,
                content=content,
                content_type=content_type,
                filename=filename,
                is_master=is_master,
                processed_data=processed_data,
                processing_status=processing_status,
                cover_letter=cover_letter,
                outreach_message=outreach_message,
                interview_prep=interview_prep,
                original_markdown=original_markdown,
                title=title,
            )

    @staticmethod
    async def _get_owned(
        session: AsyncSession, model: type, pk_name: str, pk_value: str, user_id: str
    ) -> Any:
        """Fetch a row by primary key, scoped to ``user_id`` (defense-in-depth over RLS)."""
        result = await session.execute(
            select(model).where(
                getattr(model, pk_name) == pk_value, model.user_id == user_id
            )
        )
        return result.scalars().first()

    async def get_resume(self, user_id: str, resume_id: str) -> dict[str, Any] | None:
        """Get resume by ID, scoped to ``user_id``."""
        async with self._user_session(user_id) as session:
            row = await self._get_owned(session, Resume, "resume_id", resume_id, user_id)
            return self._resume_to_dict(row) if row else None

    async def get_master_resume(self, user_id: str) -> dict[str, Any] | None:
        """Get the caller's master resume if one exists."""
        async with self._user_session(user_id) as session:
            result = await session.execute(
                select(Resume).where(Resume.is_master.is_(True), Resume.user_id == user_id)
            )
            row = result.scalars().first()
            return self._resume_to_dict(row) if row else None

    async def update_resume(
        self, user_id: str, resume_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        """Update resume by ID, scoped to ``user_id``.

        Raises:
            ValueError: If resume not found (or not owned by ``user_id``).
        """
        async with self._user_session(user_id) as session:
            row = await self._get_owned(session, Resume, "resume_id", resume_id, user_id)
            if row is None:
                raise ValueError(f"Resume not found: {resume_id}")
            for key, value in updates.items():
                if key in ("resume_id", "user_id"):
                    continue
                if hasattr(row, key):
                    setattr(row, key, value)
                else:
                    logger.warning("Ignoring unknown resume field on update: %s", key)
            row.updated_at = _now()
            await session.commit()
            return self._resume_to_dict(row)

    async def delete_resume(self, user_id: str, resume_id: str) -> bool:
        """Delete resume by ID, scoped to ``user_id``."""
        async with self._user_session(user_id) as session:
            row = await self._get_owned(session, Resume, "resume_id", resume_id, user_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def list_resumes(self, user_id: str) -> list[dict[str, Any]]:
        """List all resumes owned by ``user_id``."""
        async with self._user_session(user_id) as session:
            result = await session.execute(
                select(Resume).where(Resume.user_id == user_id).order_by(Resume.created_at)
            )
            return [self._resume_to_dict(row) for row in result.scalars().all()]

    async def set_master_resume(self, user_id: str, resume_id: str) -> bool:
        """Set a resume as the caller's master, unsetting any existing master.

        Returns False if the resume doesn't exist (or isn't owned by
        ``user_id``). Demote-then-promote happens in a single transaction so
        the partial unique index is never violated.
        """
        async with self._user_session(user_id) as session:
            target = await self._get_owned(session, Resume, "resume_id", resume_id, user_id)
            if target is None:
                logger.warning("Cannot set master: resume %s not found for user", resume_id)
                return False

            current = await session.execute(
                select(Resume).where(Resume.is_master.is_(True), Resume.user_id == user_id)
            )
            for row in current.scalars().all():
                if row.resume_id != resume_id:
                    row.is_master = False
            # Flush the demotions before promoting to satisfy the unique index.
            await session.flush()
            target.is_master = True
            await session.commit()
            return True

    # -- Job operations -----------------------------------------------------

    async def create_job(
        self, user_id: str, content: str, resume_id: str | None = None
    ) -> dict[str, Any]:
        """Create a new job description entry owned by ``user_id``."""
        job_id = str(uuid4())
        now = _now()
        async with self._user_session(user_id) as session:
            session.add(
                Job(
                    job_id=job_id,
                    user_id=user_id,
                    content=content,
                    resume_id=resume_id,
                    created_at=now,
                    metadata_json={},
                )
            )
            await session.commit()
        return {
            "job_id": job_id,
            "content": content,
            "resume_id": resume_id,
            "created_at": now,
        }

    async def get_job(self, user_id: str, job_id: str) -> dict[str, Any] | None:
        """Get job by ID, scoped to ``user_id`` (dynamic fields flattened)."""
        async with self._user_session(user_id) as session:
            row = await self._get_owned(session, Job, "job_id", job_id, user_id)
            return self._job_to_dict(row) if row else None

    async def update_job(
        self, user_id: str, job_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update a job by ID, scoped to ``user_id``.

        Core columns are set directly; every other key is merged into
        ``metadata_json`` so dynamic pipeline fields (``preview_hash``,
        ``job_keywords``, ``company``/``role``, …) round-trip through
        ``get_job`` as top-level keys.
        """
        async with self._user_session(user_id) as session:
            row = await self._get_owned(session, Job, "job_id", job_id, user_id)
            if row is None:
                return None
            meta = dict(row.metadata_json or {})
            for key, value in updates.items():
                if key in ("user_id",):
                    continue
                if key in _JOB_CORE_FIELDS:
                    setattr(row, key, value)
                else:
                    meta[key] = value
            # Reassign so SQLAlchemy detects the JSON mutation.
            row.metadata_json = meta
            await session.commit()
            return self._job_to_dict(row)

    async def delete_job(self, user_id: str, job_id: str) -> bool:
        """Delete a job by ID, scoped to ``user_id`` (used to clean up an orphaned manual-add job)."""
        async with self._user_session(user_id) as session:
            row = await self._get_owned(session, Job, "job_id", job_id, user_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    # -- Improvement operations ---------------------------------------------

    async def create_improvement(
        self,
        user_id: str,
        original_resume_id: str,
        tailored_resume_id: str,
        job_id: str,
        improvements: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create an improvement result entry owned by ``user_id``."""
        request_id = str(uuid4())
        now = _now()
        async with self._user_session(user_id) as session:
            session.add(
                Improvement(
                    request_id=request_id,
                    user_id=user_id,
                    original_resume_id=original_resume_id,
                    tailored_resume_id=tailored_resume_id,
                    job_id=job_id,
                    improvements=improvements,
                    created_at=now,
                )
            )
            await session.commit()
        return {
            "request_id": request_id,
            "original_resume_id": original_resume_id,
            "tailored_resume_id": tailored_resume_id,
            "job_id": job_id,
            "improvements": improvements,
            "created_at": now,
        }

    async def get_improvement_by_tailored_resume(
        self, user_id: str, tailored_resume_id: str
    ) -> dict[str, Any] | None:
        """Get improvement record by tailored resume ID, scoped to ``user_id``."""
        async with self._user_session(user_id) as session:
            result = await session.execute(
                select(Improvement).where(
                    Improvement.tailored_resume_id == tailored_resume_id,
                    Improvement.user_id == user_id,
                )
            )
            row = result.scalars().first()
            return self._improvement_to_dict(row) if row else None

    # -- Application (tracker) operations -----------------------------------

    async def _next_position(self, session: AsyncSession, user_id: str, status: str) -> int:
        result = await session.execute(
            select(func.count())
            .select_from(Application)
            .where(Application.status == status, Application.user_id == user_id)
        )
        return int(result.scalar() or 0)

    async def _renumber(self, session: AsyncSession, user_id: str, status: str) -> None:
        """Renumber a column's positions to a contiguous 0..n-1 sequence."""
        result = await session.execute(
            select(Application)
            .where(Application.status == status, Application.user_id == user_id)
            .order_by(Application.position, Application.created_at)
        )
        for index, row in enumerate(result.scalars().all()):
            if row.position != index:
                row.position = index

    async def create_application(
        self,
        user_id: str,
        job_id: str,
        resume_id: str,
        master_resume_id: str | None = None,
        status: str = "applied",
        company: str | None = None,
        role: str | None = None,
        applied_at: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Create a tracker card owned by ``user_id``, deduped on (owner, job_id, resume_id).

        If a card for the same job+resume already exists for this user it is
        returned as-is (survives double-submit / retried confirms).
        """
        async with self._user_session(user_id) as session:
            existing = await session.execute(
                select(Application).where(
                    Application.user_id == user_id,
                    Application.job_id == job_id,
                    Application.resume_id == resume_id,
                )
            )
            found = existing.scalars().first()
            if found is not None:
                return self._application_to_dict(found)

            now = _now()
            if applied_at is None and status != "saved":
                applied_at = now
            position = await self._next_position(session, user_id, status)
            row = Application(
                application_id=str(uuid4()),
                user_id=user_id,
                job_id=job_id,
                resume_id=resume_id,
                master_resume_id=master_resume_id,
                status=status,
                company=company,
                role=role,
                applied_at=applied_at,
                notes=notes,
                position=position,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                # A concurrent create won the (user_id, job_id, resume_id) unique
                # constraint — return the existing card instead of duplicating.
                await session.rollback()
                dup = await session.execute(
                    select(Application).where(
                        Application.user_id == user_id,
                        Application.job_id == job_id,
                        Application.resume_id == resume_id,
                    )
                )
                found = dup.scalars().first()
                if found is not None:
                    logger.debug(
                        "Deduped concurrent application create for job=%s resume=%s",
                        job_id,
                        resume_id,
                    )
                    return self._application_to_dict(found)
                raise
            return self._application_to_dict(row)

    async def list_applications(
        self, user_id: str, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List applications owned by ``user_id``, ordered by (status, position)."""
        async with self._user_session(user_id) as session:
            stmt = select(Application).where(Application.user_id == user_id)
            if status is not None:
                stmt = stmt.where(Application.status == status)
            stmt = stmt.order_by(Application.status, Application.position)
            result = await session.execute(stmt)
            return [self._application_to_dict(row) for row in result.scalars().all()]

    async def get_application(
        self, user_id: str, application_id: str
    ) -> dict[str, Any] | None:
        """Get an application by ID, scoped to ``user_id``."""
        async with self._user_session(user_id) as session:
            row = await self._get_owned(
                session, Application, "application_id", application_id, user_id
            )
            return self._application_to_dict(row) if row else None

    async def update_application(
        self, user_id: str, application_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update an application owned by ``user_id``; renumber columns when status/position change.

        ``position`` is interpreted as the desired index within the (possibly
        new) ``status`` column; siblings are renumbered server-side so the
        column stays a contiguous 0..n-1 sequence.
        """
        async with self._user_session(user_id) as session:
            row = await self._get_owned(
                session, Application, "application_id", application_id, user_id
            )
            if row is None:
                return None

            old_status = row.status
            new_status = updates.get("status", old_status)
            target_position = updates.get("position", None)

            for key in ("company", "role", "applied_at", "notes"):
                if key in updates:
                    setattr(row, key, updates[key])

            moved = "status" in updates or "position" in updates
            if moved:
                row.status = new_status
                # Park it out of the way, renumber both columns, then reinsert.
                row.position = 10_000_000
                await session.flush()
                if old_status != new_status:
                    await self._renumber(session, user_id, old_status)
                # Renumber the target column excluding this row, then splice in.
                siblings = await session.execute(
                    select(Application)
                    .where(
                        Application.user_id == user_id,
                        Application.status == new_status,
                        Application.application_id != application_id,
                    )
                    .order_by(Application.position, Application.created_at)
                )
                ordered = list(siblings.scalars().all())
                if target_position is None or target_position > len(ordered):
                    target_position = len(ordered)
                if target_position < 0:
                    target_position = 0
                ordered.insert(target_position, row)
                for index, item in enumerate(ordered):
                    item.position = index

            row.updated_at = _now()
            await session.commit()
            return self._application_to_dict(row)

    async def bulk_update_applications(
        self, user_id: str, application_ids: list[str], status: str
    ) -> int:
        """Move many of the caller's applications to the end of ``status``. Returns count moved."""
        moved = 0
        async with self._user_session(user_id) as session:
            affected_old: set[str] = set()
            for application_id in application_ids:
                row = await self._get_owned(
                    session, Application, "application_id", application_id, user_id
                )
                if row is None:
                    continue
                affected_old.add(row.status)
                row.status = status
                row.position = 20_000_000 + moved  # provisional, renumbered below
                row.updated_at = _now()
                moved += 1
            await session.flush()
            for old_status in affected_old - {status}:
                await self._renumber(session, user_id, old_status)
            await self._renumber(session, user_id, status)
            await session.commit()
        return moved

    async def delete_application(self, user_id: str, application_id: str) -> bool:
        """Delete an application owned by ``user_id``; renumber its column."""
        async with self._user_session(user_id) as session:
            row = await self._get_owned(
                session, Application, "application_id", application_id, user_id
            )
            if row is None:
                return False
            status = row.status
            await session.delete(row)
            await session.flush()
            await self._renumber(session, user_id, status)
            await session.commit()
            return True

    async def bulk_delete_applications(self, user_id: str, application_ids: list[str]) -> int:
        """Delete many of the caller's applications; renumber affected columns. Returns count."""
        deleted = 0
        async with self._user_session(user_id) as session:
            affected: set[str] = set()
            for application_id in application_ids:
                row = await self._get_owned(
                    session, Application, "application_id", application_id, user_id
                )
                if row is None:
                    continue
                affected.add(row.status)
                await session.delete(row)
                deleted += 1
            await session.flush()
            for status in affected:
                await self._renumber(session, user_id, status)
            await session.commit()
        return deleted

    # -- Encrypted API key store (sync; read on the LLM hot path) -----------

    def get_api_key_ciphertexts(self, user_id: str) -> dict[str, str]:
        """Return ``{provider: ciphertext}`` for keys stored by ``user_id`` (sync)."""
        with self._user_sync_session(user_id) as session:
            rows = (
                session.execute(select(ApiKey).where(ApiKey.user_id == user_id))
                .scalars()
                .all()
            )
            return {row.provider: row.ciphertext for row in rows}

    def set_api_key_ciphertext(self, user_id: str, provider: str, ciphertext: str) -> None:
        """Upsert one provider's ciphertext for ``user_id`` (sync)."""
        with self._user_sync_session(user_id) as session:
            row = (
                session.execute(
                    select(ApiKey).where(
                        ApiKey.user_id == user_id, ApiKey.provider == provider
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                session.add(
                    ApiKey(
                        id=str(uuid4()),
                        user_id=user_id,
                        provider=provider,
                        ciphertext=ciphertext,
                        updated_at=_now(),
                    )
                )
            else:
                row.ciphertext = ciphertext
                row.updated_at = _now()
            session.commit()

    def delete_api_key(self, user_id: str, provider: str) -> None:
        """Delete one provider's key for ``user_id`` (sync)."""
        with self._user_sync_session(user_id) as session:
            row = (
                session.execute(
                    select(ApiKey).where(
                        ApiKey.user_id == user_id, ApiKey.provider == provider
                    )
                )
                .scalars()
                .first()
            )
            if row is not None:
                session.delete(row)
                session.commit()

    def clear_api_keys(self, user_id: str) -> None:
        """Delete all keys stored by ``user_id`` (sync)."""
        with self._user_sync_session(user_id) as session:
            session.execute(delete(ApiKey).where(ApiKey.user_id == user_id))
            session.commit()

    def replace_api_keys(self, user_id: str, ciphertexts: dict[str, str]) -> None:
        """Atomically replace ``user_id``'s whole key store (clear + insert in one txn).

        A single transaction means a failure mid-write can't leave the store
        half-cleared and wipe a user's previously saved keys.
        """
        with self._user_sync_session(user_id) as session:
            session.execute(delete(ApiKey).where(ApiKey.user_id == user_id))
            now = _now()
            for provider, ciphertext in ciphertexts.items():
                if ciphertext:
                    session.add(
                        ApiKey(
                            id=str(uuid4()),
                            user_id=user_id,
                            provider=provider,
                            ciphertext=ciphertext,
                            updated_at=now,
                        )
                    )
            session.commit()

    # -- Stats / maintenance ------------------------------------------------

    async def get_stats(self, user_id: str) -> dict[str, Any]:
        """Get database statistics scoped to ``user_id``."""
        async with self._user_session(user_id) as session:
            resumes = await session.scalar(
                select(func.count()).select_from(Resume).where(Resume.user_id == user_id)
            )
            jobs = await session.scalar(
                select(func.count()).select_from(Job).where(Job.user_id == user_id)
            )
            improvements = await session.scalar(
                select(func.count())
                .select_from(Improvement)
                .where(Improvement.user_id == user_id)
            )
            master = await session.execute(
                select(Resume.resume_id)
                .where(Resume.is_master.is_(True), Resume.user_id == user_id)
                .limit(1)
            )
            return {
                "total_resumes": int(resumes or 0),
                "total_jobs": int(jobs or 0),
                "total_improvements": int(improvements or 0),
                "has_master_resume": master.first() is not None,
            }

    async def reset_database(self, user_id: str) -> None:
        """Reset ``user_id``'s data by truncating their rows and clearing their uploads.

        Clears the caller's resumes/jobs/improvements **and** tracker
        applications (leaving orphaned cards after a full data reset would be
        a bug). Encrypted ``api_keys`` are preserved — matching the
        pre-existing behavior where a reset never wiped the user's stored
        credentials. Never touches another account's rows.
        """
        async with self._user_session(user_id) as session:
            await session.execute(delete(Application).where(Application.user_id == user_id))
            await session.execute(delete(Improvement).where(Improvement.user_id == user_id))
            await session.execute(delete(Job).where(Job.user_id == user_id))
            await session.execute(delete(Resume).where(Resume.user_id == user_id))
            await session.commit()

        uploads_dir = settings.data_dir / "uploads" / user_id
        if uploads_dir.exists():
            shutil.rmtree(uploads_dir)
            uploads_dir.mkdir(parents=True, exist_ok=True)


# Global database instance
db = Database()
