"""The database tests again, against real Postgres (e.g. Supabase), in a throwaway schema.

Skipped unless TEST_DATABASE_URL is set:
    TEST_DATABASE_URL=postgresql://... python -m pytest tests/test_db_postgres.py
"""

import os
import uuid
from urllib.parse import quote

import psycopg
import pytest

import db
from test_db import *  # noqa: F401,F403 - re-run the SQLite suite's tests on Postgres
from test_db import T0, _note

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

# These inspect SQLite files or SQLite-specific errors directly.
SQLITE_ONLY = {
    "test_init_creates_tables_and_is_repeatable", "test_init_creates_missing_parent_dir",
    "test_data_survives_reopening", "test_existing_v1_database_upgrades_without_data_loss",
    "test_draft_requires_existing_note", "test_using_db_before_init_fails_clearly",
}
for _name in SQLITE_ONLY:
    globals().pop(_name, None)


def _with_search_path(url: str, schema: str) -> str:
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}options={quote(f'-csearch_path={schema}')}"


@pytest.fixture(autouse=True)
def fresh_db():
    """Overrides test_db's SQLite fixture: a brand-new schema per test, dropped afterwards."""
    schema = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(URL, autocommit=True, prepare_threshold=None) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    db.init_db(database_url=_with_search_path(URL, schema))
    yield
    with psycopg.connect(URL, autocommit=True, prepare_threshold=None) as conn:
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_postgres_backend_is_active_and_migrated() -> None:
    assert db.backend() == "postgres"
    assert db.schema_version() == len(db.PG_MIGRATIONS)
    db.init_db(database_url=db._pg_url)  # re-running migrations is a no-op
    assert db.schema_version() == len(db.PG_MIGRATIONS)


def test_scores_keep_exact_decimals() -> None:
    note_id = _note()
    db.set_note_triage(note_id, 8.1, True, "Industry Transparency", "r", "a", "k")
    assert db.get_note(note_id).score == 8.1  # DOUBLE PRECISION, not float4


def test_large_telegram_ids_fit() -> None:
    note_id = db.add_note(2_147_483_648, -1004421996422, "ids beyond 32-bit", T0)
    assert db.get_note(note_id).tg_chat_id == -1004421996422


def test_claims_work_on_postgres() -> None:
    note_id = _note()
    assert db.claim_note(note_id, 360) and not db.claim_note(note_id, 360)
    db.release_note(note_id)
    assert db.claim_note(note_id, 360)
