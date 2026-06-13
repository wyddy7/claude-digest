"""Surface 4 — 👤 Профиль (read-only plan + usage). Public; always viewable.

The ONE place quota usage is shown. All numbers come from the user's effective
limit rows (db.get_effective_limit) — never Python constants.
"""

from datetime import datetime, timezone
from html import escape

import digest_bot.db as db
import digest_bot.subscriptions as subscriptions


async def show_profile(update, context):
    user = context.user_data["user"]
    user_id = user["id"]
    tg_user_id = user["tg_user_id"]

    settings = await db.load_settings(user_id)
    channels = settings.get("channels") or []
    focus = settings.get("current_focus") or ""
    model = settings.get("model") or db.DEFAULT_MODEL

    channel_cap = await db.get_effective_limit(user_id, "channels_max", 15)
    history_days = await db.get_effective_limit(user_id, "history_days", 0)

    active = await subscriptions.is_subscription_active(tg_user_id)
    until = await subscriptions.active_until(tg_user_id)
    if active and until:
        days_left = max(0, (until - datetime.now(timezone.utc)).days)
        if subscriptions._parse_ts(user.get("pro_until")) and \
                subscriptions._parse_ts(user["pro_until"]) > datetime.now(timezone.utc):
            plan = f"Pro · активен до {until.strftime('%Y-%m-%d')}"
        else:
            plan = f"Pro-триал · {days_left} дн."
    else:
        plan = "🔒 Истёк"

    history_label = "без лимита" if str(history_days) in ("0", "0.0") else f"{history_days} дн."

    # Build the channel list: show names, not only the count.
    if channels:
        ch_lines = "\n".join(f"  • @{escape(ch)}" for ch in channels)
        channels_block = f"Каналов: {len(channels)}/{channel_cap}\n{ch_lines}"
    else:
        channels_block = f"Каналов: 0/{channel_cap} (добавь в ⚙️ Настройки)"

    body = (
        "👤 <b>Профиль</b>\n\n"
        f"План: {escape(plan)}\n"
        f"{channels_block}\n"
        f"Модель дайджеста: <code>{escape(str(model))}</code>\n"
        f"Фокус: {escape(focus) if focus else '«по значимости»'}\n"
        f"История: {history_label}"
    )
    await update.effective_message.reply_text(body, parse_mode="HTML")
