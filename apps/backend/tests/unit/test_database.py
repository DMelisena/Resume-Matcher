"""Tests for the real SQLAlchemy/Postgres layer (app.database.Database).

Every integration test mocks `db`, so the actual persistence layer was barely
exercised. These run against a real (local) Postgres + RLS instance via the
shared `isolated_db` fixture, so CRUD, master-resume assignment, the jobs
``metadata_json`` round-trip, applications, and stats are verified end-to-end
on the real storage — including that every query is properly scoped to the
caller's ``user_id``.
"""

import pytest

from app.db_engine import init_models_sync


class TestResumeCrud:
    async def test_create_and_get(self, isolated_db):
        db, user_id = isolated_db
        created = await db.create_resume(user_id, content="# Resume", filename="r.pdf")
        assert created["resume_id"]
        fetched = await db.get_resume(user_id, created["resume_id"])
        assert fetched is not None
        assert fetched["content"] == "# Resume"
        assert fetched["filename"] == "r.pdf"

    async def test_get_missing_returns_none(self, isolated_db):
        db, user_id = isolated_db
        assert await db.get_resume(user_id, "does-not-exist") is None

    async def test_list_resumes(self, isolated_db):
        db, user_id = isolated_db
        await db.create_resume(user_id, content="a")
        await db.create_resume(user_id, content="b")
        assert len(await db.list_resumes(user_id)) == 2

    async def test_update_resume_changes_field_and_timestamp(self, isolated_db):
        db, user_id = isolated_db
        created = await db.create_resume(user_id, content="x")
        updated = await db.update_resume(user_id, created["resume_id"], {"title": "New Title"})
        assert updated["title"] == "New Title"
        assert updated["updated_at"] >= created["updated_at"]

    async def test_update_missing_raises(self, isolated_db):
        db, user_id = isolated_db
        with pytest.raises(ValueError):
            await db.update_resume(user_id, "missing", {"title": "X"})

    async def test_delete_resume(self, isolated_db):
        db, user_id = isolated_db
        created = await db.create_resume(user_id, content="x")
        assert await db.delete_resume(user_id, created["resume_id"]) is True
        assert await db.get_resume(user_id, created["resume_id"]) is None

    async def test_delete_missing_returns_false(self, isolated_db):
        db, user_id = isolated_db
        assert await db.delete_resume(user_id, "missing") is False

    async def test_original_markdown_absence_semantics(self, isolated_db):
        db, user_id = isolated_db
        # Omitted when None (preserve TinyDB behavior); present when supplied.
        without = await db.create_resume(user_id, content="x")
        assert "original_markdown" not in without
        with_md = await db.create_resume(user_id, content="x", original_markdown="# raw")
        fetched = await db.get_resume(user_id, with_md["resume_id"])
        assert fetched["original_markdown"] == "# raw"

    async def test_interview_prep_round_trips_as_text(self, isolated_db):
        db, user_id = isolated_db
        created = await db.create_resume(
            user_id,
            content="x",
            interview_prep='{"role_fit_analysis":["fit"]}',
        )
        fetched = await db.get_resume(user_id, created["resume_id"])
        assert fetched["interview_prep"] == '{"role_fit_analysis":["fit"]}'

    def test_init_models_sync_is_idempotent(self, isolated_db):
        """`create_all` against an already-migrated Postgres database must be a
        safe no-op (the authoritative schema lives in supabase/migrations/;
        this is only ever a dev/test convenience layered on top of it)."""
        db, _user_id = isolated_db
        db._ensure_initialized()
        # Already ran once during isolated_db's own setup; running again here
        # must not raise or duplicate anything.
        init_models_sync(db._sync_engine)


