"""IMT Access integration: catalog parsing, OTP, JWT, digest formatting, idempotency."""

import base64
import json
from datetime import date

import pytest

from imt_access.catalog import parse_catalog, sync_catalog
from imt_access.delivery import build_digest_messages, format_new_resources_alert
from imt_access.otp import extract_otp
from imt_access.portal import _jwt_expiry
from imt_access.publications import (
    EpwIssue,
    PublicationItem,
    _parse_issue_date,
    fetch_ft_headlines,
)
from imt_access import stories as imt_stories

CATALOG_HTML = """
<html><body>
<ul class="sidebar">
  <li><a class="sub-item" onclick="displayItem('0',this); return false"> E-Books (Subscribed)</a></li>
  <li><a class="sub-item" onclick="displayItem('1',this); return false"> E-Resources (Subscribed)</a></li>
  <li><a class="sub-item" onclick="displayItem('4',this); return false"> Online Magazines</a></li>
</ul>
<div class="row category category1">
  <div class="col-lg-2"><div class="card w-100 my-2 shadow-2-strong displayFavouriteIcon">
    <div class="card-body d-flex flex-column">
      <div class="d-flex flex-row align-items-end"><h6 style="font-size: 14px;" class="text-dark">JSTOR</h6></div>
      <div class="d-flex align-items-end pt-3 mt-auto">
        <a target="_blank" class="viewBtn" href="https://www-jstor-org.imtnagpur.mapmyaccess.com/"
           onclick="trackResourceClick('660a91b04bbf603dcdf7a83c','x')">View</a>
        <div class="viewIcon favouriteIconDiv"><i class="fa fa-star favourite favouriteIcon" id="660a91b04bbf603dcdf7a83c"></i></div>
      </div>
    </div>
  </div></div>
  <div class="col-lg-2"><div class="card w-100 my-2 shadow-2-strong displayFavouriteIcon">
    <div class="card-body d-flex flex-column">
      <div class="d-flex flex-row align-items-end"><h6 style="font-size: 14px;" class="text-dark">Scopus</h6></div>
      <div class="d-flex align-items-end pt-3 mt-auto">
        <a target="_blank" class="viewBtn" href="https://www-scopus-com.imtnagpur.mapmyaccess.com/"
           onclick="trackResourceClick('668522e56295540cc875eabb','x')">View</a>
        <div class="viewIcon favouriteIconDiv"><i class="fa fa-star favourite favouriteIcon" id="668522e56295540cc875eabb"></i></div>
      </div>
    </div>
  </div></div>
</div>
<div class="row category category4">
  <div class="col-lg-2"><div class="card w-100 my-2 shadow-2-strong displayFavouriteIcon">
    <div class="card-body d-flex flex-column">
      <div class="d-flex flex-row align-items-end"><h6 style="font-size: 14px;" class="text-dark">Economic and Political weekly (EPW)</h6></div>
      <div class="d-flex align-items-end pt-3 mt-auto">
        <a target="_blank" class="viewBtn" href="https://www-epw-in.imtnagpur.mapmyaccess.com/"
           onclick="trackResourceClick('66f3979d4431db396ed7425f','x')">View</a>
        <div class="viewIcon favouriteIconDiv"><i class="fa fa-star favourite favouriteIcon" id="66f3979d4431db396ed7425f"></i></div>
      </div>
    </div>
  </div></div>
</div>
</body></html>
"""

ISSUE_HTML = """
<html><head><title>Vol. 61, Issue No. 38, 19 Sep, 2026 | Economic and Political Weekly</title></head>
<body>
<h2>Editorials</h2>
<h3><a href="/journal/2026/38/editorials/dire-straits-hegemon.html">Dire Straits for the Hegemon</a></h3>
<h3><a href="/journal/2026/38/editorials/prepare-rising-inflation.html">Prepare for Rising Inflation</a></h3>
<h2>Special Articles</h2>
<h4><a href="/journal/2026/38/special-articles/credit-deposit-ratio-and-central-banks-balance.html">Credit-Deposit Ratio and Central Bank's Balance Sheet</a></h4>
</body></html>
"""


# --- catalog parsing ---


