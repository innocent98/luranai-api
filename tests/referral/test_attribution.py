"""Signup attribution: a valid ``referral_code`` links the new tenant to a marketer.

Revenue-critical: attribution must never block or alter a signup, and no response may reveal which
codes exist.
"""

import re
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.db.models.referral import (
    AttributionSource,
    Marketer,
    MarketerStatus,
    ReferralAttribution,
    SignupOrigin,
)
from app.db.models.tenant import Tenant
from app.main import app
from app.services.referral import attribution as attribution_module
from app.services.referral.attribution import attribute_signup, origin_hash
from app.utils.http_rate_limiter import registration_rate_limit

URL = "/api/v1/auth/register-company"
_EMAIL_PATCH = "app.services.company_registration_service.email_service.send_verification_email"
_TOKEN_PATCH = (
    "app.services.company_registration_service.token_service.create_email_verification_token"
)
UA = "Mozilla/5.0 (X11; Fake) UniqueAgentMarker/9.9"


@pytest.fixture(autouse=True)
def _no_signup_rate_limit(client):
    """Several signups per test; the 3/hour IP limit is not what is under test here."""
    app.dependency_overrides[registration_rate_limit] = lambda: None
    yield
    app.dependency_overrides.pop(registration_rate_limit, None)


@pytest.fixture(autouse=True)
def _mail():
    with patch(_EMAIL_PATCH, return_value=True), patch(_TOKEN_PATCH, return_value="tok"):
        yield


def _marketer(db, *, code="ada-ref", email=None, status=MarketerStatus.ACTIVE) -> Marketer:
    marketer = Marketer(
        name="Ada Marketer",
        email=email or f"ada_{uuid4().hex[:6]}@marketer-co.example",
        code=code,
        status=status,
    )
    db.add(marketer)
    db.commit()
    return marketer


def _payload(**extra) -> dict:
    tag = uuid4().hex[:8]
    return {
        "company_name": f"Acme {tag}",
        "admin_full_name": "Alice Admin",
        "admin_email": f"alice_{tag}@signup-co.example",
        **extra,
    }


def _signup(client, **extra):
    return client.post(URL, json=_payload(**extra), headers={"User-Agent": UA})


def _attributions(db) -> list[ReferralAttribution]:
    db.expire_all()
    return list(db.execute(select(ReferralAttribution)).scalars())


def test_valid_code_creates_attribution(client, db):
    marketer_id = _marketer(db).id

    resp = _signup(client, referral_code="ada-ref")

    assert resp.status_code == 201, resp.json()
    rows = _attributions(db)
    assert len(rows) == 1
    assert rows[0].marketer_id == marketer_id
    assert str(rows[0].tenant_id) == resp.json()["tenant_id"]
    assert rows[0].source == AttributionSource.FORM
    assert rows[0].self_referral is False


def test_referral_source_cookie_is_recorded(client, db):
    _marketer(db)

    resp = _signup(client, referral_code="ada-ref", referral_source="cookie")

    assert resp.status_code == 201
    assert _attributions(db)[0].source == AttributionSource.COOKIE


@pytest.mark.parametrize("source", ["bogus", 5, ["cookie"], None])
def test_bad_referral_source_never_blocks_signup(client, db, source):
    _marketer(db)

    resp = _signup(client, referral_code="ada-ref", referral_source=source)

    assert resp.status_code == 201
    assert _attributions(db)[0].source == AttributionSource.FORM


@pytest.mark.parametrize("sent", ["ada-ref", "ADA-REF", "Ada-Ref", "  ada-ref  ", "\tADA-ref\n"])
def test_code_matching_ignores_case_and_padding(client, db, sent):
    marketer_id = _marketer(db).id

    resp = _signup(client, referral_code=sent)

    assert resp.status_code == 201
    rows = _attributions(db)
    assert [r.marketer_id for r in rows] == [marketer_id]


def test_unknown_suspended_malformed_and_overlong_codes_signup_still_201_no_attribution(client, db):
    _marketer(db, code="gone-ref", status=MarketerStatus.SUSPENDED)
    _marketer(db, code="live-ref")
    bad_codes = [
        "no-such-code",  # unknown
        "gone-ref",  # suspended marketer
        "ab",  # too short
        "bad code!",  # malformed characters
        "admin",  # reserved
        "",  # blank
        "   ",  # whitespace only
        "x" * 65,  # over-long
        "x" * 500,
        123,  # non-string
        ["live-ref"],
        {"code": "live-ref"},
        True,
        None,
    ]
    for code in bad_codes:
        resp = _signup(client, referral_code=code)
        assert resp.status_code == 201, (code, resp.json())

    assert _attributions(db) == []
    assert db.execute(select(SignupOrigin)).first() is None
    assert len(list(db.execute(select(Tenant)).scalars())) >= len(bad_codes)


