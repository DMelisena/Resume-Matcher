"""Shared test fixtures for Resume Matcher backend tests."""

import copy
import importlib
import os
from typing import TYPE_CHECKING, NamedTuple
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

# Local Supabase stack default (see supabase/migrations/ + `supabase start`):
# role `resume_matcher_app` has no privileges of its own, only membership in
# `authenticated` (adopted per-transaction, same as the running app) — never
# point tests at a service_role/superuser connection. Override with
# TEST_DATABASE_URL to point at a different Postgres (e.g. in CI).
DEFAULT_TEST_DATABASE_URL = (
    "postgresql://resume_matcher_app:localtestpass123@127.0.0.1:54422/postgres"
)
# Local GoTrue (Supabase Auth) admin endpoint + its well-known local service
# role key (public in every Supabase CLI project's docs — this is the LOCAL
# dev stack only, never a deployed project; nothing here ever ships in app
# code or reaches app/config.py). Used ONLY to create/delete disposable test
# accounts so `user_id`'s FK into auth.users is satisfiable — the app itself
# never touches this endpoint or this key. Override with TEST_SUPABASE_URL /
# TEST_SUPABASE_SERVICE_ROLE_KEY for a different local stack.
DEFAULT_TEST_SUPABASE_URL = "http://127.0.0.1:54421"
DEFAULT_TEST_SUPABASE_SERVICE_ROLE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vf"
    "cm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0.EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"
)


# ---------------------------------------------------------------------------
# Sample resume data — full ResumeData-compatible dict
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_resume() -> dict:
    """A realistic resume dict matching the ResumeData schema."""
    return {
        "personalInfo": {
            "name": "Jane Doe",
            "title": "Senior Backend Engineer",
            "email": "jane@example.com",
            "phone": "+1-555-0100",
            "location": "San Francisco, CA",
            "website": "https://janedoe.dev",
            "linkedin": "linkedin.com/in/janedoe",
            "github": "github.com/janedoe",
        },
        "summary": "Backend engineer with 6 years of experience building scalable Python APIs and microservices.",
        "workExperience": [
            {
                "id": 1,
                "title": "Senior Backend Engineer",
                "company": "Acme Corp",
                "location": "San Francisco, CA",
                "years": "Jan 2021 - Present",
                "description": [
                    "Built REST APIs serving 50K requests/day using Python and FastAPI",
                    "Led migration from monolith to microservices architecture",
                    "Mentored 3 junior developers on backend best practices",
                ],
            },
            {
                "id": 2,
                "title": "Software Engineer",
                "company": "StartupCo",
                "location": "New York, NY",
                "years": "Jun 2018 - Dec 2020",
                "description": [
                    "Developed payment processing system handling $2M monthly",
                    "Wrote unit and integration tests improving coverage from 40% to 85%",
                ],
            },
        ],
        "education": [
            {
                "id": 1,
                "institution": "MIT",
                "degree": "B.S. Computer Science",
                "years": "2014 - 2018",
                "description": "Graduated with honors, Dean's List",
            }
        ],
        "personalProjects": [
            {
                "id": 1,
                "name": "OpenAPI Generator",
                "role": "Creator & Maintainer",
                "years": "Mar 2021 - Present",
                "description": [
                    "CLI tool generating API clients from OpenAPI specs",
                    "500+ GitHub stars, used by 30+ companies",
                ],
            }
        ],
        "additional": {
            "technicalSkills": ["Python", "FastAPI", "Docker", "AWS", "PostgreSQL", "Redis"],
            "languages": ["English (Native)", "Spanish (Conversational)"],
            "certificationsTraining": ["AWS Solutions Architect Associate"],
            "awards": ["Employee of the Year 2022"],
        },
        "customSections": {},
        "sectionMeta": [],
    }


@pytest.fixture
def sample_resume_copy(sample_resume) -> dict:
    """Deep copy of sample_resume for mutation-safe tests."""
    return copy.deepcopy(sample_resume)


# ---------------------------------------------------------------------------
# Job-related fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_job_keywords() -> dict:
    """Extracted job keywords matching the LLM output format."""
    return {
        "required_skills": ["Python", "FastAPI", "Docker", "Kubernetes"],
        "preferred_skills": ["AWS", "Terraform", "GraphQL"],
        "experience_requirements": ["5+ years backend development"],
        "education_requirements": ["Bachelor's in CS or equivalent"],
        "key_responsibilities": [
            "Design and build scalable APIs",
            "Lead technical architecture decisions",
        ],
        "keywords": ["microservices", "CI/CD", "agile", "REST API"],
        "experience_years": 5,
        "seniority_level": "senior",
    }


