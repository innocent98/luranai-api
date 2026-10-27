"""provider -> adapter registry."""

from __future__ import annotations

import httpx

from app.channels.base import ChannelAdapter
from app.channels.cloud_api import CloudApiAdapter
from app.channels.linked_device import LinkedDeviceAdapter

# Tests mount the in-process fake sidecar here (see tests/fakes/fake_sidecar.py).
_linked_device_transport: httpx.AsyncBaseTransport | None = None


def set_linked_device_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _linked_device_transport
    _linked_device_transport = transport


def get_adapter(provider: str = "linked_device") -> ChannelAdapter:
    if provider == "linked_device":
        return LinkedDeviceAdapter(transport=_linked_device_transport)  # type: ignore[return-value]
    if provider == "cloud_api":
        return CloudApiAdapter()  # type: ignore[return-value]
    raise ValueError(f"unknown channel provider: {provider}")