def test_parse_catalog_extracts_cards_with_categories():
    resources = parse_catalog(CATALOG_HTML)
    assert len(resources) == 3
    by_id = {r.portal_id: r for r in resources}
    jstor = by_id["660a91b04bbf603dcdf7a83c"]
    assert jstor.name == "JSTOR"
    assert jstor.category == "E-Resources (Subscribed)"
    assert jstor.url == "https://www-jstor-org.imtnagpur.mapmyaccess.com/"
    assert jstor.proxied is True
    epw = by_id["66f3979d4431db396ed7425f"]
    assert epw.category == "Online Magazines"


def test_parse_catalog_dedupes_and_skips_incomplete():
    html = CATALOG_HTML.replace('class="text-dark">JSTOR', 'class="text-dark">JSTOR', 1)
    # duplicate the JSTOR card
    html = html.replace(
        '<div class="col-lg-2"><div class="card w-100 my-2 shadow-2-strong displayFavouriteIcon">',
        '<div class="col-lg-2"><div class="card w-100 my-2 shadow-2-strong displayFavouriteIcon">',
        1,
    )
    resources = parse_catalog(html + CATALOG_HTML)
    ids = [r.portal_id for r in resources]
    assert len(ids) == len(set(ids))


def test_sync_catalog_diffs_new_and_removed(monkeypatch):
    from imt_access import store

    monkeypatch.setattr(
        store,
        "get_known_resources",
        lambda: {
            "660a91b04bbf603dcdf7a83c": {
                "portal_id": "660a91b04bbf603dcdf7a83c",
                "name": "JSTOR",
                "is_active": True,
                "first_seen_at": "2026-01-01T00:00:00+00:00",
            },
            "oldgone": {"portal_id": "oldgone", "name": "Old DB", "is_active": True},
        },
    )
    upserted: list[dict] = []
    inactivated: list[str] = []
    monkeypatch.setattr(store, "upsert_resources", lambda rows: upserted.extend(rows) or True)
    monkeypatch.setattr(store, "mark_resources_inactive", lambda ids: inactivated.extend(ids))
    monkeypatch.setattr(store, "update_imt_state", lambda **kw: None)

    diff = sync_catalog(CATALOG_HTML)
    assert diff.total == 3
    # JSTOR known; Scopus + EPW are new
    new_ids = {r.portal_id for r in diff.new}
    assert new_ids == {"668522e56295540cc875eabb", "66f3979d4431db396ed7425f"}
    assert inactivated == ["oldgone"]
    # JSTOR keeps its original first_seen_at
    jstor_row = next(r for r in upserted if r["portal_id"] == "660a91b04bbf603dcdf7a83c")
    assert jstor_row["first_seen_at"] == "2026-01-01T00:00:00+00:00"


# --- OTP extraction ---


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Your OTP is 482910. It expires in 10 minutes.", "482910"),
        ("MapMyAccess verification code: 7712", "7712"),
        ("Use 993344 as the one-time password", "993344"),
        ("No code here, call 2026 for info", None),
        ("", None),
    ],
)
def test_extract_otp(text, expected):
    assert extract_otp(text) == expected


# --- JWT expiry ---


def _make_jwt(exp: float) -> str:
    def b64(obj) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{b64({'alg': 'HS256'})}.{b64({'exp': exp})}.sig"


def test_jwt_expiry_reads_exp():
    token = _make_jwt(1790007956)
    assert _jwt_expiry(token) == 1790007956


def test_jwt_expiry_handles_garbage():
    assert _jwt_expiry("not-a-jwt") is None
    assert _jwt_expiry("") is None


# --- EPW issue parsing ---


def test_parse_issue_date_formats():
    assert _parse_issue_date("Vol. 61, Issue No. 38, 19 Sep, 2026") == date(2026, 9, 19)
    assert _parse_issue_date("Vol. 60, Issue No. 1, 4 January, 2025") == date(2025, 1, 4)
    assert _parse_issue_date("no date here") is None


def test_epw_issue_items_flatten_sections():
    issue = EpwIssue(label="Vol. 61, Issue No. 38", issue_path="/journal/2026/38", publication_date=date(2026, 9, 19))
    day = date(2026, 9, 19)
    issue.sections["Editorials"] = [
        PublicationItem(kind="epw", title="A", url="http://x/a", publication_date=day),
        PublicationItem(kind="epw", title="B", url="http://x/b", publication_date=day),
    ]
    issue.sections["Insight"] = [PublicationItem(kind="epw", title="C", url="http://x/c", publication_date=day)]
    assert len(issue.items) == 3
    rows = [i.to_row() for i in issue.items]
    assert rows[0]["kind"] == "epw"
    assert rows[0]["publication_date"] == "2026-09-19"


