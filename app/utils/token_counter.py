"""Accurate token counting using tiktoken."""

from functools import lru_cache

import tiktoken
from tiktoken.core import Encoding


@lru_cache(maxsize=1)
def get_encoding(model: str = "gpt-4o-mini") -> Encoding:
    """Get tiktoken encoding (cached)."""
    return tiktoken.encoding_for_model(model)


def count_tokens(text: str, model: str = "gpt-4o-mini") -> int:
    """Count tokens accurately using tiktoken."""
    encoding = get_encoding(model)
    return len(encoding.encode(text))


def count_messages_tokens(messages: list[dict[str, str]], model: str = "gpt-4o-mini") -> int:
    """Count tokens for chat messages (includes message formatting overhead)."""
    encoding = get_encoding(model)
    tokens = 0

    for message in messages:
        tokens += 4  # Message formatting overhead
        for key, value in message.items():
            tokens += len(encoding.encode(value))
            if key == "name":
                tokens += -1  # Role is always required and always 1 token

    tokens += 2  # Reply priming
    return tokens


def truncate_to_token_limit(text: str, max_tokens: int, model: str = "gpt-4o-mini") -> str:
    """Truncate text to fit within token limit."""
    encoding = get_encoding(model)
    tokens = encoding.encode(text)

    if len(tokens) <= max_tokens:
        return text

    truncated_tokens = tokens[:max_tokens]
    return encoding.decode(truncated_tokens)
