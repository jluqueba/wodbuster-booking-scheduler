"""One-shot schema explorer for ``LoadClass.ashx`` (US-005 form uplift).

Temporary tool used to discover the class-type and time-slot field
names so the rules form can build real dropdowns instead of free-text
inputs.

Delete once the form uplift (:mod:`rules.routes`'s ``/api/classes``
endpoint) is landed and verified.

Redaction:

- Any field whose key contains ``atletas`` (case-insensitive) is
  replaced by a length marker — Phase 0 identified ``AtletasEntrenando``
  as a PII field carrying other athletes' names.
- Everything else is passed through, truncated to 60 chars for strings
  and one representative sample for lists of dicts.
- Depth is capped at 2 so the response body stays small.
"""

from __future__ import annotations

from typing import Any

_MAX_DEPTH = 2
_MAX_STR_LEN = 60
_MAX_DICT_KEYS = 40
_REDACTION_MARKERS = ("atletas",)


def summarize_shape(value: Any, *, _depth: int = 0) -> Any:
    """Return a JSON-serialisable summary of ``value`` with PII redacted.

    Depth cap semantics: containers (dict, list) are collapsed to a
    type marker once ``_depth`` reaches ``_MAX_DEPTH``. Scalars always
    pass through so a dict of dicts of scalars renders as
    ``{"outer": {"inner": scalar}}`` without losing the leaf value.
    """
    if isinstance(value, dict):
        if _depth >= _MAX_DEPTH:
            return "<dict>"
        summary: dict[str, Any] = {}
        for key, nested in list(value.items())[:_MAX_DICT_KEYS]:
            if _should_redact(key):
                summary[key] = _redact(nested)
                continue
            summary[key] = summarize_shape(nested, _depth=_depth + 1)
        return summary

    if isinstance(value, list):
        if _depth >= _MAX_DEPTH:
            return f"<list len={len(value)}>"
        if not value:
            return []
        # Lists do NOT count toward the depth budget: the interesting
        # content lives inside the items, and a list of dicts is
        # conceptually the same nesting level as a single dict.
        first = summarize_shape(value[0], _depth=_depth)
        return (
            [first, f"<+ {len(value) - 1} more items>"] if len(value) > 1 else [first]
        )

    if isinstance(value, str):
        if len(value) > _MAX_STR_LEN:
            return f"<str len={len(value)}>"
        return value

    # Scalars: int, float, bool, None — safe to include verbatim
    # regardless of depth.
    return value


def _should_redact(key: str) -> bool:
    return any(marker in key.lower() for marker in _REDACTION_MARKERS)


def _redact(value: Any) -> str:
    if isinstance(value, list):
        return f"<list len={len(value)}, redacted>"
    if isinstance(value, dict):
        return f"<dict keys={len(value)}, redacted>"
    if isinstance(value, str):
        return f"<str len={len(value)}, redacted>"
    return f"<{type(value).__name__}, redacted>"


__all__ = ["summarize_shape"]