class TestMasterResume:
    async def test_no_master_initially(self, isolated_db):
        db, user_id = isolated_db
        assert await db.get_master_resume(user_id) is None

    async def test_set_master_unsets_previous(self, isolated_db):
        db, user_id = isolated_db
        r1 = await db.create_resume(user_id, content="1")
        r2 = await db.create_resume(user_id, content="2")

        assert await db.set_master_resume(user_id, r1["resume_id"]) is True
        assert (await db.get_master_resume(user_id))["resume_id"] == r1["resume_id"]

        assert await db.set_master_resume(user_id, r2["resume_id"]) is True
        master = await db.get_master_resume(user_id)
        assert master["resume_id"] == r2["resume_id"]
        # Only one master at a time.
        assert sum(1 for r in await db.list_resumes(user_id) if r["is_master"]) == 1

    async def test_set_master_missing_returns_false(self, isolated_db):
        db, user_id = isolated_db
        assert await db.set_master_resume(user_id, "missing") is False

    async def test_atomic_first_upload_becomes_master(self, isolated_db):
        db, user_id = isolated_db
        created = await db.create_resume_atomic_master(
            user_id, content="first", processing_status="ready"
        )
        assert created["is_master"] is True

    async def test_atomic_second_upload_not_master(self, isolated_db):
        db, user_id = isolated_db
        await db.create_resume_atomic_master(user_id, content="first", processing_status="ready")
        second = await db.create_resume_atomic_master(
            user_id, content="second", processing_status="ready"
        )
        assert second["is_master"] is False

    async def test_atomic_recovers_when_master_stuck(self, isolated_db):
        db, user_id = isolated_db
        # Master stuck in "failed" → next upload is promoted to master.
        first = await db.create_resume_atomic_master(
            user_id, content="first", processing_status="failed"
        )
        assert first["is_master"] is True
        second = await db.create_resume_atomic_master(
            user_id, content="second", processing_status="ready"
        )
        assert second["is_master"] is True
        assert (await db.get_master_resume(user_id))["resume_id"] == second["resume_id"]

    async def test_master_is_per_account_not_global(self, isolated_db, isolated_db2):
        """Two accounts can each have their own master resume simultaneously —
        the single-master invariant is scoped per user_id, not process-wide."""
        db, user_id = isolated_db
        db2, user_id2 = isolated_db2
        mine = await db.create_resume_atomic_master(user_id, content="mine", processing_status="ready")
        theirs = await db2.create_resume_atomic_master(
            user_id2, content="theirs", processing_status="ready"
        )
        assert mine["is_master"] is True
        assert theirs["is_master"] is True
        assert (await db.get_master_resume(user_id))["resume_id"] == mine["resume_id"]
        assert (await db2.get_master_resume(user_id2))["resume_id"] == theirs["resume_id"]


class TestJobs:
    async def test_create_and_get_job(self, isolated_db):
        db, user_id = isolated_db
        created = await db.create_job(user_id, content="Engineer role", resume_id="r1")
        fetched = await db.get_job(user_id, created["job_id"])
        assert fetched["content"] == "Engineer role"
        assert fetched["resume_id"] == "r1"

    async def test_get_missing_job_returns_none(self, isolated_db):
        db, user_id = isolated_db
        assert await db.get_job(user_id, "missing") is None

    async def test_update_job(self, isolated_db):
        db, user_id = isolated_db
        created = await db.create_job(user_id, content="old")
        updated = await db.update_job(user_id, created["job_id"], {"content": "new"})
        assert updated["content"] == "new"

    async def test_update_missing_job_returns_none(self, isolated_db):
        db, user_id = isolated_db
        assert await db.update_job(user_id, "missing", {"content": "x"}) is None

    async def test_dynamic_fields_round_trip_as_top_level(self, isolated_db):
        """Dynamic pipeline fields must survive write→read as top-level keys.

        This is the highest-risk migration detail: ``/improve/confirm`` rejects
        with 400 if ``preview_hash``/``preview_hashes`` don't round-trip.
        """
        db, user_id = isolated_db
        created = await db.create_job(user_id, content="jd")
        await db.update_job(
            user_id,
            created["job_id"],
            {
                "job_keywords": {"required_skills": ["Python", "AWS"]},
                "job_keywords_hash": "deadbeef",
                "preview_hash": "abc123",
                "preview_hashes": {"keywords": "abc123", "nudge": "def456"},
                "preview_prompt_id": "keywords",
                "company": "Acme Corp",
                "role": "Staff Engineer",
            },
        )
        fetched = await db.get_job(user_id, created["job_id"])
        # Core fields preserved.
        assert fetched["content"] == "jd"
        # Dynamic fields flattened to the top level.
        assert fetched["preview_hash"] == "abc123"
        assert fetched["preview_hashes"] == {"keywords": "abc123", "nudge": "def456"}
        assert fetched["job_keywords_hash"] == "deadbeef"
        assert fetched["job_keywords"]["required_skills"] == ["Python", "AWS"]
        assert fetched["company"] == "Acme Corp"
        assert fetched["role"] == "Staff Engineer"

    async def test_update_job_merges_metadata(self, isolated_db):
        db, user_id = isolated_db
        created = await db.create_job(user_id, content="jd")
        await db.update_job(user_id, created["job_id"], {"preview_hash": "h1"})
        await db.update_job(user_id, created["job_id"], {"company": "Acme"})
        fetched = await db.get_job(user_id, created["job_id"])
        # The second update must not wipe the first dynamic field.
        assert fetched["preview_hash"] == "h1"
        assert fetched["company"] == "Acme"