def test_fetch_ft_headlines_parses_content_links(monkeypatch):
    from imt_access import publications

    html = """
    <html><body>
      <a href="/content/aaa">Big Tech uses guarantees to keep $300bn AI exposure off balance sheets</a>
      <a href="/content/aaa">Big Tech uses guarantees to keep $300bn AI exposure off balance sheets</a>
      <a href="/content/bbb">Saudi Arabia quits China-led cross-border currency platform</a>
      <a href="/content/ccc">Short</a>
      <a href="/other/ddd">Not a content link at all, ignore me please</a>
    </body></html>
    """
    monkeypatch.setattr(publications, "_curl_get", lambda url: html)
    items = fetch_ft_headlines(limit=8)
    assert len(items) == 2
    assert items[0].url == "https://www.ft.com/content/aaa"
    assert items[0].kind == "ft"


def test_fetch_ft_headlines_handles_failure(monkeypatch):
    from imt_access import publications

    monkeypatch.setattr(publications, "_curl_get", lambda url: None)
    assert fetch_ft_headlines() == []


# --- digest formatting ---


def _sample_digest_inputs():
    issue = EpwIssue(
        label="Vol. 61, Issue No. 38, 19 Sep, 2026",
        issue_path="/journal/2026/38",
        publication_date=date(2026, 9, 19),
    )
    issue.sections["Editorials"] = [
        PublicationItem(kind="epw", title="Dire Straits for the Hegemon", url="http://x/a", section="Editorials")
    ]
    issue.sections["Special Articles"] = [
        PublicationItem(kind="epw", title="Credit-Deposit Ratio", url="http://x/b", section="Special Articles")
    ]
    ft = [PublicationItem(kind="ft", title="FT headline one", url="https://www.ft.com/content/1")]
    et = [PublicationItem(kind="et_prime", title="ET Prime story one", url="https://economictimes.indiatimes.com/prime/1")]
    return issue, ft, et


def test_build_digest_messages_split_and_numbered():
    issue, ft, et = _sample_digest_inputs()
    msgs = build_digest_messages(
        epw_issue=issue, ft_items=ft, et_items=et, intro="Busy day.", run_date=date(2026, 9, 21)
    )

    assert msgs["intro"] == ""
    stories = msgs["stories"]
    assert "Library —" in stories
    assert "Busy day." not in stories
    assert "Vol. 61, Issue No. 38" in stories
    # Numbering is global across sources, in order: EPW sections -> FT -> ET.
    assert "1. Dire Straits for the Hegemon" in stories
    assert "2. Credit-Deposit Ratio" in stories
    assert '3. <a href="https://www.ft.com/content/1">FT headline one</a>' in stories
    assert "4. <a" in stories and "ET Prime story one</a>" in stories
    assert "https://www.ft.com/content/1" in stories

    # Ordered items match the numbering (manifest order).
    assert [i.title for i in msgs["items"]] == [
        "Dire Straits for the Hegemon",
        "Credit-Deposit Ratio",
        "FT headline one",
        "ET Prime story one",
    ]
    # The story list asks the reader to pick a story.
    assert "reply with a number" in msgs["stories"].lower()
    assert len(msgs["items"]) == 4


def test_build_digest_messages_keeps_only_a_few_stories():
    issue = EpwIssue(label="Vol. 61", issue_path="/journal/2026/38", publication_date=date(2026, 9, 19))
    issue.sections["Editorials"] = [
        PublicationItem(kind="epw", title="Editorial one", url="http://x/1", section="Editorials"),
        PublicationItem(kind="epw", title="Editorial two", url="http://x/2", section="Editorials"),
        PublicationItem(kind="epw", title="Editorial three", url="http://x/3", section="Editorials"),
    ]
    issue.sections["Book Reviews"] = [
        PublicationItem(kind="epw", title="A long book review", url="http://x/b", section="Book Reviews"),
    ]
    ft = [PublicationItem(kind="ft", title=f"FT {i}", url=f"https://www.ft.com/content/{i}") for i in range(5)]
    et = [PublicationItem(kind="et_prime", title=f"ET {i}", url=f"https://et/{i}") for i in range(5)]
    msgs = build_digest_messages(epw_issue=issue, ft_items=ft, et_items=et)
    titles = [item.title for item in msgs["items"]]
    assert titles == ["Editorial one", "Editorial two", "FT 0", "FT 1", "ET 0", "ET 1"]
    assert "book review" not in msgs["stories"].lower()
    assert "Editorial three" not in msgs["stories"]


