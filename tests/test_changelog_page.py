"""Public /changelog page and its parser."""

from utils.changelog import parse_changelog

SAMPLE = """[2.0.0] 2026-01-02
- Added `thing` <b>x</b>
  wrapped line
- Second change

[1.4.1] 2025-12-30
- Fixed a bug
"""


def test_parse_changelog_structure_and_escaping():
    releases = parse_changelog(SAMPLE)
    assert [r["version"] for r in releases] == ["2.0.0", "1.4.1"]
    assert [r["kind"] for r in releases] == ["major", "patch"]
    assert releases[0]["date_label"] == "2 Jan 2026"
    first = str(releases[0]["entries"][0])
    assert "<code>thing</code>" in first
    assert "&lt;b&gt;" in first and "<b>" not in first
    assert "wrapped line" in first
    assert len(releases[0]["entries"]) == 2


def test_changelog_is_public(client):
    resp = client.get("/changelog")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    with open("changelog.txt", encoding="utf-8") as f:
        newest = f.readline().strip().split("]")[0].lstrip("[")
    assert f'id="v{newest}"' in html


def test_public_pages_link_to_changelog(client):
    for path in ("/", "/login", "/register"):
        assert "/changelog" in client.get(path).get_data(as_text=True)
