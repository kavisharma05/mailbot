"""Tests for conversation memory, intent routing, and contact resolution."""

from unittest.mock import patch

from ai.assistant import lookup_contact_email, resolve_person_email_live
from ai.intent import (
    INTENT_COMPOSE,
    INTENT_CONTACT_LOOKUP,
    INTENT_MAILBOX_QA,
    classify_message,
)
from services.contacts import (
    fuzzy_token_hit,
    is_generic_person,
    normalize_name_key,
    parse_sender,
    resolve_contact,
    resolve_email_robust,
    sender_matches_name,
)
from services.conversation import clear_convo, get_convo, remember_turn


def test_compose_intent_send_to_email():
    result = classify_message("send a mail to sharmakavish14@gmail.com")
    assert result["intent"] == INTENT_COMPOSE
    assert "sharmakavish14@gmail.com" in (result.get("recipient") or "")


def test_retrieval_intent_send_all_from_person():
    result = classify_message("send all mails from saleena mam")
    assert result["intent"] == INTENT_MAILBOX_QA


def test_contact_lookup_intent():
    result = classify_message("what is mail id of santhi mam")
    assert result["intent"] == INTENT_CONTACT_LOOKUP
    assert result.get("person")


def test_contact_lookup_her_email_uses_last_person():
    convo = {"last_person": "saleena"}
    result = classify_message("what's her email", convo)
    assert result["intent"] == INTENT_CONTACT_LOOKUP
    assert result.get("person") == "saleena"


def test_pls_mail_named_person():
    result = classify_message("pls mail saleena")
    assert result["intent"] == INTENT_COMPOSE
    assert "saleena" in (result.get("recipient") or "").lower()


def test_compose_with_content():
    result = classify_message(
        "send a mail to sharmakavish14@gmail.com saying ready to ship by end of the week"
    )
    assert result["intent"] == INTENT_COMPOSE
    assert result.get("content")


def test_compose_rejects_generic_faculty_recipient():
    result = classify_message("Can we upload a file here to send to a faculty")
    assert result["intent"] == INTENT_COMPOSE
    assert not result.get("recipient")


def test_compose_rejects_generic_friend_recipient():
    for text in (
        "write a mail to a friend",
        "write a mail to my friend saying happy birthday",
        "write a mail to faculty or a friendly",
    ):
        result = classify_message(text)
        assert result["intent"] == INTENT_COMPOSE
        assert not result.get("recipient"), text


def test_gmail_connected_question_is_not_compose():
    with patch("ai.intent._llm_classify") as mock_llm:
        result = classify_message("Is gmail connected")
    mock_llm.assert_not_called()
    assert result["intent"] == INTENT_MAILBOX_QA


def test_ambiguous_question_skips_llm_classify():
    with patch("ai.intent._llm_classify") as mock_llm:
        result = classify_message("any update on the CV submission?")
    mock_llm.assert_not_called()
    assert result["intent"] == INTENT_MAILBOX_QA


def test_need_to_email_someone_is_compose_without_recipient():
    with patch("ai.intent._llm_classify") as mock_llm:
        result = classify_message("I need to email someone about the assignment")
    mock_llm.assert_not_called()
    assert result["intent"] == INTENT_COMPOSE
    assert not result.get("recipient")


def test_parse_sender_display_email():
    name, email = parse_sender('Dr. Santhi Perumal <santhi.perumal@imtnag.ac.in>')
    assert name == "Dr. Santhi Perumal"
    assert email == "santhi.perumal@imtnag.ac.in"


def test_sender_matches_requested_name():
    assert sender_matches_name(
        "Dr. Santhi Perumal <santhi.perumal@imtnag.ac.in>",
        "santhi mam",
    )
    assert not sender_matches_name(
        "LRC IMTNAG <lrc@imtnag.ac.in>",
        "santhi mam",
    )


def test_sender_matches_with_typo():
    assert sender_matches_name(
        "Saleena Khan <saleena.khan@imtnag.ac.in>",
        "Salina",
    )


def test_normalize_name_key_strips_honorifics():
    assert normalize_name_key("santhi mam") == "santhi"
    assert normalize_name_key("Dr. Saleena Khan") == "saleena khan"
    assert normalize_name_key("Saleena ma'am") == "saleena"


