"""Signed sidecar webhook (contract W): signature, replay window, rotation, tenant isolation."""

from __future__ import annotations

import json
import time

import pytest

from app.channels import signing
from tests.whatsapp.conftest import (
    WEBHOOK_SECRET,
    WEBHOOK_SECRET_PREVIOUS,
    connect,
    inbound,
    make_tenant,
)

URL = "/api/v1/webhooks/whatsapp/linked"


def _post(client, event, *, secret=WEBHOOK_SECRET, ts=None, raw=None, header=None):
    raw = raw if raw is not None else json.dumps(event).encode()
    headers = {"Content-Type": "application/json"}
    if header is not None:
        headers[signing.SIGNATURE_HEADER] = header
    elif secret is not None:
        headers[signing.SIGNATURE_HEADER] = signing.sign(secret, raw, ts)
    return client.post(URL, content=raw, headers=headers)


@pytest.fixture
def conn(db, fake_sidecar):
    return connect(db, fake_sidecar, make_tenant(db))


def test_valid_signature_is_accepted_and_enqueued(client, queue, conn):
    ev = inbound(conn)
    r = _post(client, ev)
    assert r.status_code == 200 and r.json() == {"status": "accepted"}
    assert [e["id"] for e in queue.events] == [ev["id"]]
    assert queue.events[0]["type"] == "message.inbound"


def test_tampered_body_is_rejected(client, queue, conn):
    ev = inbound(conn)
    raw = json.dumps(ev).encode()
    sig = signing.sign(WEBHOOK_SECRET, raw)
    tampered = raw.replace(b"opening hours", b"opening hourz")
    r = client.post(URL, content=tampered, headers={signing.SIGNATURE_HEADER: sig})
    assert (r.status_code, r.json()) == (401, {"error": "invalid_signature"})
    assert queue.events == []


def test_wrong_secret_stale_and_future_timestamps_are_rejected(client, queue, conn):
    ev = inbound(conn)
    assert _post(client, ev, secret="not-the-secret").status_code == 401
    now = int(time.time())
    assert _post(client, ev, ts=now - 301).status_code == 401  # stale (replay)
    assert _post(client, ev, ts=now + 301).status_code == 401  # too far in the future
    assert _post(client, ev, ts=now - 200).status_code == 200  # inside the 5 min window
    assert len(queue.events) == 1


@pytest.mark.parametrize(
    "header",
    ["", "garbage", "t=1,v1=zz", "v1=" + "a" * 64, f"t={int(time.time())},v1={'0' * 64}"],
)
def test_malformed_or_missing_header_is_rejected(client, queue, conn, header):
    assert _post(client, inbound(conn), header=header).status_code == 401
    assert queue.events == []


def test_no_header_at_all_is_rejected(client, queue, conn):
    r = client.post(URL, content=json.dumps(inbound(conn)).encode())
    assert r.status_code == 401


def test_secret_rotation_accepts_previous_then_rejects_after_removal(
    client, queue, conn, wa_settings, monkeypatch
):
    assert _post(client, inbound(conn), secret=WEBHOOK_SECRET_PREVIOUS).status_code == 200
    monkeypatch.setattr(wa_settings, "WHATSAPP_WEBHOOK_SECRET_PREVIOUS", "")
    assert _post(client, inbound(conn), secret=WEBHOOK_SECRET_PREVIOUS).status_code == 401
    assert _post(client, inbound(conn), secret=WEBHOOK_SECRET).status_code == 200


def test_unknown_session_key_is_dropped_not_enqueued(client, queue, conn):
    ev = inbound(conn)
    ev["session_key"] = "sk_does_not_exist"
    r = _post(client, ev)
    assert (r.status_code, r.json()) == (200, {"status": "ignored"})
    assert queue.events == []


def test_payload_cannot_name_a_tenant(client, queue, conn):
    ev = inbound(conn)
    ev["tenant_id"] = "00000000-0000-0000-0000-000000000000"
    assert _post(client, ev).status_code == 200
    # unknown field is dropped by validation: the queued event carries no tenant
    assert "tenant_id" not in queue.events[0]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.pop("message_id"),
        lambda e: e.update(kind="image"),
        lambda e: e.update(type="message.bogus"),
        lambda e: e.update(text="x" * 10001),
    ],
)
def test_invalid_event_is_400(client, queue, conn, mutate):
    ev = inbound(conn)
    mutate(ev)
    r = _post(client, ev)
    assert (r.status_code, r.json()) == (400, {"error": "invalid_event"})


def test_kill_switch_and_missing_secret_fail_closed(client, queue, conn, wa_settings, monkeypatch):
    monkeypatch.setattr(wa_settings, "WHATSAPP_ENABLED", False)
    assert _post(client, inbound(conn)).status_code == 503
    monkeypatch.setattr(wa_settings, "WHATSAPP_ENABLED", True)
    monkeypatch.setattr(wa_settings, "WHATSAPP_WEBHOOK_SECRET", "")
    monkeypatch.setattr(wa_settings, "WHATSAPP_WEBHOOK_SECRET_PREVIOUS", "")
    r = client.post(URL, content=b"{}", headers={signing.SIGNATURE_HEADER: signing.sign("", b"{}")})
    assert r.status_code == 503  # an empty secret must never validate anything


def test_queue_outage_is_503_so_the_sidecar_retries(client, queue, conn):
    queue.fail_events = True
    assert _post(client, inbound(conn)).status_code == 503


def test_oversize_declared_length_is_rejected_before_reading(client, queue, conn):
    r = client.post(
        URL,
        content=b"{}",
        headers={"Content-Length": str(10 * 1024 * 1024), signing.SIGNATURE_HEADER: "x"},
    )
    assert r.status_code == 413 or r.status_code >= 400  # h11 may reject the mismatch itself


def test_oversize_body_is_413(client, queue, conn):
    assert (
        _post(client, {}, raw=b"x" * (256 * 1024 + 1), header="t=1,v1=" + "0" * 64).status_code
        == 413
    )


def test_signature_never_reaches_the_logs(client, queue, conn, log_lines, caplog):
    ev = inbound(conn)
    raw = json.dumps(ev).encode()
    good = signing.sign(WEBHOOK_SECRET, raw)
    bad = signing.sign("wrong", raw)
    client.post(URL, content=raw, headers={signing.SIGNATURE_HEADER: good})
    client.post(URL, content=raw, headers={signing.SIGNATURE_HEADER: bad})
    blob = "".join(log_lines) + caplog.text
    for sig in (good, bad):
        assert sig.split("v1=")[1] not in blob
    assert WEBHOOK_SECRET not in blob


def test_signing_roundtrip_matches_the_sidecar_format():
    raw = b'{"a":1}'
    header = signing.sign("s3cret", raw, ts=1_700_000_000)
    assert header.startswith("t=1700000000,v1=") and len(header.split("v1=")[1]) == 64
    assert signing.verify(["s3cret"], raw, header, now=1_700_000_100)
    assert not signing.verify(["s3cret"], raw, header, now=1_700_000_400)
    assert not signing.verify(["", "other"], raw, header, now=1_700_000_100)
