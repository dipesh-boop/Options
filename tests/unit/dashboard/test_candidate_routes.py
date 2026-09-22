"""Step 22.5 (PAPER_TRADING_V1.4.4): read-only /api/candidates dashboard
routes. No POST/PUT route exists for this resource anywhere -- confirming
a candidate only ever happens via the separate scripts/confirm_candidate.py
operator command."""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import src.dashboard.app as dashboard_app
from src.review.candidates import CandidateStatus, InMemoryCandidateReviewStore
from tests.unit.review.conftest import make_candidate


@pytest.fixture
def client():
    store = InMemoryCandidateReviewStore()
    dashboard_app.set_candidate_review_store(store)
    c = TestClient(dashboard_app.app)
    yield c, store
    dashboard_app.set_candidate_review_store(None)


class TestNoCandidateReviewStoreConfigured:
    def test_503_when_no_store_is_configured(self):
        dashboard_app.set_candidate_review_store(None)
        c = TestClient(dashboard_app.app)
        r = c.get("/api/candidates")
        assert r.status_code == 503


class TestListCandidates:
    def test_empty_store_returns_empty_list(self, client):
        c, _ = client
        r = c.get("/api/candidates")
        assert r.status_code == 200
        assert r.json() == []

    def test_defaults_to_awaiting_human_only(self, client):
        c, store = client
        store.save_candidate(make_candidate(candidate_id="c1"))
        store.save_candidate(replace(make_candidate(candidate_id="c2"), status=CandidateStatus.REJECTED))

        r = c.get("/api/candidates")
        assert r.status_code == 200
        ids = {row["candidate_id"] for row in r.json()}
        assert ids == {"c1"}

    def test_include_resolved_returns_every_candidate(self, client):
        c, store = client
        store.save_candidate(make_candidate(candidate_id="c1"))
        store.save_candidate(replace(make_candidate(candidate_id="c2"), status=CandidateStatus.REJECTED))

        r = c.get("/api/candidates?include_resolved=true")
        assert r.status_code == 200
        ids = {row["candidate_id"] for row in r.json()}
        assert ids == {"c1", "c2"}

    def test_response_carries_the_economically_relevant_fields(self, client):
        c, store = client
        store.save_candidate(make_candidate())
        r = c.get("/api/candidates")
        row = r.json()[0]
        assert row["ticker"] == "SPY"
        assert row["strategy"] == "cash_secured_put"
        assert row["llm_review_performed"] is False
        assert row["status"] == "awaiting_human"
        assert row["capital_required"] > 0
        assert len(row["legs"]) == 1


class TestGetCandidate:
    def test_404_for_unknown_candidate(self, client):
        c, _ = client
        r = c.get("/api/candidates/does-not-exist")
        assert r.status_code == 404

    def test_returns_the_candidate_by_id(self, client):
        c, store = client
        store.save_candidate(make_candidate(candidate_id="cand-xyz"))
        r = c.get("/api/candidates/cand-xyz")
        assert r.status_code == 200
        assert r.json()["candidate_id"] == "cand-xyz"


class TestNoWriteRouteExistsForCandidates:
    """Confirming a candidate is CLI-only -- see
    scripts/confirm_candidate.py and src.review.confirmation's module
    docstring. There must be no dashboard action capable of it."""

    def test_no_post_put_delete_patch_route_for_candidates(self):
        for route in dashboard_app.app.routes:
            path = getattr(route, "path", "")
            if not path.startswith("/api/candidates"):
                continue
            methods = getattr(route, "methods", set()) or set()
            forbidden = methods & {"POST", "PUT", "DELETE", "PATCH"}
            assert not forbidden, f"route {path!r} unexpectedly exposes {forbidden}"
