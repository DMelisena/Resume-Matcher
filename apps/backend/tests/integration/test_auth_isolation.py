"""Regression coverage for per-account data isolation (the point of Supabase Auth).

Exercises the REAL FastAPI app, the REAL `Database` facade, and the REAL
Postgres Row-Level Security policies (supabase/migrations/
20260925165120_user_owned_tables_and_rls.sql) against a live local Postgres —
no mocking of `db` or of RLS. `get_current_user_id` (the only piece that
verifies a Supabase JWT) is swapped for a fake that trusts a header
(``authed_client``, from conftest.py), so this suite doesn't need a running
Supabase Auth service for JWT verification, but the two accounts themselves
ARE real Supabase Auth users (``isolated_db``/``isolated_db2`` create real,
disposable accounts via the local GoTrue admin API — `user_id` has a real FK
into `auth.users`, so a fabricated uuid won't do). Everything past auth
verification — ownership columns, the facade's `user_id` filtering, and the
database's own RLS policies — runs for real. This is deliberately the
strongest layer to test directly, since RLS still protects a caller's data
even if a future endpoint forgets a `user_id` filter.

Uses ``isolated_db``/``isolated_db2`` (see conftest.py), so it needs a local
Postgres + Supabase Auth stack (``supabase start``); it skips cleanly if none
is reachable.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.auth import get_current_user_id
from app.main import app


def _client_as(user_id: str) -> AsyncClient:
    """An AsyncClient hitting the real app, authenticated as ``user_id``.

    Overrides only `get_current_user_id` (the JWT-verification boundary) —
    everything downstream (routers, the `db` facade, Postgres RLS) is real.
    """
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_current_user_id, None)


class TestJobIsolation:
    async def test_owner_can_read_own_job(self, isolated_db):
        async with _client_as(isolated_db.user_id) as client:
            create = await client.post(
                "/api/v1/jobs/upload",
                json={"job_descriptions": ["ALICE ONLY: staff engineer role"]},
            )
            assert create.status_code == 200
            job_id = create.json()["job_id"][0]

            got = await client.get(f"/api/v1/jobs/{job_id}")
            assert got.status_code == 200
            assert got.json()["content"] == "ALICE ONLY: staff engineer role"

    async def test_other_account_cannot_read_job(self, isolated_db, isolated_db2):
        async with _client_as(isolated_db.user_id) as client:
            create = await client.post(
                "/api/v1/jobs/upload",
                json={"job_descriptions": ["ALICE SECRET JOB"]},
            )
            job_id = create.json()["job_id"][0]

        async with _client_as(isolated_db2.user_id) as client:
            got = await client.get(f"/api/v1/jobs/{job_id}")
        # Not found, not forbidden: the row is invisible to the other
        # account, not merely access-denied — RLS filters it out entirely.
        assert got.status_code == 404

    async def test_other_account_cannot_delete_job(self, isolated_db, isolated_db2):
        """Confirm the delete path (used to clean up an orphaned manual-add
        job) can't be abused to delete another account's job."""
        async with _client_as(isolated_db.user_id) as client:
            create = await client.post(
                "/api/v1/jobs/upload", json={"job_descriptions": ["ALICE JOB TO PROTECT"]}
            )
            job_id = create.json()["job_id"][0]

        deleted = await isolated_db.db.delete_job(isolated_db2.user_id, job_id)
        assert deleted is False  # the other account's delete reports "not found", not success

        still_there = await isolated_db.db.get_job(isolated_db.user_id, job_id)
        assert still_there is not None
        assert still_there["content"] == "ALICE JOB TO PROTECT"


class TestApplicationTrackerIsolation:
    async def test_board_only_shows_own_cards(self, isolated_db, isolated_db2):
        await isolated_db.db.create_application(
            isolated_db.user_id, job_id="job-a", resume_id="res-a", status="applied"
        )
        await isolated_db.db.create_application(
            isolated_db2.user_id, job_id="job-b", resume_id="res-b", status="applied"
        )

        async with _client_as(isolated_db.user_id) as client:
            resp = await client.get("/api/v1/applications")
        cards = resp.json()["columns"]["applied"]
        assert len(cards) == 1
        assert cards[0]["job_id"] == "job-a"

        async with _client_as(isolated_db2.user_id) as client:
            resp = await client.get("/api/v1/applications")
        cards = resp.json()["columns"]["applied"]
        assert len(cards) == 1
        assert cards[0]["job_id"] == "job-b"

    async def test_cannot_update_another_accounts_card(self, isolated_db, isolated_db2):
        card = await isolated_db.db.create_application(
            isolated_db.user_id, job_id="job-x", resume_id="res-x", status="applied"
        )

        async with _client_as(isolated_db2.user_id) as client:
            resp = await client.patch(
                f"/api/v1/applications/{card['application_id']}",
                json={"notes": "not your card"},
            )
        assert resp.status_code == 404

        unchanged = await isolated_db.db.get_application(
            isolated_db.user_id, card["application_id"]
        )
        assert unchanged is not None
        assert unchanged["notes"] is None


class TestApiKeyIsolation:
    async def test_keys_are_not_shared_across_accounts(self, isolated_db, isolated_db2):
        isolated_db.db.set_api_key_ciphertext(isolated_db.user_id, "openai", "alice-ciphertext")
        isolated_db.db.set_api_key_ciphertext(isolated_db2.user_id, "openai", "bob-ciphertext")

        alice_keys = isolated_db.db.get_api_key_ciphertexts(isolated_db.user_id)
        bob_keys = isolated_db.db.get_api_key_ciphertexts(isolated_db2.user_id)

        assert alice_keys == {"openai": "alice-ciphertext"}
        assert bob_keys == {"openai": "bob-ciphertext"}


class TestRowLevelSecurityAtTheDatabaseLayer:
    """Belt-and-suspenders check: RLS blocks cross-account rows even if a
    future code path forgets to filter by user_id in the facade."""

    async def test_rls_hides_rows_even_with_an_unfiltered_query(
        self, isolated_db, isolated_db2
    ):
        job = await isolated_db.db.create_job(
            isolated_db.user_id, content="ALICE ONLY VIA RAW QUERY TEST"
        )

        # Simulate a query that forgot the `user_id` filter (the facade
        # always adds one — this proves RLS is the backstop, not just app
        # code discipline).
        async with isolated_db.db._user_session(isolated_db2.user_id) as session:  # noqa: SLF001
            result = await session.execute(
                text("SELECT * FROM jobs WHERE job_id = :id"), {"id": job["job_id"]}
            )
            assert result.first() is None

        async with isolated_db.db._user_session(isolated_db.user_id) as session:  # noqa: SLF001
            result = await session.execute(
                text("SELECT * FROM jobs WHERE job_id = :id"), {"id": job["job_id"]}
            )
            assert result.first() is not None

    async def test_rls_blocks_cross_account_delete_at_the_sql_layer(
        self, isolated_db, isolated_db2
    ):
        job = await isolated_db.db.create_job(
            isolated_db.user_id, content="ALICE JOB TARGETED FOR RAW DELETE"
        )

        async with isolated_db.db._user_session(isolated_db2.user_id) as session:  # noqa: SLF001
            result = await session.execute(
                text("DELETE FROM jobs WHERE job_id = :id"), {"id": job["job_id"]}
            )
            assert result.rowcount == 0
            await session.rollback()

        still_there = await isolated_db.db.get_job(isolated_db.user_id, job["job_id"])
        assert still_there is not None