def test_is_generic_person():
    assert is_generic_person("a faculty")
    assert is_generic_person("the professor")
    assert is_generic_person("faculty")
    assert is_generic_person("a friend")
    assert is_generic_person("my friend")
    assert is_generic_person("faculty or a friendly")
    assert is_generic_person("name of the friend")
    assert not is_generic_person("Saleena Khan")
    assert not is_generic_person("saleena.khan@imtnag.ac.in")


def test_fuzzy_token_hit():
    assert fuzzy_token_hit("saleena", "salina khan")
    assert fuzzy_token_hit("santhi", "santhi perumal")
    assert not fuzzy_token_hit("saleena", "john smith")


@patch("services.contacts.find_contacts_by_name")
def test_resolve_contact_honorific_and_typo(mock_find):
    mock_find.return_value = [
        {
            "email": "saleena.khan@imtnag.ac.in",
            "display_name": "Saleena Khan",
            "name_key": "saleena khan",
            "message_count": 5,
        }
    ]
    matches = resolve_contact("Saleena mam")
    assert len(matches) == 1
    assert matches[0]["email"] == "saleena.khan@imtnag.ac.in"

    matches = resolve_contact("Salina")
    assert len(matches) == 1


@patch("ai.assistant.resolve_person_email_live")
@patch("services.contacts.find_contacts_by_name", return_value=[])
def test_lookup_contact_email_live_fallback(mock_find, mock_live):
    mock_live.return_value = "saleena.khan@imtnag.ac.in"
    answer = lookup_contact_email("Saleena")
    assert "saleena.khan@imtnag.ac.in" in answer
    mock_live.assert_called_once_with("Saleena")


@patch("ai.assistant.llm_pick_contact")
@patch("ai.assistant.resolve_person_email_live")
@patch("services.contacts.find_contacts_by_name", return_value=[])
def test_resolve_email_robust_live_then_llm(mock_find, mock_live, mock_llm):
    mock_live.return_value = None
    mock_llm.return_value = "saleena.khan@imtnag.ac.in"
    mock_find.side_effect = [
        [],
        [
            {
                "email": "saleena.khan@imtnag.ac.in",
                "display_name": "Saleena Khan",
                "name_key": "saleena khan",
                "message_count": 3,
            }
        ],
    ]
    email, _matches, note = resolve_email_robust("Salina Khan")
    assert email == "saleena.khan@imtnag.ac.in"
    assert note is None


@patch("ai.assistant.list_message_summaries")
@patch("services.contacts.find_contacts_by_name", return_value=[])
def test_resolve_person_email_live_from_inbox(mock_find, mock_list):
    mock_list.return_value = [
        {
            "sender": "Saleena Khan <saleena.khan@imtnag.ac.in>",
            "to": "",
            "cc": "",
        }
    ]
    email = resolve_person_email_live("Saleena")
    assert email == "saleena.khan@imtnag.ac.in"


@patch("ai.assistant.list_message_summaries")
@patch("services.contacts.find_contacts_by_name", return_value=[])
def test_resolve_person_email_live_from_sent_to_header(mock_find, mock_list):
    mock_list.return_value = [
        {
            "sender": "Student <student@imtnag.ac.in>",
            "to": "Saleena Khan <saleena.khan@imtnag.ac.in>",
            "cc": "",
        }
    ]
    email = resolve_person_email_live("Saleena Khan")
    assert email == "saleena.khan@imtnag.ac.in"


def test_resolve_email_robust_generic_person():
    email, matches, note = resolve_email_robust("a faculty")
    assert email is None
    assert matches == []
    assert note == "Who should I send it to? Tell me their name or email address."


def test_conversation_memory_remember_turn():
    user_data: dict = {}
    remember_turn(
        user_data,
        user_text="mails from saleena mam",
        bot_text="Here are 3 mails",
        intent="mailbox_qa",
        person="saleena mam",
        query="mails from saleena mam",
    )
    convo = get_convo(user_data)
    assert convo["last_person"] == "saleena"
    assert convo["last_query"] == "mails from saleena mam"
    assert len(convo["history"]) == 2


