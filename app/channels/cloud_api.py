"""Option B adapter slot (Meta WhatsApp Cloud API). Intentionally NOT implemented.

Starts only after Meta Tech Provider approval (plan task T14). The registry knows the
provider name so schemas and callers do not change when it lands.
"""

from __future__ import annotations

from app.channels.base import ChannelCapabilities


class CloudApiAdapter:
    provider = "cloud_api"
    capabilities = ChannelCapabilities(
        rich_buttons=True, templates_required_outside_window=True, media_kinds=("image",)
    )

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        raise NotImplementedError(f"cloud_api adapter is not built yet ({name})")
