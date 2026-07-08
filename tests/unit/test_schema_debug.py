"""Unit tests for :func:`summarize_shape`.

Sanity-check the redaction contract before the debug endpoint hits
production data. If the summarizer ever emits verbatim ``AtletasEntrenando``
values, an operator's browser could ship PII off the app.
"""

from __future__ import annotations

from wodbuster_worker.rules.schema_debug import summarize_shape


def test_scalars_pass_through_verbatim() -> None:
    assert summarize_shape(42) == 42
    assert summarize_shape(3.14) == 3.14
    assert summarize_shape(True) is True
    assert summarize_shape(None) is None


def test_short_strings_pass_through_verbatim() -> None:
    assert summarize_shape("WOD") == "WOD"
    assert summarize_shape("21:30") == "21:30"


def test_long_strings_are_length_marked() -> None:
    long = "x" * 200
    result = summarize_shape(long)
    assert isinstance(result, str)
    assert "len=200" in result


def test_atletas_key_is_redacted_even_at_depth_zero() -> None:
    payload = {
        "AtletasEntrenando": [{"nombre": "Real Name"}, {"nombre": "Another"}],
        "TieneFiltros": True,
    }
    result = summarize_shape(payload)
    assert "Real Name" not in str(result)
    assert "Another" not in str(result)
    assert isinstance(result, dict)
    assert result["TieneFiltros"] is True


def test_nested_atletas_is_also_redacted() -> None:
    payload = {
        "Data": [
            {
                "Id": 45654,
                "AtletasEntrenando": [{"nombre": "Buried PII"}],
                "TipoEstado": "Borrable",
            }
        ]
    }
    result = summarize_shape(payload)
    assert "Buried PII" not in str(result)


def test_lists_are_sampled_to_first_entry() -> None:
    payload = {
        "Data": [
            {"Id": 1, "Time": "07:30"},
            {"Id": 2, "Time": "08:30"},
            {"Id": 3, "Time": "09:30"},
        ]
    }
    result = summarize_shape(payload)
    data = result["Data"]
    assert isinstance(data, list)
    # First entry summarised + a "+ N more" marker.
    assert isinstance(data[0], dict)
    assert data[0]["Id"] == 1
    assert data[0]["Time"] == "07:30"
    assert "more items" in data[1]


def test_depth_cap_prevents_arbitrary_recursion() -> None:
    # Build a payload deeper than the two-level cap.
    payload = {"a": {"b": {"c": {"d": "leaf"}}}}
    result = summarize_shape(payload)
    # payload (0) -> a dict (1) -> b dict (2, capped to "<dict>").
    assert result == {"a": {"b": "<dict>"}}
