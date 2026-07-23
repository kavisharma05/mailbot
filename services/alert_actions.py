"""Inline keyboard actions for actionable mail alerts."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

ACT_DONE = "done"
ACT_DISMISS = "dismiss"
ACT_REMIND = "remind"
ACT_SNOOZE = "snooze"


def action_callback(action: str, assignment_id: str, *, extra: str = "") -> str:
    if extra:
        return f"act:{action}:{assignment_id}:{extra}"
    return f"act:{action}:{assignment_id}"


def build_action_keyboard(assignment_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Done", callback_data=action_callback(ACT_DONE, assignment_id)),
                InlineKeyboardButton(
                    "🚫 Not important",
                    callback_data=action_callback(ACT_DISMISS, assignment_id),
                ),
            ],
            [
                InlineKeyboardButton(
                    "⏰ Remind again",
                    callback_data=action_callback(ACT_REMIND, assignment_id),
                ),
                InlineKeyboardButton(
                    "💤 Snooze 1h",
                    callback_data=action_callback(ACT_SNOOZE, assignment_id, extra="1"),
                ),
                InlineKeyboardButton(
                    "💤 Snooze 3h",
                    callback_data=action_callback(ACT_SNOOZE, assignment_id, extra="3"),
                ),
            ],
        ]
    )


def parse_action_callback(data: str) -> tuple[str, str, str] | None:
    parts = (data or "").split(":")
    if len(parts) < 3 or parts[0] != "act":
        return None
    action, assignment_id = parts[1], parts[2]
    extra = parts[3] if len(parts) > 3 else ""
    if action not in (ACT_DONE, ACT_DISMISS, ACT_REMIND, ACT_SNOOZE) or not assignment_id:
        return None
    return action, assignment_id, extra
