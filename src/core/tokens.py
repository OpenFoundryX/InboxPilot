"""Counting tokens, for budgeting a prompt before it is sent.

Only for *estimates* — what a corpus will cost, whether to trim before calling.
For what a request actually cost, read `usage` off the API response instead: it
is exact, free, and needs no agreement with the provider's tokenizer.

The distinction matters because a character-count heuristic is wrong in the
direction that hurts. Mail is full of URLs, message-ids and header noise, which
tokenize far worse than prose — `len(text) // 4` reads as a safe rule of thumb
and then quietly under-reports a mailbox corpus by a fifth.
"""

from functools import lru_cache

from core.logging import get_logger

log = get_logger(__name__)

# Fallback ratio if tiktoken is unavailable. Deliberately below the usual ~4
# chars/token for English: the text being measured here is email, and
# over-estimating a budget is the harmless direction to be wrong in.
_CHARS_PER_TOKEN = 3.5


@lru_cache(maxsize=8)
def _encoding(model: str):
    """The tokenizer for `model`, or the current default for unknown names.

    Cached because building an encoding reads a vocabulary file, and this is
    called on every retrieval.
    """
    try:
        import tiktoken
    except ImportError:
        return None

    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        # A model newer than the installed tiktoken. Its encoding is almost
        # certainly the current default, and being approximately right beats
        # failing on an estimate.
        try:
            return tiktoken.get_encoding("o200k_base")
        except Exception:
            return None
    except Exception:
        return None


def count(text: str, model: str = "gpt-4o") -> int:
    """Approximate token count for `text`."""
    if not text:
        return 0
    encoding = _encoding(model)
    if encoding is None:
        return int(len(text) / _CHARS_PER_TOKEN)
    try:
        return len(encoding.encode(text))
    except Exception:
        return int(len(text) / _CHARS_PER_TOKEN)


def is_exact() -> bool:
    """Whether counts come from a real tokenizer rather than the fallback."""
    return _encoding("gpt-4o") is not None
