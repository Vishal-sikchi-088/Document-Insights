"""Mock summarization.

No real AI/LLM integration is in scope for this assignment — this derives
a structurally plausible summary from the document's own text (leading
sentences plus basic stats) so the rest of the pipeline has something
realistic to carry from `processing` through to `completed`. Kept as a
pure function, independent of the worker loop, so it's trivially unit
testable without touching Mongo/Redis.
"""
import re

from app.models.document import SummaryModel

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_MAX_SUMMARY_SENTENCES = 3
_MAX_KEY_POINTS = 3
_FALLBACK_CHAR_LIMIT = 280


def generate_mock_summary(content: str) -> SummaryModel:
    sentences = [s for s in (s.strip() for s in _SENTENCE_BOUNDARY.split(content.strip())) if s]

    # Content without recognizable sentence punctuation (a single long
    # run-on line, for instance) still needs a bounded summary rather than
    # echoing the entire document back.
    summary_sentences = sentences[:_MAX_SUMMARY_SENTENCES] or [content.strip()[:_FALLBACK_CHAR_LIMIT]]
    key_points = sentences[:_MAX_KEY_POINTS] or summary_sentences

    return SummaryModel(
        summary_text=" ".join(summary_sentences),
        word_count=len(content.split()),
        character_count=len(content),
        key_points=key_points,
    )
