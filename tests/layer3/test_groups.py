"""Groups view (job sector, industry, employer, postal area, city, province), similar customers, new chart data."""
import json

import pytest

from c360.layer3.data.source import GROUP_BY, postal_area
from tests.layer3.conftest import needs_store


def test_postal_area_accepts_a_full_code_or_its_first_three_characters():
    assert postal_area("l4w 2a1") == "L4W" and postal_area("L4W2A1") == "L4W" and postal_area("M5V") == "M5V"
    assert postal_area("12345") is None and postal_area("") is None and postal_area(None) is None


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("C360_L3_OUT", str(tmp_path))
    from fastapi.testclient import TestClient
    from c360.layer3 import actions, api
    actions._store = None
    yield TestClient(api.app)
    actions._store = None


@needs_store()
def test_groups_by_every_dimension(client):
    for by in GROUP_BY:
        d = client.get(f"/api/l3/groups?by={by}&limit=5").json()
        assert d["by"] == by and 0 < len(d["groups"]) <= 5
        g = d["groups"][0]
        assert g["grp"] and g["cases"] >= g["customers"] > 0 and 0 <= g["hardship_share"] <= 1 and 0 <= g["lost_income_share"] <= 1
        assert [x["cases"] for x in d["groups"]] == sorted((x["cases"] for x in d["groups"]), reverse=True)
    assert client.get("/api/l3/groups?by=gender").status_code == 400          # only the six listed ways to group


@needs_store()
def test_postal_area_filter_and_worklist_drill_down(client):
    area = client.get("/api/l3/groups?by=area&limit=1").json()["groups"][0]["grp"]
    d = client.get(f"/api/l3/groups?by=sector&area={area.lower()} 1A1").json()      # a full postal code works too
    assert d["area"] == area and d["total"]["cases"] >= sum(g["cases"] for g in d["groups"]) > 0
    rows = client.get(f"/api/l3/worklist?area={area}&limit=200").json()["rows"]
    assert rows and all(r["postal_area"] == area for r in rows)
    emp = client.get("/api/l3/groups?by=employer&limit=1").json()["groups"][0]["grp"]
    rows = client.get("/api/l3/worklist", params={"group_by": "employer", "group": emp}).json()["rows"]
    assert rows and all(r["employer"] == emp for r in rows)


@needs_store()
def test_similar_customers_show_figures_not_other_people(client):
    d = client.get("/api/l3/groups?by=employer&limit=1").json()
    case_id = client.get("/api/l3/worklist", params={"group_by": "employer", "group": d["groups"][0]["grp"]}).json()["rows"][0]["case_id"]
    p = client.get(f"/api/l3/cases/{case_id}/peers").json()
    keys = [g["key"] for g in p["groups"]]
    assert "employer" in keys and "area" in keys
    emp = next(g for g in p["groups"] if g["key"] == "employer")
    assert emp["open_cases"] == d["groups"][0]["cases"] - 1                      # everyone at the employer except this customer
    assert case_id not in [c["case_id"] for g in p["groups"] for c in g["cases"]]
    text = json.dumps(p)
    assert "first_name" not in text and "last_name" not in text
    assert client.get("/api/l3/cases/CS-2026-000001/peers").status_code == 404
    assert isinstance(client.get("/api/l3/groups").json()["layoff_alerts"], list)


@needs_store()
def test_overview_has_the_new_chart_data(client):
    p = client.get("/api/l3/portfolio").json()
    for k in ("hardship_reasons", "products", "due_by_day", "contact_outcomes", "sectors"):
        assert p[k], k
    assert sum(r["cases"] for r in p["products"]) == p["tiles"]["open_cases"]
