import pytest
from uuid import uuid4

from app.services.voice.session_tokens import VoiceSessionTokenStore


@pytest.mark.asyncio
async def test_mint_then_resolve_round_trips_context():
    store = VoiceSessionTokenStore()
    tenant_id, session_id = uuid4(), uuid4()

    token = await store.mint(tenant_id=tenant_id, luran_session_id=session_id, text_out=True)

    assert isinstance(token, str) and len(token) >= 32
    ctx = await store.resolve(token)
    assert ctx is not None
    assert ctx.tenant_id == tenant_id
    assert ctx.luran_session_id == session_id
    assert ctx.text_out is True


@pytest.mark.asyncio
async def test_resolve_unknown_token_returns_none():
    store = VoiceSessionTokenStore()
    assert await store.resolve("not-a-real-token") is None


@pytest.mark.asyncio
async def test_revoke_makes_token_unresolvable():
    store = VoiceSessionTokenStore()
    token = await store.mint(tenant_id=uuid4(), luran_session_id=uuid4(), text_out=False)
    await store.revoke(token)
    assert await store.resolve(token) is None