@pytest.fixture
def sample_job_description() -> str:
    """A realistic job description text."""
    return (
        "Senior Backend Engineer at TechCorp\n\n"
        "We are looking for a Senior Backend Engineer to join our platform team. "
        "You will design and build scalable APIs using Python and FastAPI. "
        "Experience with Docker, Kubernetes, and AWS is required. "
        "Terraform and GraphQL experience is a plus.\n\n"
        "Requirements:\n"
        "- 5+ years backend development experience\n"
        "- Strong Python skills with FastAPI or similar frameworks\n"
        "- Experience with microservices architecture\n"
        "- Familiarity with CI/CD pipelines and agile methodologies\n"
        "- Bachelor's degree in CS or equivalent\n"
    )


# ---------------------------------------------------------------------------
# Master resume — used for alignment validation
# ---------------------------------------------------------------------------

@pytest.fixture
def master_resume(sample_resume) -> dict:
    """Master resume (source of truth) — same as sample_resume by default."""
    return copy.deepcopy(sample_resume)


# ---------------------------------------------------------------------------
# ResumeChange fixtures for diff-based tests
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_changes():
    """A set of ResumeChange dicts covering all action types."""
    from app.schemas.models import ResumeChange

    return [
        ResumeChange(
            path="summary",
            action="replace",
            original="Backend engineer with 6 years of experience building scalable Python APIs and microservices.",
            value="Senior backend engineer with 6 years building scalable Python APIs, microservices, and cloud infrastructure on AWS.",
            reason="Added cloud/AWS keywords from JD",
        ),
        ResumeChange(
            path="workExperience[0].description[0]",
            action="replace",
            original="Built REST APIs serving 50K requests/day using Python and FastAPI",
            value="Designed and built REST APIs serving 50K requests/day using Python, FastAPI, and Docker",
            reason="Added Docker keyword from JD",
        ),
        ResumeChange(
            path="workExperience[0].description",
            action="append",
            original=None,
            value="Implemented CI/CD pipelines with GitHub Actions reducing deploy time by 40%",
            reason="Added CI/CD keyword from JD",
        ),
        ResumeChange(
            path="additional.technicalSkills",
            action="reorder",
            original=None,
            value=["Python", "FastAPI", "Docker", "AWS", "PostgreSQL", "Redis"],
            reason="Already in good order, no change needed",
        ),
    ]


# ---------------------------------------------------------------------------
# Isolated database — real Postgres (RLS-enforced), one fresh user_id per test
# ---------------------------------------------------------------------------


if TYPE_CHECKING:
    from app.database import Database


class IsolatedDB(NamedTuple):
    """What `isolated_db` yields: a real `Database` plus this test's own account id."""

    db: "Database"
    user_id: str