def test_clear_convo():
    user_data = {"convo": {"last_person": "x"}, "compose": {"step": "need_content"}}
    clear_convo(user_data)
    assert "convo" not in user_data
    assert "compose" not in user_data


def test_compose_intent_send_person_a_mail():
    result = classify_message("send saleena a mail saying I will submit tomorrow")
    assert result["intent"] == INTENT_COMPOSE
    assert "saleena" in (result.get("recipient") or "").lower()
    assert result.get("content")


def test_compose_intent_mail_person_about():
    result = classify_message("mail vinay about the CP test")
    assert result["intent"] == INTENT_COMPOSE
    assert "vinay" in (result.get("recipient") or "").lower()
    assert "CP test" in (result.get("content") or "")


def test_compose_intent_bare_email_name():
    result = classify_message("email saleena mam")
    assert result["intent"] == INTENT_COMPOSE
    assert "saleena" in (result.get("recipient") or "").lower()


def test_compose_intent_pronoun_uses_last_person():
    convo = {"last_person": "saleena", "last_person_email": "skhan@imtnag.ac.in"}
    result = classify_message("send her a mail saying I'll be late", convo)
    assert result["intent"] == INTENT_COMPOSE
    assert result.get("recipient") == "saleena"


def test_compose_intent_send_it_to_her():
    convo = {"last_person": "vinay"}
    result = classify_message("send it to her", convo)
    assert result["intent"] == INTENT_COMPOSE
    assert result.get("recipient") == "vinay"


def test_compose_intent_same_person():
    convo = {"last_person": "saleena"}
    result = classify_message("send a mail to the same person", convo)
    assert result["intent"] == INTENT_COMPOSE
    assert result.get("recipient") == "saleena"


def test_extract_person_from_received_mail_phrase():
    from services.contacts import extract_person_reference

    assert extract_person_reference("We have received mail from saleena") == "saleena"
    assert extract_person_reference("her", {"last_person": "saleena"}) == "saleena"
    assert extract_person_reference("saleena mam") == "saleena mam"


def test_hinglish_compose_intent():
    result = classify_message("saleena ko mail bhejo")
    assert result["intent"] == INTENT_COMPOSE
    assert "saleena" in (result.get("recipient") or "").lower()

    result = classify_message("mail bhejo vinay ko")
    assert result["intent"] == INTENT_COMPOSE
    assert "vinay" in (result.get("recipient") or "").lower()

    convo = {"last_person": "saleena"}
    result = classify_message("unko mail bhejo", convo)
    assert result["intent"] == INTENT_COMPOSE
    assert result.get("recipient") == "saleena"

    result = classify_message("saleena ki mail dikhao")
    assert result["intent"] == INTENT_MAILBOX_QA
    assert "saleena" in (result.get("person") or "").lower()


def test_reply_to_her_is_compose():
    convo = {"last_person": "vinay"}
    result = classify_message("reply to her saying I will submit tomorrow", convo)
    assert result["intent"] == INTENT_COMPOSE
    assert result.get("recipient") == "vinay"


def test_extract_person_from_compose_sentences():
    from services.contacts import extract_person_reference, looks_like_recipient_switch

    convo = {"last_person": "saleena"}
    assert extract_person_reference("send her a mail", convo) == "saleena"
    assert extract_person_reference("send it to her", convo) == "saleena"
    assert extract_person_reference("send a mail to the same person", convo) == "saleena"
    assert extract_person_reference("send it to vinay instead") == "vinay"
    assert extract_person_reference("I want to send a mail to saleena") == "saleena"
    assert looks_like_recipient_switch("send it to vinay")
    assert looks_like_recipient_switch("send to vinay instead")
    assert not looks_like_recipient_switch("Please share notes from today's class")


