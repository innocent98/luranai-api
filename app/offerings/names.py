"""A once-per-request index over the tenant's item names.

The guard and the offer detector ask the same questions of every sentence ("does this name an
item?", "which name is this number part of?"). Answering them by looping over all names made the
cost grow with `sentences x items` (a Growth tenant has up to 2,000 items). The index maps each
name word to the names that contain it, so a sentence only looks at names that share a word with
it: the cost follows the sentence, not the catalog.

No imports from the rest of `app.offerings` (grounding, guard and amounts all use this module).
"""

import re
from collections.abc import Iterable, Iterator

NAME_STOP = {"out", "and", "the", "for", "with", "new", "set"}
_WORD = re.compile(r"[a-z0-9]+")
_QTY_SKIP = {"with", "pack", "from", "that", "this"}


def singular(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith(("ches", "shes", "sses", "xes")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def name_tokens(name: str) -> frozenset[str]:
    """The words that identify an item name: 3+ letters or any number, singularised."""
    return frozenset(
        singular(t)
        for t in _WORD.findall(name.lower())
        if (len(t) >= 3 or t.isdigit()) and t not in NAME_STOP
    )


def words_of(text: str) -> set[str]:
    return {singular(w) for w in _WORD.findall(text.lower())}


class NameIndex:
    def __init__(self, names: Iterable[str]):
        self.names: list[str] = list(dict.fromkeys(n for n in names if n))
        self.low = [n.lower() for n in self.names]
        self.tokens = [name_tokens(n) for n in self.names]
        self.by_word: dict[str, list[int]] = {}
        self.tokenless: list[int] = []
        for i, toks in enumerate(self.tokens):
            if not toks:
                self.tokenless.append(i)
            for t in toks:
                self.by_word.setdefault(t, []).append(i)
        self.low_set = frozenset(self.low)
        self.numbers = frozenset(n for name in self.names for n in re.findall(r"\d+", name))
        self.quantity_words = frozenset(
            w
            for name in self.names
            for w in re.findall(r"[a-z]{4,}", name.lower())
            if w not in _QTY_SKIP
        )
        self.words = frozenset(
            w for name in self.names for w in re.findall(r"[a-z0-9]{3,}", name.lower())
        )

    def __iter__(self) -> Iterator[str]:
        return iter(self.names)

    def __len__(self) -> int:
        return len(self.names)

    def __bool__(self) -> bool:
        return bool(self.names)

    def candidates(self, words: set[str]) -> list[int]:
        """Indexes of the names that share a word with `words` (plus names with no word at all)."""
        seen: dict[int, None] = {}
        for w in words:
            for i in self.by_word.get(w, ()):
                seen[i] = None
        for i in self.tokenless:
            seen[i] = None
        return list(seen)

    def mentions(self, text: str) -> list[tuple[int, int, int]]:
        """(start, end, name index) for every full-name occurrence in `text` (case-insensitive)."""
        cached = self.__dict__.setdefault("_mentions", {}).get(text)
        if cached is not None:
            return cached
        low = text.lower()
        if len(low) != len(text):  # a character that changes length when lowered: never guess
            return []
        out = []
        for i in self.candidates(words_of(text)):
            name = self.low[i]
            at = low.find(name)
            while at != -1:
                out.append((at, at + len(name), i))
                at = low.find(name, at + len(name))
        memo = self.__dict__["_mentions"]
        if len(memo) < 512:
            memo[text] = out
        return out

    def blank(self, text: str) -> str:
        """`text` with every full-name occurrence replaced by spaces (same length)."""
        spans = [(a, b) for a, b, _i in self.mentions(text)]
        if not spans:
            return text
        chars = list(text)
        for a, b in spans:
            chars[a:b] = " " * (b - a)
        return "".join(chars)


def as_index(names: "Iterable[str] | NameIndex") -> NameIndex:
    return names if isinstance(names, NameIndex) else NameIndex(names)
