"""Unit tests for services.issue_scrubber."""

from services import issue_scrubber


def test_scrub_text_redacts_email():
    out = issue_scrubber.scrub_text("contact me at jane.doe+spam@example.co.uk please")
    assert "jane.doe" not in out
    assert "example.co.uk" not in out
    assert "<email>" in out


def test_scrub_text_redacts_ipv4_and_ipv6():
    out = issue_scrubber.scrub_text("client 10.0.0.5 hit edge ::1 then 2001:db8::1")
    assert "10.0.0.5" not in out
    assert "2001:db8" not in out
    assert "<ip>" in out


def test_scrub_text_redacts_github_pats_and_openai_keys():
    sample = (
        "leaked github_pat_ABCDEFGHIJKLMNOPQRSTUV "
        "and ghp_1234567890abcdefghij "
        "plus sk-AAAAAAAAAAAAAAAAAAAAAAAA"
    )
    out = issue_scrubber.scrub_text(sample)
    assert "github_pat_" not in out
    assert "ghp_1234" not in out
    assert "sk-AAAA" not in out
    assert "<redacted-token>" in out


def test_scrub_text_redacts_authorization_header():
    out = issue_scrubber.scrub_text("Authorization: Bearer abc.def.ghi-very-secret")
    assert "abc.def.ghi" not in out
    assert "<redacted-auth>" in out


def test_scrub_text_handles_none_and_empty():
    assert issue_scrubber.scrub_text(None) == ""
    assert issue_scrubber.scrub_text("") == ""


def test_scrub_dict_redacts_sensitive_keys_recursively():
    payload = {
        "user_email": "alice@example.com",
        "password": "hunter2",
        "nested": {
            "api_key": "topsecretkey",
            "harmless": "kept-as-is",
        },
        "tokens": ["github_pat_ZZZZZZZZZZZZZZZZZZZZZZZZ", "regular text"],
    }
    out = issue_scrubber.scrub_dict(payload)

    assert out["password"] == "<redacted>"
    assert out["nested"]["api_key"] == "<redacted>"
    assert out["nested"]["harmless"] == "kept-as-is"
    # Email values that are NOT under a sensitive key still get text-scrubbed.
    assert "alice@example.com" not in out["user_email"]
    # Token list items get text-scrubbed too.
    assert "github_pat_ZZZZ" not in out["tokens"][0]


def test_scrub_dict_passes_through_non_string_scalars():
    payload = {"count": 42, "flag": True, "ratio": 3.14, "nothing": None}
    out = issue_scrubber.scrub_dict(payload)
    assert out == payload


def test_scrub_traceback_is_alias_for_scrub_text():
    tb = (
        'File "/app/foo.py", line 12, in handler\n'
        '    user = "bob@example.com"\n'
        "ValueError: bad email"
    )
    out = issue_scrubber.scrub_traceback(tb)
    assert "bob@example.com" not in out
    assert "<email>" in out
