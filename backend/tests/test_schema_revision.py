"""Startup refuses databases that are not at the Alembic head this code requires."""
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.database import SCHEMA_REVISION


def test_schema_revision_is_the_single_alembic_head():
    cfg = Config("backend/alembic.ini")
    cfg.set_main_option("script_location", "backend/migrations")
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == [SCHEMA_REVISION], "Bump app.database.SCHEMA_REVISION when adding a migration"
