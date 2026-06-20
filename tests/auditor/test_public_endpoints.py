"""Tests for public auditor endpoints (no-auth, rate-limited)."""
import uuid
from unittest.mock import AsyncMock, patch


from app.db.models.audit import AuditReport, AuditScope, AuditStatus
from app.db.models.lead import Lead


# ---------------------------------------------------------------------------
# POST /public/audit
# ---------------------------------------------------------------------------

def test_post_public_audit_starts_and_returns_id(client, db):
    """POST /public/audit creates a report and returns audit_report_id."""
    with patch("app.auditor.service.enqueue_audit", new_callable=AsyncMock):
        resp = client.post("/api/v1/public/audit", json={"url": "https://example.com"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["success"] is True
    assert "audit_report_id" in body["data"]


def test_post_public_audit_enqueue_failure_returns_503(client, db):
    """When Redis/enqueue fails, endpoint returns 503 ENQUEUE_FAILED — not a silent 202."""
    with patch(
        "app.auditor.service.enqueue_audit",
        new_callable=AsyncMock,
        side_effect=ConnectionError("Redis unavailable"),
    ):
        resp = client.post("/api/v1/public/audit", json={"url": "https://example.com"})

    assert resp.status_code == 503
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "ENQUEUE_FAILED"

    # The report must exist in DB but be marked FAILED, not QUEUED
    failed = db.query(AuditReport).filter(AuditReport.status == AuditStatus.FAILED).all()
    assert len(failed) == 1


# ---------------------------------------------------------------------------
# Rate limit — hermetic test with mocked Redis
# ---------------------------------------------------------------------------

def test_post_public_audit_rate_limit_enforced(client, db):
    """After 3 audits from same IP, 4th call returns 429.

    Drives check_rate_limit with a mocked Redis so the real sliding-window
    logic executes. The mock Redis increments an in-memory counter per key,
    making the 4th acquire() call exceed max_calls=3.
    """

    from app.api.v1.endpoints.public_auditor import public_audit_limiter

    # Build a fake Redis that tracks a sorted-set per key using a plain dict.
    # zremrangebyscore / zadd / zcard / zrange / expire are the only methods
    # check_rate_limit and acquire() use.
    class _FakeRedis:
        def __init__(self):
            self._store: dict[str, list[float]] = {}

        async def zremrangebyscore(self, key, _min, max_score):
            self._store.setdefault(key, [])
            self._store[key] = [s for s in self._store[key] if s > float(max_score)]

        async def zcard(self, key):
            return len(self._store.get(key, []))

        async def zadd(self, key, mapping):
            score = list(mapping.values())[0]
            self._store.setdefault(key, []).append(score)

        async def expire(self, key, ttl):
            pass

        async def zrange(self, key, start, stop, withscores=False):
            vals = self._store.get(key, [])
            if not vals:
                return []
            sliced = vals[start: stop + 1 if stop >= 0 else None]
            if withscores:
                return [(str(s), s) for s in sliced]
            return [str(s) for s in sliced]

    fake_redis = _FakeRedis()

    # Patch the limiter's internal _redis so it uses our fake instead of real Redis.
    original_redis = public_audit_limiter._redis
    public_audit_limiter._redis = fake_redis  # type: ignore[assignment]

    try:
        with patch("app.auditor.service.enqueue_audit", new_callable=AsyncMock):
            for i in range(3):
                resp = client.post(
                    "/api/v1/public/audit", json={"url": f"https://example{i}.com"}
                )
                assert resp.status_code == 202, (
                    f"Expected 202 on call {i + 1}, got {resp.status_code}: {resp.json()}"
                )

            # 4th call — should be rate limited at 429
            resp4 = client.post("/api/v1/public/audit", json={"url": "https://blocked.com"})
            assert resp4.status_code == 429

            # Verify limiter key embeds the client IP (loopback in test client)
            ip = "testclient"  # httpx TestClient uses "testclient" as host
            expected_key = f"public_audit:{ip}"
            assert expected_key in fake_redis._store, (
                f"Expected Redis key '{expected_key}' not found; keys: {list(fake_redis._store)}"
            )
            assert len(fake_redis._store[expected_key]) == 3  # 3 successful acquires recorded
    finally:
        public_audit_limiter._redis = original_redis  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# GET /public/audit/{id}
# ---------------------------------------------------------------------------

def test_get_public_report_returns_scores_and_blurred_gaps(client, db):
    """GET /public/audit/{id} returns scores + blurred gap titles, NOT raw content."""
    report = AuditReport(
        url="https://ex.com",
        domain="ex.com",
        scope=AuditScope.PUBLIC,
        status=AuditStatus.COMPLETED,
        overall_score=74,
        projected_score=91,
        knowledge_score=70,
        support_score=60,
        conversion_score=80,
        gaps=[
            {
                "title": "Refund policy",
                "why_it_matters": "trust",
                "recommended_content": "secret detail",
                "kind": "policy",
                "demand_count": 3,
            }
        ],
    )
    db.add(report)
    db.commit()

    resp = client.get(f"/api/v1/public/audit/{report.id}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["overall_score"] == 74
    assert data["projected_score"] == 91
    assert len(data["gaps"]) == 1
    gap = data["gaps"][0]
    assert gap["title"] == "Refund policy"
    assert gap["kind"] == "policy"
    assert gap["demand_count"] == 3
    # Strict: no raw content must leak
    assert "recommended_content" not in gap
    assert "why_it_matters" not in gap


def test_get_public_report_not_found(client, db):
    """GET /public/audit/{id} returns 404 for unknown id."""
    resp = client.get(f"/api/v1/public/audit/{uuid.uuid4()}")
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "AUDIT_NOT_FOUND"


# ---------------------------------------------------------------------------
# POST /public/audit/{id}/email
# ---------------------------------------------------------------------------

def test_post_email_creates_lead_with_email_hash(client, db):
    """POST /public/audit/{id}/email creates a Lead with email_hash and sends audit_report template."""
    report = AuditReport(
        url="https://ex.com",
        domain="ex.com",
        scope=AuditScope.PUBLIC,
        status=AuditStatus.COMPLETED,
        overall_score=74,
        knowledge_score=70,
        support_score=60,
        conversion_score=80,
        projected_score=91,
        gaps=[{"title": "Refund policy", "kind": "policy", "demand_count": 3}],
    )
    db.add(report)
    db.commit()

    with patch(
        "app.api.v1.endpoints.public_auditor.email_service.send_templated_email",
        return_value=True,
    ) as mock_email:
        resp = client.post(
            f"/api/v1/public/audit/{report.id}/email",
            json={"email": "owner@example.com"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert "lead_id" in body["data"]

    # Lead row has email_hash (set by the before_insert event listener)
    leads = db.query(Lead).filter(Lead.audit_report_id == report.id).all()
    assert len(leads) == 1
    assert leads[0].source == "public_auditor"
    expected_hash = Lead.compute_email_hash("owner@example.com")
    assert leads[0].email_hash == expected_hash

    # AuditReport.email_hash is also populated explicitly in the handler
    refreshed = db.query(AuditReport).filter(AuditReport.id == report.id).one()
    assert refreshed.email_hash == expected_hash

    # Email was sent using the dedicated audit_report template
    mock_email.assert_called_once()
    call_kwargs = mock_email.call_args.kwargs
    assert call_kwargs["template_name"] == "audit_report.html"

    # Context carries the four scores and at least one gap title
    ctx = call_kwargs["context"]
    assert ctx["overall_score"] == 74
    assert ctx["knowledge_score"] == 70
    assert ctx["support_score"] == 60
    assert ctx["conversion_score"] == 80
    assert ctx["projected_score"] == 91
    assert "Refund policy" in ctx["gap_titles"]


def test_post_email_report_not_found(client, db):
    """POST /public/audit/{id}/email returns 404 for unknown report."""
    resp = client.post(
        f"/api/v1/public/audit/{uuid.uuid4()}/email",
        json={"email": "owner@example.com"},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "AUDIT_NOT_FOUND"


# ---------------------------------------------------------------------------
# Fix 3 — rate-limit + idempotency on lead-capture email
# ---------------------------------------------------------------------------

def test_post_email_rate_limit_enforced(client, db):
    """After 5 email requests from same IP, 6th returns 429."""
    from app.api.v1.endpoints.public_auditor import capture_lead_limiter

    class _FakeRedis:
        def __init__(self):
            self._store: dict = {}

        async def zremrangebyscore(self, key, _min, max_score):
            self._store.setdefault(key, [])
            self._store[key] = [s for s in self._store[key] if s > float(max_score)]

        async def zcard(self, key):
            return len(self._store.get(key, []))

        async def zadd(self, key, mapping):
            score = list(mapping.values())[0]
            self._store.setdefault(key, []).append(score)

        async def expire(self, key, ttl):
            pass

        async def zrange(self, key, start, stop, withscores=False):
            vals = self._store.get(key, [])
            if not vals:
                return []
            sliced = vals[start: stop + 1 if stop >= 0 else None]
            if withscores:
                return [(str(s), s) for s in sliced]
            return [str(s) for s in sliced]

    fake_redis = _FakeRedis()
    original_redis = capture_lead_limiter._redis
    capture_lead_limiter._redis = fake_redis  # type: ignore[assignment]

    # Create 6 distinct reports so each request hits a valid report.
    # Collect IDs before commit to avoid DetachedInstanceError after session boundary.
    report_objects = []
    for _ in range(6):
        r = AuditReport(
            url="https://ex.com",
            domain="ex.com",
            scope=AuditScope.PUBLIC,
            status=AuditStatus.COMPLETED,
            overall_score=70,
            knowledge_score=70,
            support_score=60,
            conversion_score=80,
            projected_score=90,
        )
        db.add(r)
        report_objects.append(r)
    db.flush()
    report_ids = [r.id for r in report_objects]

    try:
        with patch(
            "app.api.v1.endpoints.public_auditor.email_service.send_templated_email",
            return_value=True,
        ):
            for i in range(5):
                resp = client.post(
                    f"/api/v1/public/audit/{report_ids[i]}/email",
                    json={"email": f"user{i}@example.com"},
                )
                assert resp.status_code == 200, (
                    f"Expected 200 on call {i+1}, got {resp.status_code}: {resp.json()}"
                )

            # 6th call — should be rate limited
            resp6 = client.post(
                f"/api/v1/public/audit/{report_ids[5]}/email",
                json={"email": "user5@example.com"},
            )
            assert resp6.status_code == 429
    finally:
        capture_lead_limiter._redis = original_redis  # type: ignore[assignment]


def test_post_email_idempotency_second_request_returns_409(client, db):
    """Second email request for the same report returns 409 and does NOT send a second email."""
    report = AuditReport(
        url="https://ex.com",
        domain="ex.com",
        scope=AuditScope.PUBLIC,
        status=AuditStatus.COMPLETED,
        overall_score=74,
        knowledge_score=70,
        support_score=60,
        conversion_score=80,
        projected_score=91,
        gaps=[{"title": "Gap A", "kind": "policy", "demand_count": 1}],
    )
    db.add(report)
    db.commit()

    with patch(
        "app.api.v1.endpoints.public_auditor.email_service.send_templated_email",
        return_value=True,
    ) as mock_email:
        # First request — should succeed
        resp1 = client.post(
            f"/api/v1/public/audit/{report.id}/email",
            json={"email": "first@example.com"},
        )
        assert resp1.status_code == 200
        assert resp1.json()["success"] is True

        # Second request for the SAME report — must be rejected
        resp2 = client.post(
            f"/api/v1/public/audit/{report.id}/email",
            json={"email": "second@example.com"},
        )
        assert resp2.status_code == 409
        body2 = resp2.json()
        assert body2["success"] is False
        assert body2["error"]["code"] == "LEAD_ALREADY_CAPTURED"

        # Email must have been sent exactly once across both requests
        assert mock_email.call_count == 1, (
            f"Expected 1 email send, got {mock_email.call_count}"
        )
