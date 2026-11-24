"""Leads list: search, sorting options and the X-Total-Count header."""
from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.database import get_db
from app.models import Lead
from app.models.entities import now_utc

AUTH = {"Authorization": "Bearer search-test-token"}


@pytest_asyncio.fixture
async def client(session_factory, monkeypatch):
    from app.main import app
    monkeypatch.setattr(settings, "api_auth_token", "search-test-token")

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    base = now_utc()
    async with session_factory() as db:
        db.add_all([
            Lead(company_name="Acme Interiors", city="Kolkata", industry="Interior design", email="hello@acme.example",
                 created_at=base - timedelta(days=3), next_follow_up_at=base + timedelta(days=2)),
            Lead(company_name="bright smiles dental", city="Mumbai", industry="Dentist", website="https://brightsmiles.example",
                 created_at=base - timedelta(days=2)),
            Lead(company_name="Zenith 100% Fitness", city="Kolkata", industry="Gym", decision_maker="Riya Sen",
                 created_at=base - timedelta(days=1), next_follow_up_at=base + timedelta(days=1)),
            Lead(company_name="Under_score Studio", city="Delhi", industry="Design", phone="+91 98300 12345",
                 created_at=base),
        ])
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def names(client, query=""):
    res = await client.get(f"/api/leads?{query}", headers=AUTH)
    assert res.status_code == 200, res.text
    return [row["company_name"] for row in res.json()], res.headers.get("x-total-count")


@pytest.mark.asyncio
async def test_total_count_header_reflects_filters_not_page_size(client):
    rows, total = await names(client, "page_size=2")
    assert len(rows) == 2 and total == "4"
    rows, total = await names(client, "q=kolkata&page_size=1&page=2")
    assert total == "2" and len(rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("term,expected", [
    ("ACME", ["Acme Interiors"]),                    # case-insensitive, company name
    ("dentist", ["bright smiles dental"]),            # industry
    ("brightsmiles.example", ["bright smiles dental"]),  # website
    ("riya", ["Zenith 100% Fitness"]),                # contact name
    ("98300", ["Under_score Studio"]),                # phone
    ("hello@acme", ["Acme Interiors"]),               # e-mail
    ("100%", ["Zenith 100% Fitness"]),                # % is literal, not a wildcard
    ("un_er", []),                                    # _ is literal (as a wildcard it would match "under")
    ("Under_score", ["Under_score Studio"]),
    ("   ", None),                                    # blank search = no filter
])
async def test_search(client, term, expected):
    rows, total = await names(client, f"q={term.replace('%', '%25').replace(' ', '%20')}")
    if expected is None:
        assert total == "4"
    else:
        assert rows == expected and total == str(len(expected))


@pytest.mark.asyncio
@pytest.mark.parametrize("sort,expected", [
    ("", ["Under_score Studio", "Zenith 100% Fitness", "bright smiles dental", "Acme Interiors"]),
    ("newest", ["Under_score Studio", "Zenith 100% Fitness", "bright smiles dental", "Acme Interiors"]),
    ("oldest", ["Acme Interiors", "bright smiles dental", "Zenith 100% Fitness", "Under_score Studio"]),
    ("company_asc", ["Acme Interiors", "bright smiles dental", "Under_score Studio", "Zenith 100% Fitness"]),
    ("company_desc", ["Zenith 100% Fitness", "Under_score Studio", "bright smiles dental", "Acme Interiors"]),
])
async def test_sorting(client, sort, expected):
    rows, _ = await names(client, f"sort_by={sort}" if sort else "")
    assert rows == expected


@pytest.mark.asyncio
async def test_follow_up_soonest_puts_leads_without_follow_up_last(client):
    rows, _ = await names(client, "sort_by=follow_up_soonest")
    assert rows[:2] == ["Zenith 100% Fitness", "Acme Interiors"]
    assert set(rows[2:]) == {"bright smiles dental", "Under_score Studio"}


@pytest.mark.asyncio
async def test_score_sort_accepted_and_unknown_sort_rejected(client):
    rows, _ = await names(client, "sort_by=score_desc")
    assert len(rows) == 4  # no scores yet: all NULL, still returned
    res = await client.get("/api/leads?sort_by=drop_table", headers=AUTH)
    assert res.status_code == 422
    res = await client.get("/api/leads?q=" + "x" * 101, headers=AUTH)
    assert res.status_code == 422
