"""Retrieval-score eval for the knowledge chunker: BEFORE vs AFTER on a small bakery FAQ.

Why: a ~600 character multi-topic FAQ used to become ONE chunk, and a short question such as
"What time do you open?" scored under RAGService.MIN_SCORE_THRESHOLD (0.35), so the AI said it
had no information. This prints, for each question, the best cosine score against the chunks the
OLD chunker made and the chunks the CURRENT chunker makes, and whether the best chunk is the
right one.

It calls the real embeddings API (a few cents). It needs OPENAI_API_KEY (and the usual
settings) in the environment; it needs NO database or Qdrant: scores are computed in memory
with the same cosine metric the collection uses.

    poetry run python scripts/kb_retrieval_eval.py                    # before = origin/develop
    poetry run python scripts/kb_retrieval_eval.py --before-ref HEAD~3
    poetry run python scripts/kb_retrieval_eval.py --json out.json

The "before" chunker is read from git (`git show <ref>:app/utils/chunking.py`), so the
production code carries no legacy copy. Existing tenants' stored chunks are NOT re-chunked by
anything: only NEW uploads use the current chunker (see docs/sop/activation_onboarding_backend.md
for how to re-index a knowledge source).
"""

import argparse
import asyncio
import importlib.util
import json
import math
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Two ways tenants really paste an FAQ: sections separated by blank lines, and Q:/A: lines.
DOC_SECTIONS = """Opening hours
We are open Monday to Saturday from 7am to 6pm, and on Sundays from 8am to 2pm.

Delivery
We deliver within 10km of the shop. Delivery costs 1,500 naira and takes about 45 minutes.

Prices
A loaf of bread is 1,200 naira. Birthday cakes start at 15,000 naira and need two days' notice.

Returns
If your order is wrong, tell us within 24 hours and we will replace it or refund you."""

DOC_QA = """Q: What are your opening hours?
A: We are open Monday to Saturday from 7am to 6pm, and on Sundays from 8am to 2pm.
Q: Do you deliver?
A: We deliver within 10km of the shop. Delivery costs 1,500 naira and takes about 45 minutes.
Q: How much does bread cost?
A: A loaf of bread is 1,200 naira. Birthday cakes start at 15,000 naira and need two days' notice.
Q: What is your returns policy?
A: If your order is wrong, tell us within 24 hours and we will replace it or refund you."""

# (question, a word the right chunk must contain)
QUESTIONS = [
    ("What time do you open?", "7am"),
    ("Are you open on Sunday?", "Sundays"),
    ("Do you deliver?", "10km"),
    ("How much is delivery?", "1,500"),
    ("How long does delivery take?", "45 minutes"),
    ("How much is a loaf of bread?", "1,200"),
    ("Do you make birthday cakes?", "Birthday cakes"),
    ("Can I return my order?", "24 hours"),
]


def _load_chunker(source: str, name: str):
    """Import a chunking module from source text and return its `text_chunker`."""
    module = types.ModuleType(name)
    exec(compile(source, name, "exec"), module.__dict__)  # noqa: S102 - our own repo's code
    return module.text_chunker


def _git_source(ref: str) -> str:
    return subprocess.run(
        ["git", "show", f"{ref}:app/utils/chunking.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _current_chunker():
    spec = importlib.util.spec_from_file_location("chunking_now", ROOT / "app/utils/chunking.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.text_chunker


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


async def _evaluate(chunker, doc: str, threshold: float, normalize, embed) -> dict:
    chunks = [c["content"] for c in chunker.chunk_text(doc)]
    vectors = [await embed(c) for c in chunks]
    rows = []
    for question, must_contain in QUESTIONS:
        q_vec = await embed(normalize(question))
        scores = [_cosine(q_vec, v) for v in vectors] or [0.0]
        best = max(range(len(scores)), key=scores.__getitem__) if chunks else None
        rows.append(
            {
                "question": question,
                "best_score": round(scores[best], 3) if best is not None else 0.0,
                "passes_threshold": bool(chunks) and scores[best] >= threshold,
                "right_chunk": bool(chunks) and must_contain in chunks[best],
            }
        )
    return {"chunks": len(chunks), "chunk_chars": [len(c) for c in chunks], "rows": rows}


def _print(label: str, result: dict, threshold: float) -> None:
    print(f"\n  {label}: {result['chunks']} chunk(s), sizes {result['chunk_chars']} chars")
    print(f"  {'question':36} {'best':>6}  >={threshold}  right chunk")
    for r in result["rows"]:
        print(
            f"  {r['question']:36} {r['best_score']:>6.3f}  "
            f"{'yes' if r['passes_threshold'] else 'NO ':>5}  "
            f"{'yes' if r['right_chunk'] else 'NO'}"
        )
    answered = sum(1 for r in result["rows"] if r["passes_threshold"] and r["right_chunk"])
    print(f"  -> {answered}/{len(result['rows'])} questions would be answered from the right chunk")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--before-ref", default="origin/develop", help="git ref of the old chunker")
    parser.add_argument("--json", help="write the full results to this file")
    args = parser.parse_args()

    from loguru import logger

    logger.remove()  # keep the report readable: no per-embedding debug lines

    from app.services.rag_service import RAGService
    from app.utils.embedding import embedding_service

    threshold = RAGService.MIN_SCORE_THRESHOLD
    normalize = RAGService(None).normalize_query  # type: ignore[arg-type]

    async def embed(text: str) -> list[float]:
        vec = await embedding_service.generate_embedding(text, use_rate_limit=False)
        if not vec:
            raise SystemExit("embedding call failed - check OPENAI_API_KEY")
        return vec

    before = _load_chunker(_git_source(args.before_ref), "chunking_before")
    after = _current_chunker()
    print(f"MIN_SCORE_THRESHOLD = {threshold}   (before = {args.before_ref}, after = working tree)")

    report = {}
    for doc_name, doc in (("sections", DOC_SECTIONS), ("q_and_a", DOC_QA)):
        print(f"\n=== FAQ pasted as: {doc_name} ({len(doc)} chars) ===")
        report[doc_name] = {}
        for label, chunker in (("BEFORE", before), ("AFTER ", after)):
            result = await _evaluate(chunker, doc, threshold, normalize, embed)
            report[doc_name][label.strip().lower()] = result
            _print(label, result, threshold)

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
