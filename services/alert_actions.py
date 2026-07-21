"""Inline keyboard actions for actionable mail alerts."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

ACT_DONE = "done"
ACT_DISMISS = "dismiss"
ACT_REMIND = "remind"


def action_callback(action: str, assignment_id: str) -> str:
    return f"act:{action}:{assignment_id}"


def build_action_keyboard(assignment_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("DONE", callback_data=action_callback(ACT_DONE, assignment_id)),
                InlineKeyboardButton(
                    "NOT IMPORTANT",
                    callback_data=action_callback(ACT_DISMISS, assignment_id),
                ),
                InlineKeyboardButton(
                    "REMIND AGAIN",
                    callback_data=action_callback(ACT_REMIND, assignment_id),
                ),
            ]
        ]
    )


def parse_action_callback(data: str) -> tuple[str, str] | None:
    parts = (data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "act":
        return None
    action, assignment_id = parts[1], parts[2]
    if action not in (ACT_DONE, ACT_DISMISS, ACT_REMIND) or not assignment_id:
        return None
    return action, assignment_id
