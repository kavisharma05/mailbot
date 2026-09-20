"""IMT Access integration: catalog parsing, OTP, JWT, digest formatting, idempotency."""

import base64
import json
from datetime import date

import pytest

from imt_access.catalog import parse_catalog, sync_catalog
from imt_access.delivery import format_daily_digest, format_new_resources_alert
from imt_access.otp import extract_otp
from imt_access.portal import _jwt_expiry
from imt_access.publications import (
    EpwIssue,
    PublicationItem,
    _parse_issue_date,
    fetch_ft_headlines,
)

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


def test_format_daily_digest_includes_all_sections():
    issue = EpwIssue(
        label="Vol. 61, Issue No. 38, 19 Sep, 2026",
        issue_path="/journal/2026/38",
        publication_date=date(2026, 9, 19),
    )
    issue.sections["Editorials"] = [
        PublicationItem(kind="epw", title="Dire Straits for the Hegemon", url="http://x/a")
    ]
    ft = [PublicationItem(kind="ft", title="FT headline one", url="https://www.ft.com/content/1")]
    et = [PublicationItem(kind="et_prime", title="ET Prime story one", url="https://economictimes.indiatimes.com/prime/1")]

    text = format_daily_digest(epw_issue=issue, ft_items=ft, et_items=et, intro="Busy day.")
    assert "Library Daily" in text
    assert "Vol. 61, Issue No. 38" in text
    assert "Dire Straits for the Hegemon" in text
    assert "Financial Times" in text
    assert "https://www.ft.com/content/1" in text
    assert "ET Prime" in text
    assert "Busy day." in text


def test_format_daily_digest_escapes_html():
    ft = [PublicationItem(kind="ft", title="A <b>bold</b> move & more", url="https://www.ft.com/content/1")]
    text = format_daily_digest(epw_issue=None, ft_items=ft, et_items=[])
    assert "<b>bold</b>" not in text
    assert "&lt;b&gt;bold&lt;/b&gt;" in text


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