@pytest.fixture
async def isolated_db(monkeypatch):
    """Point the global ``db`` singleton at a real (local) Postgres + RLS instance,
    across ``app.database`` and every router module that imported it, and hand
    the test a fresh random ``user_id`` no other test has used.

    Lets endpoint / e2e tests run against REAL Postgres with REAL Row-Level
    Security (not a mock, and not SQLite — RLS is a Postgres-only feature, and
    it is the authoritative isolation boundary this whole feature exists to
    enforce). Tests don't need per-test schemas or truncation: a unique
    ``user_id`` per test *is* the isolation, via the same RLS policies the app
    itself relies on in production.

    Requires a reachable Postgres with the RLS migration applied — by default
    the local Supabase stack (``supabase start`` from the repo root; see
    ``supabase/migrations/``). Override the target with ``TEST_DATABASE_URL``.
    Skips (not fails) if nothing is reachable, so the rest of the suite still
    runs in an environment with no local Postgres.
    """
    import app.config as config_module
    import app.database as database_module
    from app.database import Database

    test_database_url = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    monkeypatch.setattr(config_module.settings, "database_url", test_database_url)

    test_db = Database()
    try:
        test_db._ensure_initialized()
        with test_db._sync() as session:
            session.execute(text("SELECT 1"))
    except OperationalError as exc:
        await test_db.close()
        pytest.skip(
            "No reachable test Postgres (TEST_DATABASE_URL or local Supabase via "
            f"`supabase start`). Connection error: {exc}"
        )

    # user_id has a real FK into auth.users (see the RLS migration) — a bare
    # random uuid4 fails that constraint the moment a test writes a row. Create
    # a real, disposable Supabase Auth account via the local GoTrue admin API
    # for this test only, and delete it again on teardown (cascades to any
    # rows the test created, via `on delete cascade`).
    import httpx

    supabase_url = os.environ.get("TEST_SUPABASE_URL", DEFAULT_TEST_SUPABASE_URL)
    service_role_key = os.environ.get(
        "TEST_SUPABASE_SERVICE_ROLE_KEY", DEFAULT_TEST_SUPABASE_SERVICE_ROLE_KEY
    )
    auth_admin = httpx.AsyncClient(
        base_url=supabase_url,
        headers={"apikey": service_role_key, "Authorization": f"Bearer {service_role_key}"},
        timeout=30.0,
    )
    try:
        resp = await auth_admin.post(
            "/auth/v1/admin/users",
            json={
                "email": f"test-{uuid4().hex}@example.invalid",
                "password": uuid4().hex,
                "email_confirm": True,
            },
        )
        resp.raise_for_status()
        user_id = resp.json()["id"]
    except httpx.HTTPError as exc:
        await auth_admin.aclose()
        await test_db.close()
        pytest.skip(f"Could not create a disposable test auth account: {exc}")

    monkeypatch.setattr(database_module, "db", test_db)
    for router_name in (
        "resumes",
        "jobs",
        "enrichment",
        "config",
        "health",
        "applications",
        "resume_wizard",
    ):
        try:
            module = importlib.import_module(f"app.routers.{router_name}")
        except ModuleNotFoundError:
            continue
        if hasattr(module, "db"):
            monkeypatch.setattr(module, "db", test_db)
    try:
        yield IsolatedDB(db=test_db, user_id=user_id)
    finally:
        await test_db.close()
        try:
            await auth_admin.delete(f"/auth/v1/admin/users/{user_id}")
        except httpx.HTTPError:
            pass  # best-effort cleanup; a leftover test account is harmless
        await auth_admin.aclose()


@pytest.fixture
async def isolated_db2(isolated_db):
    """A second real, disposable account sharing `isolated_db`'s Database/engine.

    For tests that need two distinct accounts to prove RLS/ownership isolation
    (e.g. "account A's master resume doesn't affect account B's"). Reuses the
    same `Database` instance — isolation between the two comes entirely from
    RLS + per-call `user_id`, exactly like two real concurrent requests would.
    """
    import httpx

    supabase_url = os.environ.get("TEST_SUPABASE_URL", DEFAULT_TEST_SUPABASE_URL)
    service_role_key = os.environ.get(
        "TEST_SUPABASE_SERVICE_ROLE_KEY", DEFAULT_TEST_SUPABASE_SERVICE_ROLE_KEY
    )
    async with httpx.AsyncClient(
        base_url=supabase_url,
        headers={"apikey": service_role_key, "Authorization": f"Bearer {service_role_key}"},
        timeout=30.0,
    ) as auth_admin:
        resp = await auth_admin.post(
            "/auth/v1/admin/users",
            json={
                "email": f"test-{uuid4().hex}@example.invalid",
                "password": uuid4().hex,
                "email_confirm": True,
            },
        )
        resp.raise_for_status()
        user_id = resp.json()["id"]
        try:
            yield IsolatedDB(db=isolated_db.db, user_id=user_id)
        finally:
            try:
                await auth_admin.delete(f"/auth/v1/admin/users/{user_id}")
            except httpx.HTTPError:
                pass


@pytest.fixture
async def authed_client(isolated_db):
    """An httpx AsyncClient wired to the real FastAPI app, real Postgres+RLS
    (via ``isolated_db``), and a bearer-token dependency override so requests
    are authenticated as ``isolated_db.user_id`` without needing a real JWT.
    """
    import httpx

    from app.auth import get_current_user_id
    from app.main import app as fastapi_app

    fastapi_app.dependency_overrides[get_current_user_id] = lambda: isolated_db.user_id
    try:
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user_id, None)
