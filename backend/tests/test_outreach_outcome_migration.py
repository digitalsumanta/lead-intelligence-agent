"""Focused tests for the data-preserving 0014 downgrade guard."""
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


MIGRATION_PATH = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0014_outreach_outcomes.py"
SPEC = importlib.util.spec_from_file_location("migration_0014_outreach_outcomes", MIGRATION_PATH)
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class RecordingOperations:
    def __init__(self, bind):
        self.bind = bind
        self.calls = []
        self.context = type("Context", (), {"as_sql": False})()

    def get_bind(self):
        return self.bind

    def get_context(self):
        return self.context

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return record


@pytest.mark.parametrize("duplicate", [False, True])
def test_downgrade_guard_preserves_multiple_activities(duplicate):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sales_activities (id INTEGER, outreach_execution_id INTEGER)"))
        connection.execute(text("INSERT INTO sales_activities VALUES (1, 10)"))
        if duplicate:
            connection.execute(text("INSERT INTO sales_activities VALUES (2, 10)"))
        operations = RecordingOperations(connection)
        original_op = MIGRATION.op
        MIGRATION.op = operations
        try:
            if duplicate:
                with pytest.raises(RuntimeError, match="multiple sales activities"):
                    MIGRATION.downgrade()
                assert operations.calls == []
            else:
                MIGRATION.downgrade()
                assert [call[0] for call in operations.calls] == [
                    "drop_index", "drop_index", "drop_column", "create_index",
                ]
                assert operations.calls[-1][1][0] == "uq_sales_activities_execution"
                assert operations.calls[-1][2]["unique"] is True
        finally:
            MIGRATION.op = original_op
    engine.dispose()
