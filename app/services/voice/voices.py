"""Curated ElevenLabs voices offered to tenants.

Deliberately a fixed list rather than a live proxy of the ElevenLabs voices
API: predictable quality and cost, no third-party call on a settings page
load, and every voice vetted for support conversations.

TODO(voice-ids): the `id` values below are PLACEHOLDERS and MUST be replaced
with real ids from our ElevenLabs account library before this ships. Fabricated
ids look plausible and fail only at mint time, far from the mistake. Pick
voices spanning gender, accent and pace, audition each one, and record which
you chose and why in the SOP.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CuratedVoice:
    id: str
    name: str
    description: str
    preview_url: str


CURATED_VOICES: list[CuratedVoice] = [
    CuratedVoice(
        id="REPLACE_ME_voice_1",
        name="Placeholder One",
        description="Replace with a real ElevenLabs voice before shipping",
        preview_url="https://storage.googleapis.com/eleven-public-prod/REPLACE_ME.mp3",
    ),
]


def is_curated_voice_id(voice_id: str) -> bool:
    """True when `voice_id` is one we offer. Used to reject arbitrary ids at
    write time rather than letting them fail at mint time."""
    return any(v.id == voice_id for v in CURATED_VOICES)
