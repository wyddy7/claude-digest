"""Surface 2 — 📰 Дайджест for a multi-tenant (non-owner) user.

Generates + delivers a digest from THIS user's own channels / personalization,
reusing the locked digest pipeline. Delivery is gated on an active subscription
via @requires_tier; post-trial unpaid users hit the paywall (handled by the
decorator) and never trigger a generation (the documented free-user cost sink).

The digest OUTPUT FORMAT is LOCKED — this module only wires per-user config into
the existing pipeline + send path; it never reshapes the digest body.
"""

import logging
import os

import httpx

import db
from agent import run_digest_pipeline
from handlers.middleware import requires_tier
from handlers.strings import DIGEST_COLLECTING, DIGEST_DAILY_CAP, DIGEST_ERROR
from personalization import is_owner, resolve_personalization
from pipeline_config import build_pipeline_config, make_openrouter_client

logger = logging.getLogger(__name__)

OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")


async def _build_user_config(user: dict) -> tuple:
    """Build (PipelineConfig, settings) for one user from their saved settings +
    resolved personalization.

    Privacy boundary: resolve_personalization gives the owner (env CHAT_ID)
    their private yaml, and every other tenant the neutral committed default
    merged with their own DB overrides — NEVER the owner's yaml. The previous
    `load_personalization_db(...) or load_personalization()` fallback leaked
    the owner's profile into every tenant's prompt."""
    user_id = user["id"]
    settings = await db.load_settings(user_id)
    cfg_data = {
        "channels": settings.get("channels") or [],
        "current_focus": settings.get("current_focus") or "",
        "focus_auto_reset": bool(settings.get("focus_auto_reset")),
        "model": settings.get("model") or db.DEFAULT_MODEL,
        "last_digest": settings.get("last_digest") or "",
        "last_digest_time": settings.get("last_digest_time") or "",
        "interaction_history": settings.get("interaction_history") or [],
    }
    cfg_yaml = resolve_personalization(
        settings.get("personalization"), user.get("tg_user_id")
    )
    # Per-user recent digests for de-dup context (scoped to THIS user, not global).
    recent = await db.load_user_history(user_id, limit=3)
    return build_pipeline_config(cfg_data, cfg_yaml, recent_digests=recent), settings


async def deliver_digest(bot, user: dict, *, on_status=None) -> int:
    """Generate + deliver one tenant's digest. Returns posts_count. The chat id is
    the numeric tg_user_id. Shared by the 📰 button and the onboarding preview."""
    from datetime import datetime as dt

    import pytz

    moscow = pytz.timezone("Europe/Moscow")
    user_id = user["id"]
    tg_user_id = user["tg_user_id"]

    config, _settings = await _build_user_config(user)
    llm_client = make_openrouter_client(OPENROUTER_KEY)
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as fetcher:
        result = await run_digest_pipeline(
            config, llm_client=llm_client, fetcher=fetcher, on_status=on_status
        )

    digest_html = result["digest_html"]
    posts_count = result.get("posts_count", 0)
    date_str = dt.now(moscow).strftime("%d.%m.%Y")
    full_text = f"📰 <b>Дайджест {date_str}</b>\n\n{digest_html}"

    from delivery import send_digest_chunks

    await send_digest_chunks(
        bot, tg_user_id, full_text,
        result.get("personal_html", ""), result.get("stats_html", ""),
    )

    # Record per-user digest history row (tenant-scoped, filtered by user_id).
    try:
        await db.append_user_digest(user_id, digest_html, posts_count)
    except Exception as exc:
        # History write is best-effort — never abort delivery on a ledger failure.
        logger.warning("append_user_digest failed (non-fatal): %s", exc)

    saved = {
        "last_digest": digest_html,
        "last_digest_time": dt.now().isoformat(),
    }
    # Focus auto-reset after delivery (parity with the legacy owner path; this was
    # silently missing for tenants). Cleared only when the user opted in.
    if _settings.get("focus_auto_reset") and _settings.get("current_focus"):
        saved["current_focus"] = ""
    await db.save_settings(user_id, saved)
    return posts_count


@requires_tier("trial_or_paid")
async def send_digest(update, context):
    """📰 Дайджест. Gated: an expired user is intercepted by the decorator
    (paywall) and this body never runs (no LLM spend). Active users are still
    capped by digests_per_day (parity with the cron fan-out) so an active trial
    user can't hammer the full pipeline unbounded; the owner is exempt for
    testing."""
    user = context.user_data["user"]

    # Manual 📰 cap — same digests_per_day budget as the cron path (shared daily
    # counter via count_user_digests_today). Without it, manual requests were an
    # unbounded spend window for any active user. Owner exempt so testing isn't
    # blocked.
    if not is_owner(user["tg_user_id"]):
        cap = await db.get_effective_limit(user["id"], "digests_per_day", None)
        try:
            cap = int(cap) if cap is not None else None
        except (TypeError, ValueError):
            cap = None
        if cap is not None and cap >= 0:
            sent_today = await db.count_user_digests_today(user["id"])
            if sent_today >= cap:
                await update.effective_message.reply_text(DIGEST_DAILY_CAP)
                return

    status_msg = await update.effective_message.reply_text(DIGEST_COLLECTING)

    async def _on_status(text: str):
        try:
            await status_msg.edit_text(text)
        except Exception as e:
            logger.debug("status edit failed (non-fatal): %s", e)

    try:
        await deliver_digest(context.bot, user, on_status=_on_status)
    except Exception as e:
        logger.warning("send_digest failed for %s: %s", user.get("tg_user_id"), e)
        await update.effective_message.reply_text(DIGEST_ERROR)