def test_error_body_identical_for_known_and_unknown_codes(client, db):
    _marketer(db)
    base = _payload()
    del base["admin_full_name"]  # required field missing -> validation error

    bodies = []
    for extra in ({}, {"referral_code": "ada-ref"}, {"referral_code": "nope-nope"}):
        resp = client.post(URL, json={**base, **extra})
        assert resp.status_code == 422
        body = resp.json()
        # pydantic echoes the caller's own request body under "input"; everything else must match
        for err in body["detail"]:
            err.pop("input", None)
        bodies.append(body)

    assert bodies[0] == bodies[1] == bodies[2]


def test_duplicate_company_error_identical_with_and_without_valid_code(client, db):
    _marketer(db)
    payload = _payload()
    assert client.post(URL, json=payload).status_code == 201

    plain = client.post(URL, json={**payload, "admin_email": "other_x@signup-co.example"})
    coded = client.post(
        URL,
        json={**payload, "admin_email": "other_x@signup-co.example", "referral_code": "ada-ref"},
    )

    assert plain.status_code == coded.status_code == 400
    assert plain.json() == coded.json()
    assert _attributions(db) == []


def test_response_shape_identical_with_valid_invalid_and_no_code(client, db):
    _marketer(db)

    shapes = []
    for extra in ({}, {"referral_code": "ada-ref"}, {"referral_code": "nope-nope"}):
        resp = _signup(client, **extra)
        assert resp.status_code == 201
        shapes.append(set(resp.json().keys()))

    assert (
        shapes[0]
        == shapes[1]
        == shapes[2]
        == {
            "message",
            "tenant_id",
            "admin_email",
            "verification_required",
        }
    )


def test_self_referral_marked_and_gmail_not_treated_as_same_domain(client, db):
    _marketer(db, code="corp-ref", email="boss@marketer-co.example")
    _marketer(db, code="gm-ref", email="someone@gmail.com")

    same_email = client.post(
        URL,
        json={**_payload(), "admin_email": "boss@marketer-co.example", "referral_code": "corp-ref"},
    )
    same_domain = client.post(
        URL,
        json={
            **_payload(),
            "admin_email": "Colleague@Marketer-Co.example",
            "referral_code": "corp-ref",
        },
    )
    other_domain = _signup(client, referral_code="corp-ref")
    gmail_other = client.post(
        URL,
        json={**_payload(), "admin_email": "stranger@gmail.com", "referral_code": "gm-ref"},
    )
    gmail_same = client.post(
        URL,
        json={**_payload(), "admin_email": "SomeOne@gmail.com", "referral_code": "gm-ref"},
    )

    flags = {}
    for name, resp in {
        "same_email": same_email,
        "same_domain": same_domain,
        "other_domain": other_domain,
        "gmail_other": gmail_other,
        "gmail_same": gmail_same,
    }.items():
        assert resp.status_code == 201, (name, resp.json())
        row = db.execute(
            select(ReferralAttribution).where(
                ReferralAttribution.tenant_id == UUID(resp.json()["tenant_id"])
            )
        ).scalar_one()
        flags[name] = row.self_referral

    assert flags == {
        "same_email": True,
        "same_domain": True,
        "other_domain": False,
        "gmail_other": False,
        "gmail_same": True,
    }


def test_origin_hash_not_reversible_and_no_raw_ip_stored(client, db):
    _marketer(db)

    resp = _signup(client, referral_code="ada-ref")
    assert resp.status_code == 201

    db.expire_all()
    origin = db.execute(select(SignupOrigin)).scalar_one()
    assert str(origin.tenant_id) == resp.json()["tenant_id"]
    assert re.fullmatch(r"[0-9a-f]{64}", origin.origin_hash)
    # TestClient's peer is the fixed host "testclient"; neither it nor the UA is stored anywhere.
    for value in (str(origin.tenant_id), origin.origin_hash, str(origin.id)):
        assert "testclient" not in value
        assert "UniqueAgentMarker" not in value
    assert origin.origin_hash == origin_hash("testclient", UA)


def test_origin_hash_properties():
    a = origin_hash("203.0.113.7", UA)
    assert a == origin_hash("203.0.113.7", UA)  # stable
    assert re.fullmatch(r"[0-9a-f]{64}", a)
    assert a != origin_hash("203.0.113.8", UA)
    assert a != origin_hash("203.0.113.7", UA + "x")
    # ("ab","c") and ("a","bc") must not collide
    assert origin_hash("ab", "c") != origin_hash("a", "bc")
    # keyed: not a plain digest of the raw values
    import hashlib

    assert a != hashlib.sha256(f"203.0.113.7{UA}".encode()).hexdigest()
    assert "203.0.113.7" not in a
    for empty in (("", ""), (None, None), ("", None)):
        h = origin_hash(*empty)
        assert re.fullmatch(r"[0-9a-f]{64}", h)
        assert h == origin_hash(*empty)