def test_stories_keyboard_number_grid():
    from imt_access.delivery import _stories_keyboard

    items = [
        PublicationItem(kind="epw", title=f"Story {i}", url=f"http://x/{i}")
        for i in range(1, 14)
    ]
    markup = _stories_keyboard(items)
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert [b.text for b in buttons] == [str(i) for i in range(1, 14)]
    assert buttons[0].callback_data == "story:1"
    assert buttons[-1].callback_data == "story:13"
    # 6 per row: 13 buttons -> 3 rows (6 + 6 + 1)
    assert [len(row) for row in markup.inline_keyboard] == [6, 6, 1]
    assert _stories_keyboard([]) is None


def test_whatsapp_story_picker_chunk_drilldown():
    from imt_access.delivery import _whatsapp_story_picker, whatsapp_story_chunk_picker

    items = (
        [PublicationItem(kind="epw", title=f"EPW story {i}", url=f"http://x/e{i}") for i in range(1, 13)]
        + [PublicationItem(kind="ft", title=f"FT story {i}", url=f"http://x/f{i}") for i in range(1, 4)]
        + [PublicationItem(kind="et_prime", title="ET story", url="http://x/p1")]
    )
    # Meta caps lists at 10 rows: top level offers one row per 10-story chunk.
    picker = _whatsapp_story_picker(items)
    assert picker["type"] == "list"
    rows = picker["action"]["sections"][0]["rows"]
    assert [r["id"] for r in rows] == ["storylist:0", "storylist:10"]
    assert rows[0]["title"] == "EPW 1–10"
    assert rows[1]["title"] == "Stories 11–16"  # mixed EPW/FT/ET chunk
    for row in rows:
        assert len(row["title"]) <= 24
    assert _whatsapp_story_picker([]) is None

    # Second level: the chunk's stories, numbered globally, ready for story:<n>.
    chunk = whatsapp_story_chunk_picker(items, 10)
    crows = chunk["action"]["sections"][0]["rows"]
    assert len(crows) == 6
    assert crows[0]["id"] == "story:11"
    assert crows[-1]["id"] == "story:16"
    assert crows[0]["title"].startswith("11. ")
    for row in crows:
        assert len(row["title"]) <= 24
    assert whatsapp_story_chunk_picker(items, 99) is None
    assert whatsapp_story_chunk_picker(items, -1) is None
    # Manifest dicts work too (WhatsApp router passes manifest items).
    as_dicts = [it.to_row() for it in items]
    assert whatsapp_story_chunk_picker(as_dicts, 0)["action"]["sections"][0]["rows"][0]["id"] == "story:1"


def test_build_digest_messages_escapes_html():
    ft = [PublicationItem(kind="ft", title="A <b>bold</b> move & more", url="https://www.ft.com/content/1")]
    msgs = build_digest_messages(epw_issue=None, ft_items=ft, et_items=[])
    assert "<b>bold</b>" not in msgs["stories"]
    assert "&lt;b&gt;bold&lt;/b&gt;" in msgs["stories"]


def test_build_digest_messages_new_resources_separate():
    from imt_access.catalog import Resource

    _issue, ft, _et = _sample_digest_inputs()
    res = [Resource(portal_id="p1", name="New DB", category="Online Journals", url="http://x")]
    msgs = build_digest_messages(
        epw_issue=None, ft_items=ft, et_items=[], new_resources=res
    )
    assert msgs["new_resources"] is not None
    assert "New DB" in msgs["new_resources"]
    assert "New DB" not in msgs["stories"]


# --- story manifest + briefs ---


def test_manifest_roundtrip(monkeypatch):
    saved: dict[str, str] = {}
    monkeypatch.setattr(
        imt_stories.store,
        "save_report",
        lambda kind, content: saved.update({"kind": kind, "content": content}),
    )
    monkeypatch.setattr(
        imt_stories.store,
        "get_latest_report",
        lambda kind: {"content": saved["content"]} if saved.get("kind") == kind else None,
    )

    items = [
        PublicationItem(kind="epw", title="A", url="http://x/a", section="Editorials"),
        PublicationItem(kind="ft", title="B", url="http://x/b"),
    ]
    today = imt_stories.datetime.now(imt_stories.IST).date()
    imt_stories.save_manifest(items, today)

    manifest = imt_stories.load_manifest()
    assert manifest is not None
    assert manifest["date"] == today.isoformat()
    assert imt_stories.manifest_size(manifest) == 2
    assert imt_stories.manifest_item(manifest, 2)["title"] == "B"
    assert imt_stories.manifest_item(manifest, 3) is None