def test_compose_content_step_does_not_intercept_as_recipient():
    """When asked 'What do you want to say?', content containing 'from' or
    'class' must be treated as content, not as a recipient clarification.
    Regression test for the send-flow bug where the bot never asked
    'Who should I send it to?'."""
    from bot.handlers import (
        COMPOSE_NEED_CONTENT,
        COMPOSE_NEED_RECIPIENT,
        _is_compose_recipient_reply,
    )

    convo = {"last_person": None, "last_person_email": None}
    compose = {"step": COMPOSE_NEED_CONTENT, "recipient": None, "instructions": None}

    # Content that previously triggered the recipient-reply misroute must
    # NOT be treated as a recipient reply when we're asking for content.
    assert not _is_compose_recipient_reply("Please share notes from today's class", convo, compose)
    assert not _is_compose_recipient_reply("Remind them the deadline for the report", convo, compose)

    # When step is need_recipient, recipient detection still works
    compose_recipient = {"step": COMPOSE_NEED_RECIPIENT, "recipient": None}
    assert _is_compose_recipient_reply("saleena@imtnag.ac.in", convo, compose_recipient)


def test_chat_message_confirm_keeps_draft_instead_of_new_intent():
    """Typed follow-ups after a draft must stay in compose (Telegram + WhatsApp)."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from bot.handlers import COMPOSE_CONFIRM, chat_message

    user_data: dict = {
        "compose": {
            "step": COMPOSE_CONFIRM,
            "recipient": "skhan@imtnag.ac.in",
            "recipient_name": "saleena",
            "instructions": "I'll be late",
            "draft": {"subject": "Late", "body": "I will be late."},
        },
        "convo": {"last_person": "saleena", "last_person_email": "skhan@imtnag.ac.in"},
    }
    update = MagicMock()
    update.effective_user = MagicMock(id=1)
    update.effective_chat = MagicMock(id=1, type="private")
    update.message.text = "send it to vinay instead"
    update.message.reply_text = AsyncMock()
    update.message.entities = None
    context = MagicMock()
    context.user_data = user_data
    context.bot = MagicMock(id=2, username="mailbot")

    async def _run():
        with (
            patch("bot.handlers._is_owner", return_value=True),
            patch(
                "bot.handlers._resolve_recipient_hint",
                AsyncMock(return_value=("vinay@imtnag.ac.in", None)),
            ),
            patch("bot.handlers._draft_and_show_compose", AsyncMock()) as draft,
        ):
            await chat_message(update, context)
        compose = user_data.get("compose")
        assert compose is not None
        assert compose["recipient"] == "vinay@imtnag.ac.in"
        assert compose["recipient_name"] == "vinay"
        draft.assert_awaited()

    asyncio.run(_run())


def test_chat_message_confirm_text_send_approves():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from bot.handlers import COMPOSE_CONFIRM, chat_message

    user_data: dict = {
        "compose": {
            "step": COMPOSE_CONFIRM,
            "recipient": "skhan@imtnag.ac.in",
            "recipient_name": "saleena",
            "draft": {"subject": "Hi", "body": "Hello"},
        }
    }
    update = MagicMock()
    update.effective_user = MagicMock(id=1)
    update.effective_chat = MagicMock(id=1, type="private")
    update.message.text = "send"
    update.message.reply_text = AsyncMock()
    update.message.entities = []
    context = MagicMock()
    context.user_data = user_data
    context.bot = MagicMock(id=2, username="mailbot")

    async def _run():
        with (
            patch("bot.handlers._is_owner", return_value=True),
            patch("bot.handlers.send_email") as send,
        ):
            await chat_message(update, context)
        send.assert_called_once()
        assert user_data.get("compose") is None
        assert "saleena" in update.message.reply_text.call_args.args[0].lower()

    asyncio.run(_run())


def test_send_command_uses_last_person_not_only_email():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from bot.handlers import send_command

    user_data: dict = {
        "convo": {"last_person": "saleena", "last_person_email": "skhan@imtnag.ac.in"}
    }
    update = MagicMock()
    update.message.text = "/send"
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = user_data
    context.args = []

    async def _run():
        with (
            patch("bot.handlers._is_owner", return_value=True),
            patch(
                "bot.handlers._resolve_recipient_hint",
                AsyncMock(return_value=("skhan@imtnag.ac.in", None)),
            ),
        ):
            await send_command(update, context)
        reply = update.message.reply_text.call_args.args[0]
        assert "saleena" in reply.lower()
        assert "skhan@imtnag.ac.in" in reply
        assert user_data["compose"]["recipient"] == "skhan@imtnag.ac.in"

    asyncio.run(_run())