def test_signup_origin_only_written_when_attributed(client, db):
    _marketer(db)

    assert _signup(client).status_code == 201
    assert _signup(client, referral_code="nope-nope").status_code == 201
    assert db.execute(select(SignupOrigin)).first() is None

    assert _signup(client, referral_code="ada-ref").status_code == 201
    db.expire_all()
    assert len(list(db.execute(select(SignupOrigin)).scalars())) == 1


def test_extra_forbid_still_rejects_unknown_fields(client, db):
    _marketer(db)

    resp = _signup(client, referral_code="ada-ref", surprise_field="x")

    assert resp.status_code == 422
    assert any("surprise_field" in str(err.get("loc")) for err in resp.json()["detail"])
    assert _attributions(db) == []


def test_attribution_failure_never_blocks_signup(client, db):
    _marketer(db)

    with patch(
        "app.services.company_registration_service.attribute_signup",
        side_effect=RuntimeError("db exploded"),
    ):
        resp = _signup(client, referral_code="ada-ref")

    # (The tenant row itself is not asserted: the harness shares one outer transaction, which the
    # failure path's session rollback also unwinds; in production the signup commit is durable.)
    assert resp.status_code == 201
    assert set(resp.json()) == {"message", "tenant_id", "admin_email", "verification_required"}
    assert _attributions(db) == []


def test_origin_hash_failure_never_blocks_signup(client, db):
    _marketer(db)

    with patch(
        "app.services.company_registration_service.origin_hash",
        side_effect=RuntimeError("boom"),
    ):
        resp = _signup(client, referral_code="ada-ref")

    assert resp.status_code == 201
    assert _attributions(db) == []


def _tenant(db) -> Tenant:
    tenant = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name="T",
        public_slug=uuid4().hex[:12],
        api_key_hash="placeholder-hash",
    )
    db.add(tenant)
    db.commit()
    return tenant


def test_double_attribution_for_same_tenant_does_not_raise(db):
    first_marketer = _marketer(db, code="first-ref")
    _marketer(db, code="second-ref")
    tenant = _tenant(db)
    h = origin_hash("203.0.113.7", UA)

    first = attribute_signup(
        db, tenant, "a@signup-co.example", "first-ref", AttributionSource.FORM, h
    )
    second = attribute_signup(
        db, tenant, "a@signup-co.example", "second-ref", AttributionSource.COOKIE, h
    )

    assert first is not None
    assert second is None or second.marketer_id == first_marketer.id  # first link wins
    db.expire_all()
    rows = list(db.execute(select(ReferralAttribution)).scalars())
    assert len(rows) == 1 and rows[0].marketer_id == first_marketer.id
    assert len(list(db.execute(select(SignupOrigin)).scalars())) == 1
    # the session is still usable after the swallowed IntegrityError
    assert db.execute(select(Tenant).where(Tenant.id == tenant.id)).scalar_one() is not None


@pytest.mark.parametrize("code", [None, "", "  ", "x" * 99, 42, ["a"], "ab", "nope-nope"])
def test_attribute_signup_returns_none_for_bad_input(db, code):
    _marketer(db)
    tenant = _tenant(db)

    assert (
        attribute_signup(db, tenant, "a@signup-co.example", code, AttributionSource.FORM, "0" * 64)
        is None
    )
    assert _attributions(db) == []


def test_attribute_signup_swallows_db_errors(db):
    _marketer(db)
    tenant = _tenant(db)

    with patch.object(
        attribution_module, "_resolve_active_marketer", side_effect=RuntimeError("x")
    ):
        assert (
            attribute_signup(
                db, tenant, "a@signup-co.example", "ada-ref", AttributionSource.FORM, "0" * 64
            )
            is None
        )


def test_integrity_error_is_contained_in_a_savepoint_on_postgres(db_session):
    """A conflicting insert (a stray origin row) must roll back alone, leaving the session usable."""
    marketer = Marketer(name="Pg Marketer", email="pg@marketer-co.example", code="pg-ref")
    tenant = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name="T",
        public_slug=uuid4().hex[:12],
        api_key_hash="placeholder-hash",
    )
    db_session.add_all([marketer, tenant])
    db_session.flush()
    db_session.add(SignupOrigin(tenant_id=tenant.id, origin_hash="0" * 64))
    db_session.flush()
    tenant_id = tenant.id

    result = attribute_signup(
        db_session, tenant, "a@signup-co.example", "pg-ref", AttributionSource.FORM, "1" * 64
    )

    assert result is None
    assert db_session.execute(select(ReferralAttribution)).first() is None
    # The earlier work in the same transaction survived and the session still works.
    assert db_session.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one()
    origin = db_session.execute(select(SignupOrigin)).scalar_one()
    assert origin.origin_hash == "0" * 64
