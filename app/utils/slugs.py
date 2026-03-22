"""Helpers for generating and validating public slugs."""

import re

PUBLIC_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PUBLIC_SLUG_MAX_LENGTH = 50
PUBLIC_SLUG_MIN_LENGTH = 3


def normalize_public_slug(value: str) -> str:
    """Normalize arbitrary text into a lowercase public slug candidate."""
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    return normalized[:PUBLIC_SLUG_MAX_LENGTH].strip("-")


def is_valid_public_slug(value: str) -> bool:
    """Return True when the slug matches the supported public format."""
    return bool(
        value
        and PUBLIC_SLUG_MIN_LENGTH <= len(value) <= PUBLIC_SLUG_MAX_LENGTH
        and PUBLIC_SLUG_PATTERN.fullmatch(value)
    )
