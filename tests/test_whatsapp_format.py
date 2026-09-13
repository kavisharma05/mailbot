from services.messaging.phone import wa_phones_match
from services.messaging.format import (
    html_to_whatsapp,
    prepare_outbound_text,
    whatsapp_interactive_from_markup,
)


def test_wa_phones_match_country_code():
    assert wa_phones_match("917489491188", "7489491188")
    assert wa_phones_match("917489491188", "917489491188")


def test_html_to_whatsapp_bold_and_breaks():
    raw = "<b>Due</b><br/>Line two"
    out = html_to_whatsapp(raw)
    assert "*Due*" in out
    assert "Line two" in out


def test_prepare_outbound_text_chunks():
    long = "<b>" + ("x" * 5000) + "</b>"
    pieces = prepare_outbound_text(long)
    assert len(pieces) >= 2
    assert all(len(p) <= 4096 for p in pieces)


def test_keyboard_more_than_three_becomes_list():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("One", callback_data="a:1"),
                InlineKeyboardButton("Two", callback_data="a:2"),
            ],
            [
                InlineKeyboardButton("Three", callback_data="a:3"),
                InlineKeyboardButton("Four", callback_data="a:4"),
            ],
        ]
    )
    interactive = whatsapp_interactive_from_markup(markup, body_hint="Pick")
    assert interactive is not None
    assert interactive["type"] == "list"
    rows = interactive["action"]["sections"][0]["rows"]
    assert len(rows) == 4