class TestImprovements:
    async def test_create_and_lookup_by_tailored_resume(self, isolated_db):
        db, user_id = isolated_db
        await db.create_improvement(
            user_id,
            original_resume_id="orig",
            tailored_resume_id="tailored-1",
            job_id="job-1",
            improvements=[{"path": "summary"}],
        )
        found = await db.get_improvement_by_tailored_resume(user_id, "tailored-1")
        assert found is not None
        assert found["job_id"] == "job-1"

    async def test_lookup_missing_returns_none(self, isolated_db):
        db, user_id = isolated_db
        assert await db.get_improvement_by_tailored_resume(user_id, "nope") is None


class TestApplications:
    async def test_create_defaults_and_position(self, isolated_db):
        db, user_id = isolated_db
        a = await db.create_application(user_id, job_id="j1", resume_id="r1")
        assert a["status"] == "applied"
        assert a["position"] == 0
        assert a["applied_at"] is not None  # applied → stamped
        b = await db.create_application(user_id, job_id="j2", resume_id="r2")
        assert b["position"] == 1  # appended to the column

    async def test_saved_status_has_no_applied_at(self, isolated_db):
        db, user_id = isolated_db
        a = await db.create_application(user_id, job_id="j1", resume_id="r1", status="saved")
        assert a["applied_at"] is None

    async def test_create_dedupes_on_job_and_resume(self, isolated_db):
        db, user_id = isolated_db
        a = await db.create_application(user_id, job_id="j1", resume_id="r1")
        again = await db.create_application(user_id, job_id="j1", resume_id="r1")
        assert again["application_id"] == a["application_id"]
        assert len(await db.list_applications(user_id)) == 1

    async def test_move_renumbers_columns(self, isolated_db):
        db, user_id = isolated_db
        a = await db.create_application(user_id, job_id="j1", resume_id="r1")
        b = await db.create_application(user_id, job_id="j2", resume_id="r2")
        # Move a to the front of "interview".
        moved = await db.update_application(
            user_id, a["application_id"], {"status": "interview", "position": 0}
        )
        assert moved["status"] == "interview"
        assert moved["position"] == 0
        # The "applied" column renumbered: b is now position 0.
        applied = await db.list_applications(user_id, status="applied")
        assert [x["application_id"] for x in applied] == [b["application_id"]]
        assert applied[0]["position"] == 0

    async def test_bulk_update_and_delete(self, isolated_db):
        db, user_id = isolated_db
        a = await db.create_application(user_id, job_id="j1", resume_id="r1")
        b = await db.create_application(user_id, job_id="j2", resume_id="r2")
        moved = await db.bulk_update_applications(
            user_id, [a["application_id"], b["application_id"]], "rejected"
        )
        assert moved == 2
        rejected = await db.list_applications(user_id, status="rejected")
        assert {x["position"] for x in rejected} == {0, 1}
        deleted = await db.bulk_delete_applications(user_id, [a["application_id"]])
        assert deleted == 1
        remaining = await db.list_applications(user_id, status="rejected")
        assert len(remaining) == 1
        assert remaining[0]["position"] == 0  # renumbered after delete


class TestApiKeyStore:
    async def test_set_get_delete_ciphertext(self, isolated_db):
        db, user_id = isolated_db
        db.set_api_key_ciphertext(user_id, "openai", "ct-openai")
        db.set_api_key_ciphertext(user_id, "anthropic", "ct-anthropic")
        assert db.get_api_key_ciphertexts(user_id) == {
            "openai": "ct-openai",
            "anthropic": "ct-anthropic",
        }
        db.delete_api_key(user_id, "openai")
        assert db.get_api_key_ciphertexts(user_id) == {"anthropic": "ct-anthropic"}
        db.clear_api_keys(user_id)
        assert db.get_api_key_ciphertexts(user_id) == {}


class TestStatsAndReset:
    async def test_get_stats(self, isolated_db):
        db, user_id = isolated_db
        await db.create_resume(user_id, content="a")
        await db.set_master_resume(user_id, (await db.list_resumes(user_id))[0]["resume_id"])
        await db.create_job(user_id, content="jd")
        stats = await db.get_stats(user_id)
        assert stats["total_resumes"] == 1
        assert stats["total_jobs"] == 1
        assert stats["has_master_resume"] is True

    async def test_reset_database_truncates(self, isolated_db, tmp_path, monkeypatch):
        db, user_id = isolated_db
        # reset_database also clears settings.data_dir/uploads/<user_id> — isolate it to tmp.
        monkeypatch.setattr("app.database.settings.data_dir", tmp_path)
        await db.create_resume(user_id, content="a")
        await db.create_job(user_id, content="jd")
        await db.create_application(user_id, job_id="j1", resume_id="r1")
        await db.reset_database(user_id)
        stats = await db.get_stats(user_id)
        assert stats["total_resumes"] == 0
        assert stats["total_jobs"] == 0
        assert stats["has_master_resume"] is False
        # Applications are cleared too (no orphans after a full reset).
        assert await db.list_applications(user_id) == []