def test_load_manifest_rejects_stale(monkeypatch):
    old = json.dumps({"date": "2020-01-01", "items": [{"n": 1, "kind": "ft", "title": "x", "url": "u"}]})
    monkeypatch.setattr(imt_stories.store, "get_latest_report", lambda kind: {"content": old})
    assert imt_stories.load_manifest() is None


def test_get_story_brief_uses_cache(monkeypatch):
    manifest = {
        "date": imt_stories.datetime.now(imt_stories.IST).date().isoformat(),
        "items": [{"n": 1, "kind": "ft", "title": "T", "url": "http://x/1", "section": ""}],
    }
    monkeypatch.setattr(imt_stories, "load_manifest", lambda: manifest)
    monkeypatch.setattr(imt_stories.store, "get_publication_summary", lambda *a: "cached brief")
    result = imt_stories.get_story_brief(1)
    assert result["brief"] == "cached brief"
    assert result["cached"] is True


def test_get_story_brief_generates_and_caches(monkeypatch):
    manifest = {
        "date": imt_stories.datetime.now(imt_stories.IST).date().isoformat(),
        "items": [{"n": 1, "kind": "ft", "title": "T", "url": "http://x/1", "section": ""}],
    }
    monkeypatch.setattr(imt_stories, "load_manifest", lambda: manifest)
    monkeypatch.setattr(imt_stories.store, "get_publication_summary", lambda *a: None)
    monkeypatch.setattr(imt_stories, "fetch_story_text", lambda item: ("some text " * 100, True))
    monkeypatch.setattr(imt_stories, "build_story_brief", lambda item, text, full: "fresh brief")
    updated: list[tuple] = []
    monkeypatch.setattr(
        imt_stories.store, "update_publication_summary", lambda *a: updated.append(a)
    )
    result = imt_stories.get_story_brief(1)
    assert result["brief"] == "fresh brief"
    assert result["cached"] is False
    assert updated and updated[0][3] == "fresh brief"


def test_format_story_brief_message():
    result = {
        "item": {"kind": "epw", "title": "Credit & You", "url": "http://x/a", "section": "Special Articles"},
        "brief": "• point one\nWhy it matters for you: banking",
        "date": "2026-09-21",
    }
    text = imt_stories.format_story_brief_message(result)
    assert "Credit &amp; You" in text
    assert "EPW" in text
    assert "Special Articles" in text
    assert "point one" in text
    assert "http://x/a" in text


def test_story_number_regex():
    from bot.handlers import _STORY_NUM_RE

    for text, n in [("7", 7), ("12", 12), ("story 7", 7), ("Story #12", 12), ("brief 3", 3)]:
        m = _STORY_NUM_RE.match(text)
        assert m and int(m.group(1)) == n
    for text in ("story", "123", "7 pm", "seven", "story time", ""):
        assert _STORY_NUM_RE.match(text) is None


def test_format_new_resources_alert():
    from imt_access.catalog import Resource

    res = [Resource(portal_id="p1", name="New DB", category="Online Journals", url="http://x")]
    text = format_new_resources_alert(res)
    assert "New DB" in text
    assert "Online Journals" in text


# --- idempotency ---


def test_daily_already_run(monkeypatch):
    from imt_access import store

    monkeypatch.setattr(store, "get_imt_state", lambda: {"last_daily_run_on": "2026-09-20"})
    assert store.daily_already_run(date(2026, 9, 20)) is True
    assert store.daily_already_run(date(2026, 9, 21)) is False


def test_daily_already_run_handles_missing(monkeypatch):
    from imt_access import store

    monkeypatch.setattr(store, "get_imt_state", lambda: {})
    assert store.daily_already_run(date(2026, 9, 20)) is False
    monkeypatch.setattr(store, "get_imt_state", lambda: {"last_daily_run_on": "garbage"})
    assert store.daily_already_run(date(2026, 9, 20)) is False
