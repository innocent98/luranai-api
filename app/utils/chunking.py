"""Text chunking utilities for document processing."""

import re
from typing import Any


class TextChunker:
    """
    Utility for splitting text into overlapping chunks.

    Optimized for RAG applications with configurable chunk size and overlap.
    """

    def __init__(
        self,
        chunk_size: int = 250,
        chunk_overlap: int = 50,
        min_chunk_size: int = 60,
    ):
        """
        Initialize text chunker.

        Args:
            chunk_size: Target number of tokens per chunk (approximate)
            chunk_overlap: Number of tokens to overlap between chunks
            min_chunk_size: Minimum chunk size in tokens
        """
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.min_chunk_size = min_chunk_size

    def _estimate_token_count(self, text: str) -> int:
        """
        Accurate token count using tiktoken.

        Args:
            text: Input text

        Returns:
            Accurate token count
        """
        from app.utils.token_counter import count_tokens

        return count_tokens(text, model="gpt-4o-mini")

    def _split_by_sentences(self, text: str) -> list[str]:
        """
        Split text into sentences.

        Args:
            text: Input text

        Returns:
            List of sentences
        """
        # Split by common sentence endings, but preserve them
        sentence_pattern = r"(?<=[.!?])\s+(?=[A-Z])"
        sentences = re.split(sentence_pattern, text)

        # Filter out empty strings
        return [s.strip() for s in sentences if s.strip()]

    # A block of text this small (tokens) is a heading or fragment, not a topic of its own.
    MIN_UNIT_TOKENS = 12
    # Past this many atoms the document is big enough that per-paragraph chunks would multiply
    # embedding calls (one tenant's upload shares the embedding rate limit with every tenant's
    # chat), so small pieces are packed to about half a chunk instead of 12 tokens.
    LARGE_DOCUMENT_ATOMS = 300
    # Blocks shorter than this and without sentence punctuation read as headings/questions.
    HEADING_MAX_CHARS = 120
    _QUESTION_LINE = re.compile(r"^\s*(?:q|question)\s*[:.)]", re.IGNORECASE)

    def _split_into_units(self, text: str) -> list[str]:
        """Split text into topical units: blank-line blocks, with Q&A and headings kept whole."""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]

        # A block holding several "Q:" lines (no blank line between them) is split per question.
        expanded: list[str] = []
        for block in blocks:
            lines = block.split("\n")
            question_at = [i for i, ln in enumerate(lines) if self._QUESTION_LINE.match(ln)]
            if len(question_at) < 2:
                expanded.append(block)
                continue
            bounds = question_at + [len(lines)]
            if question_at[0] > 0:
                expanded.append("\n".join(lines[: question_at[0]]).strip())
            for a, b in zip(bounds, bounds[1:], strict=False):
                expanded.append("\n".join(lines[a:b]).strip())
        blocks = [b for b in expanded if b]

        units: list[str] = []
        i = 0
        while i < len(blocks):
            block = blocks[i]
            nxt = blocks[i + 1] if i + 1 < len(blocks) else None
            if nxt is not None and self._is_heading_like(block) and not self._is_heading_like(nxt):
                units.append(f"{block}\n{nxt}")
                i += 2
            else:
                units.append(block)
                i += 1
        return units

    def _is_heading_like(self, block: str) -> bool:
        """A single short line that does not end like a sentence: a title or a question."""
        if "\n" in block or len(block) > self.HEADING_MAX_CHARS:
            return False
        return not block.rstrip().endswith((".", "!", ";"))

    def _split_oversize(self, unit: str) -> list[str]:
        """Break a unit larger than `chunk_size` along line, then sentence, boundaries."""
        pieces: list[str] = []
        for line in unit.split("\n"):
            line = line.strip()
            if line:
                pieces.extend(self._split_by_sentences(line))
        return pieces

    def chunk_text(
        self,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Split text into topical chunks with metadata.

        Paragraphs / Q&A pairs / headed sections become their own chunk so a short question
        matches one topic instead of a blend of several. Pieces too small to stand alone are
        merged with a neighbour, units larger than `chunk_size` are packed by sentence with
        overlap, and nothing is dropped for being short: any non-empty text yields at least
        one chunk.

        Args:
            text: Input text to chunk
            metadata: Optional metadata to include with each chunk

        Returns:
            List of chunk dictionaries with content and metadata
        """
        if not text or not text.strip():
            return []

        # Atoms: (content, tokens, mergeable). Oversize pieces are never merged with neighbours.
        atoms: list[tuple[str, int, bool]] = []
        for unit in self._split_into_units(text):
            tokens = self._estimate_token_count(unit)
            if tokens <= self.chunk_size:
                atoms.append((unit, tokens, True))
                continue
            for packed in self._pack_sentences(self._split_oversize(unit)):
                atoms.append((packed["content"], packed["token_count"], False))

        min_unit = (
            self.MIN_UNIT_TOKENS
            if len(atoms) <= self.LARGE_DOCUMENT_ATOMS
            else self.chunk_size // 2
        )
        merged: list[tuple[str, int]] = []
        buf: list[str] = []
        buf_tokens = 0

        def flush() -> None:
            nonlocal buf, buf_tokens
            if buf:
                merged.append(("\n\n".join(buf), buf_tokens))
            buf, buf_tokens = [], 0

        for content, tokens, mergeable in atoms:
            if buf and (not mergeable or buf_tokens + tokens > self.chunk_size):
                flush()
            if not mergeable:
                merged.append((content, tokens))
                continue
            buf.append(content)
            buf_tokens += tokens
            if buf_tokens >= min_unit:
                flush()
        flush()

        # A tiny tail (e.g. "Thanks!") rides along with the chunk before it when it fits.
        if (
            len(merged) > 1
            and merged[-1][1] < min_unit
            and merged[-2][1] + merged[-1][1] <= self.chunk_size
        ):
            (prev, prev_tokens), (last, last_tokens) = merged[-2], merged[-1]
            merged[-2:] = [(f"{prev}\n\n{last}", prev_tokens + last_tokens)]

        return [
            {
                "content": content,
                "chunk_index": idx,
                "token_count": tokens,
                "metadata": dict(metadata or {}),
            }
            for idx, (content, tokens) in enumerate(merged)
        ]

    def _pack_sentences(
        self,
        sentences: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Pack pre-split sentences into overlapping chunks of at most ~`chunk_size` tokens."""
        if not sentences:
            return []

        chunks: list[dict[str, Any]] = []
        current_chunk: list[str] = []
        current_token_count = 0
        chunk_index = 0

        for sentence in sentences:
            sentence_tokens = self._estimate_token_count(sentence)

            # If single sentence exceeds chunk size, split it further
            if sentence_tokens > self.chunk_size:
                # Flush current chunk if it has content
                if current_chunk:
                    chunk_content = " ".join(current_chunk)
                    chunks.append(
                        {
                            "content": chunk_content,
                            "chunk_index": chunk_index,
                            "token_count": current_token_count,
                            "metadata": metadata or {},
                        }
                    )
                    chunk_index += 1
                    current_chunk = []
                    current_token_count = 0

                # Split long sentence by words
                words = sentence.split()
                word_chunk: list[str] = []
                word_token_count = 0

                for word in words:
                    word_tokens = self._estimate_token_count(word)

                    if word_token_count + word_tokens > self.chunk_size:
                        if word_chunk:
                            chunk_content = " ".join(word_chunk)
                            chunks.append(
                                {
                                    "content": chunk_content,
                                    "chunk_index": chunk_index,
                                    "token_count": word_token_count,
                                    "metadata": {**(metadata or {}), "split_type": "long_sentence"},
                                }
                            )
                            chunk_index += 1

                            # Keep overlap
                            overlap_words = word_chunk[-self.chunk_overlap :]
                            word_chunk = overlap_words + [word]
                            word_token_count = self._estimate_token_count(" ".join(word_chunk))
                        else:
                            word_chunk = [word]
                            word_token_count = word_tokens
                    else:
                        word_chunk.append(word)
                        word_token_count += word_tokens

                # Add remaining words as chunk
                if word_chunk:
                    chunk_content = " ".join(word_chunk)
                    chunks.append(
                        {
                            "content": chunk_content,
                            "chunk_index": chunk_index,
                            "token_count": word_token_count,
                            "metadata": {**(metadata or {}), "split_type": "long_sentence"},
                        }
                    )
                    chunk_index += 1

                continue

            # Check if adding this sentence exceeds chunk size
            if current_token_count + sentence_tokens > self.chunk_size:
                # Save current chunk
                if current_chunk:
                    chunk_content = " ".join(current_chunk)
                    chunks.append(
                        {
                            "content": chunk_content,
                            "chunk_index": chunk_index,
                            "token_count": current_token_count,
                            "metadata": metadata or {},
                        }
                    )
                    chunk_index += 1

                    # Create overlap by keeping last few sentences
                    overlap_sentences: list[str] = []
                    overlap_tokens = 0

                    for prev_sentence in reversed(current_chunk):
                        prev_tokens = self._estimate_token_count(prev_sentence)
                        if overlap_tokens + prev_tokens <= self.chunk_overlap:
                            overlap_sentences.insert(0, prev_sentence)
                            overlap_tokens += prev_tokens
                        else:
                            break

                    current_chunk = overlap_sentences + [sentence]
                    current_token_count = overlap_tokens + sentence_tokens
                else:
                    current_chunk = [sentence]
                    current_token_count = sentence_tokens
            else:
                current_chunk.append(sentence)
                current_token_count += sentence_tokens

        # Add final chunk if it meets minimum size
        if current_chunk:
            chunk_content = " ".join(current_chunk)
            chunks.append(
                {
                    "content": chunk_content,
                    "chunk_index": chunk_index,
                    "token_count": current_token_count,
                    "metadata": metadata or {},
                }
            )

        return chunks

    def chunk_by_sections(
        self,
        text: str,
        section_separator: str = "\n\n",
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Chunk text by sections (paragraphs) with overlap.

        Args:
            text: Input text
            section_separator: Separator for sections (default: double newline)
            metadata: Optional metadata

        Returns:
            List of chunk dictionaries
        """
        sections = text.split(section_separator)
        all_chunks: list[dict[str, Any]] = []

        for section_idx, section in enumerate(sections):
            if not section.strip():
                continue

            section_meta = {
                **(metadata or {}),
                "section_index": section_idx,
            }

            section_chunks = self.chunk_text(section, metadata=section_meta)
            all_chunks.extend(section_chunks)

        # Re-index chunks
        for idx, chunk in enumerate(all_chunks):
            chunk["chunk_index"] = idx

        return all_chunks


# Global instance
text_chunker = TextChunker(
    chunk_size=250,  # ~187 words (~1000 chars) - Optimal for semantic precision
    chunk_overlap=50,  # ~37 words overlap - Preserve context across chunks
    min_chunk_size=60,  # ~45 words minimum - Allow smaller focused chunks
)
