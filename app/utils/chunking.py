"""Text chunking utilities for document processing."""

from typing import List, Dict, Any
import re


class TextChunker:
    """
    Utility for splitting text into overlapping chunks.

    Optimized for RAG applications with configurable chunk size and overlap.
    """

    def __init__(
        self,
        chunk_size: int = 400,
        chunk_overlap: int = 50,
        min_chunk_size: int = 100,
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

    def _split_by_sentences(self, text: str) -> List[str]:
        """
        Split text into sentences.

        Args:
            text: Input text

        Returns:
            List of sentences
        """
        # Split by common sentence endings, but preserve them
        sentence_pattern = r'(?<=[.!?])\s+(?=[A-Z])'
        sentences = re.split(sentence_pattern, text)

        # Filter out empty strings
        return [s.strip() for s in sentences if s.strip()]

    def chunk_text(
        self,
        text: str,
        metadata: Dict[str, Any] = None,
    ) -> List[Dict[str, Any]]:
        """
        Split text into overlapping chunks with metadata.

        Args:
            text: Input text to chunk
            metadata: Optional metadata to include with each chunk

        Returns:
            List of chunk dictionaries with content and metadata
        """
        if not text or not text.strip():
            return []

        # Split into sentences
        sentences = self._split_by_sentences(text)

        if not sentences:
            return []

        chunks = []
        current_chunk = []
        current_token_count = 0
        chunk_index = 0

        for sentence in sentences:
            sentence_tokens = self._estimate_token_count(sentence)

            # If single sentence exceeds chunk size, split it further
            if sentence_tokens > self.chunk_size:
                # Flush current chunk if it has content
                if current_chunk:
                    chunk_content = ' '.join(current_chunk)
                    chunks.append({
                        'content': chunk_content,
                        'chunk_index': chunk_index,
                        'token_count': current_token_count,
                        'metadata': metadata or {},
                    })
                    chunk_index += 1
                    current_chunk = []
                    current_token_count = 0

                # Split long sentence by words
                words = sentence.split()
                word_chunk = []
                word_token_count = 0

                for word in words:
                    word_tokens = self._estimate_token_count(word)

                    if word_token_count + word_tokens > self.chunk_size:
                        if word_chunk:
                            chunk_content = ' '.join(word_chunk)
                            chunks.append({
                                'content': chunk_content,
                                'chunk_index': chunk_index,
                                'token_count': word_token_count,
                                'metadata': {**(metadata or {}), 'split_type': 'long_sentence'},
                            })
                            chunk_index += 1

                            # Keep overlap
                            overlap_words = word_chunk[-self.chunk_overlap:]
                            word_chunk = overlap_words + [word]
                            word_token_count = self._estimate_token_count(' '.join(word_chunk))
                        else:
                            word_chunk = [word]
                            word_token_count = word_tokens
                    else:
                        word_chunk.append(word)
                        word_token_count += word_tokens

                # Add remaining words as chunk
                if word_chunk:
                    chunk_content = ' '.join(word_chunk)
                    chunks.append({
                        'content': chunk_content,
                        'chunk_index': chunk_index,
                        'token_count': word_token_count,
                        'metadata': {**(metadata or {}), 'split_type': 'long_sentence'},
                    })
                    chunk_index += 1

                continue

            # Check if adding this sentence exceeds chunk size
            if current_token_count + sentence_tokens > self.chunk_size:
                # Save current chunk
                if current_chunk:
                    chunk_content = ' '.join(current_chunk)
                    chunks.append({
                        'content': chunk_content,
                        'chunk_index': chunk_index,
                        'token_count': current_token_count,
                        'metadata': metadata or {},
                    })
                    chunk_index += 1

                    # Create overlap by keeping last few sentences
                    overlap_sentences = []
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
        if current_chunk and current_token_count >= self.min_chunk_size:
            chunk_content = ' '.join(current_chunk)
            chunks.append({
                'content': chunk_content,
                'chunk_index': chunk_index,
                'token_count': current_token_count,
                'metadata': metadata or {},
            })

        return chunks

    def chunk_by_sections(
        self,
        text: str,
        section_separator: str = "\n\n",
        metadata: Dict[str, Any] = None,
    ) -> List[Dict[str, Any]]:
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
        all_chunks = []

        for section_idx, section in enumerate(sections):
            if not section.strip():
                continue

            section_meta = {
                **(metadata or {}),
                'section_index': section_idx,
            }

            section_chunks = self.chunk_text(section, metadata=section_meta)
            all_chunks.extend(section_chunks)

        # Re-index chunks
        for idx, chunk in enumerate(all_chunks):
            chunk['chunk_index'] = idx

        return all_chunks


# Global instance
text_chunker = TextChunker(
    chunk_size=250,  # ~187 words (~1000 chars) - Optimal for semantic precision
    chunk_overlap=50,  # ~37 words overlap - Preserve context across chunks
    min_chunk_size=60,  # ~45 words minimum - Allow smaller focused chunks
)
