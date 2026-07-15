"""GET /api/v1/leads — sort contract.

The dashboard's sort control offers four options (score high/low, newest/oldest)
and passes its value straight through as ?sort=. The endpoint only accepted
"score" and "captured", so the page's own default — score_desc — 422'd on load.

These pin the four values the UI actually sends, plus the two original values,
which stay accepted as aliases: /api/v1 has other clients and dropping them
would be a breaking change rather than an additive one.
"""

import uuid

from .test_leads_sort_helpers import auth, make_tenant_user, seed_leads_with_scores


class TestUiSortValues:
    """The four values the dashboard's sort control can emit."""

    def test_score_desc_is_accepted_and_orders_high_to_low(self, pg_client, db_session):
        """The page's default. This is the exact request that 422'd."""
        user, token = make_tenant_user(db_session)
        seed_leads_with_scores(db_session, user.tenant_id, [10, 90, 50])

        resp = pg_client.get("/api/v1/leads?sort=score_desc", headers=auth(token))

        assert resp.status_code == 200, resp.text
        scores = [i["lead_score"] for i in resp.json()["data"]["items"]]
        assert scores == [90, 50, 10]

    def test_score_asc_orders_low_to_high(self, pg_client, db_session):
        user, token = make_tenant_user(db_session)
        seed_leads_with_scores(db_session, user.tenant_id, [10, 90, 50])

        resp = pg_client.get("/api/v1/leads?sort=score_asc", headers=auth(token))

        assert resp.status_code == 200, resp.text
        scores = [i["lead_score"] for i in resp.json()["data"]["items"]]
        assert scores == [10, 50, 90]

    def test_created_desc_orders_newest_first(self, pg_client, db_session):
        user, token = make_tenant_user(db_session)
        seed_leads_with_scores(db_session, user.tenant_id, [10, 90, 50], stagger_created=True)

        resp = pg_client.get("/api/v1/leads?sort=created_desc", headers=auth(token))

        assert resp.status_code == 200, resp.text
        created = [i["created_at"] for i in resp.json()["data"]["items"]]
        assert created == sorted(created, reverse=True)

    def test_created_asc_orders_oldest_first(self, pg_client, db_session):
        user, token = make_tenant_user(db_session)
        seed_leads_with_scores(db_session, user.tenant_id, [10, 90, 50], stagger_created=True)

        resp = pg_client.get("/api/v1/leads?sort=created_asc", headers=auth(token))

        assert resp.status_code == 200, resp.text
        created = [i["created_at"] for i in resp.json()["data"]["items"]]
        assert created == sorted(created)


class TestLegacySortValues:
    """v1 has other clients — the original values must keep working."""

    def test_score_still_accepted_as_alias_for_score_desc(self, pg_client, db_session):
        user, token = make_tenant_user(db_session)
        seed_leads_with_scores(db_session, user.tenant_id, [10, 90, 50])

        resp = pg_client.get("/api/v1/leads?sort=score", headers=auth(token))

        assert resp.status_code == 200, resp.text
        scores = [i["lead_score"] for i in resp.json()["data"]["items"]]
        assert scores == [90, 50, 10]

    def test_captured_still_accepted_as_alias_for_created_desc(self, pg_client, db_session):
        user, token = make_tenant_user(db_session)
        seed_leads_with_scores(db_session, user.tenant_id, [10, 90, 50], stagger_created=True)

        resp = pg_client.get("/api/v1/leads?sort=captured", headers=auth(token))

        assert resp.status_code == 200, resp.text
        created = [i["created_at"] for i in resp.json()["data"]["items"]]
        assert created == sorted(created, reverse=True)


class TestSortValidation:
    def test_unknown_sort_is_still_rejected(self, pg_client, db_session):
        """Widening the enum must not turn it into a free-text field."""
        _user, token = make_tenant_user(db_session)

        resp = pg_client.get("/api/v1/leads?sort=" + uuid.uuid4().hex, headers=auth(token))

        assert resp.status_code == 422
