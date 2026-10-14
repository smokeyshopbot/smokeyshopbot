"""Check current membership before interactive bot access; never cache a join forever."""

import logging
import time
import re

import database as db
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import ApplicationHandlerStop
from utils.channel_gate_settings import channel_gate_enabled, chat_member_is_joined, validate_channel_settings
from utils.i18n import LANGUAGE_NAMES, normalize_lang, tr

logger = logging.getLogger(__name__)
CALLBACK_PREFIX = "channel_gate:"


async def _language(update, saved_user, enabled):
    value = saved_user.get("language") or saved_user.get("language_code") or update.effective_user.language_code
    lang = normalize_lang(str(value or "en").replace("_", "-").split("-", 1)[0])
    return lang if lang in enabled else "en"


def _keyboard(settings, lang, enabled, unavailable=False):
    rows = []
    if not unavailable:
        rows.append([InlineKeyboardButton(tr(lang, "btn_join_channel"), url=settings["required_channel_join_url"])])
    rows.append([InlineKeyboardButton(tr(lang, "btn_retry_channel_check" if unavailable else "btn_check_channel_join"), callback_data=CALLBACK_PREFIX + "check")])
    if len(enabled) > 1:
        rows.append([InlineKeyboardButton(LANGUAGE_NAMES[code], callback_data=CALLBACK_PREFIX + "language:" + code) for code in enabled])
    return InlineKeyboardMarkup(rows)


async def _show_prompt(update, context, text, markup=None):
    query = update.callback_query
    if query and query.message:
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode=None)
            return
        except BadRequest as error:
            if "message is not modified" in str(error).lower():
                return
            # Old/deleted messages may no longer be editable. Send a fresh prompt.
    if update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=None)
    else:
        await context.bot.send_message(update.effective_user.id, text, reply_markup=markup, parse_mode=None)


def _ttl(settings):
    try:
        return min(60, max(0, int(settings.get('required_channel_cache_seconds', 30))))
    except (TypeError, ValueError):
        return 30


async def handle_membership_update(update, context):
    event = update.chat_member
    settings = await db.get_secret_settings()
    channel = str(settings.get('required_channel_id') or '').strip()
    matches = (channel == str(event.chat.id) or
               channel.casefold() == '@' + str(event.chat.username or '').casefold())
    if not matches:
        return
    cache = context.bot_data.setdefault('channel_membership_cache', {})
    uid = event.new_chat_member.user.id
    for key in list(cache):
        if key[1] == uid:
            cache.pop(key, None)
    # Telegram's leave/join events invalidate the cached status immediately.


async def _check_membership(context, settings, user_id, force=False):
    cache = context.bot_data.setdefault('channel_membership_cache', {})
    key = (settings['required_channel_id'], user_id)
    cached = cache.get(key)
    if not force and _ttl(settings) and cached and cached[0] > time.monotonic():
        return True
    cache.pop(key, None)
    if context.bot_data.get("channel_membership_retry_until", 0) > time.monotonic():
        return None
    channel_id = settings["required_channel_id"]
    if channel_id.startswith("-100"):
        channel_id = int(channel_id)
    try:
        member = await context.bot.get_chat_member(
            chat_id=channel_id, user_id=user_id,
            read_timeout=3, connect_timeout=3, write_timeout=3, pool_timeout=3,
        )
        joined = chat_member_is_joined(member)
        if joined and _ttl(settings):
            if len(cache) >= 10000:
                cache.pop(next(iter(cache)), None)
            cache[key] = (time.monotonic() + _ttl(settings), True)
        return joined
    except RetryAfter as error:
        delay = error.retry_after
        seconds = delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)
        context.bot_data["channel_membership_retry_until"] = time.monotonic() + seconds + 1
        logger.warning("Channel membership checks temporarily rate limited by Telegram.")
    except TelegramError as error:
        logger.warning("Channel membership check failed (%s). Check channel ID and bot admin access.", type(error).__name__)
    return None


async def require_channel_membership(update, context):
    user = update.effective_user
    if not user or user.is_bot:
        return
    raw_settings = await db.get_secret_settings()
    message = getattr(update, 'message', None)
    match = re.match(r'^/start(?:@\w+)?\s+product_([a-fA-F0-9]{24})\s*$', str(getattr(message, 'text', '') or ''))
    if match:
        context.user_data['pending_product_link'] = match.group(1).lower()
    poll_link = re.match(r'^/start(?:@\w+)?\s+poll_([A-F0-9]{12})\s*$', str(getattr(message, 'text', '') or ''))
    if poll_link:
        context.user_data['pending_poll_link'] = poll_link.group(1)
    if not channel_gate_enabled(raw_settings.get("required_channel_enabled")):
        return
    saved_user = await db.upsert_user(user.id, user.username or "")
    enabled = await db.get_enabled_languages()
    lang = await _language(update, saved_user, enabled)
    query = update.callback_query
    data = str(query.data or "") if query else ""
    language_choice = None
    if data.startswith(CALLBACK_PREFIX + "language:"):
        language_choice = data[len(CALLBACK_PREFIX + "language:"):]
    elif data.startswith("lang:"):
        language_choice = data[len("lang:"):]
    if language_choice in enabled:
        lang = await db.set_user_language(user.id, language_choice)
    if saved_user.get("blocked"):
        if query:
            await query.answer(tr(lang, "blocked"), show_alert=True)
        await _show_prompt(update, context, tr(lang, "blocked"))
        raise ApplicationHandlerStop
    try:
        settings = validate_channel_settings(True, raw_settings.get("required_channel_id"), raw_settings.get("required_channel_join_url"))
        settings['required_channel_cache_seconds'] = raw_settings.get('required_channel_cache_seconds', 30)
        joined = await _check_membership(context, settings, user.id, force=data == CALLBACK_PREFIX + 'check')
    except ValueError:
        logger.error("Required channel settings are invalid. Correct them in WebAdmin Secret Settings.")
        settings = {}
        joined = None
    if joined is True:
        # No prompt, no persistent exemption. The next action checks membership again.
        return
    if query:
        product_link = re.fullmatch(r'(?:product_id|favorite_buy_id|favorite_product_id):([a-fA-F0-9]{24})', data)
        if product_link:
            context.user_data['pending_product_link'] = product_link.group(1).lower()
    unavailable = joined is None
    key = "channel_check_unavailable" if unavailable else "channel_join_required"
    if query:
        alert_key = key if unavailable else "channel_not_joined"
        await query.answer(tr(lang, alert_key), show_alert=True)
    await _show_prompt(update, context, tr(lang, key), _keyboard(settings, lang, enabled, unavailable))
    raise ApplicationHandlerStop


async def handle_channel_gate_action(update, context):
    """Runs only after the membership and maintenance guards have allowed access."""
    query = update.callback_query
    enabled = await db.get_enabled_languages()
    data = str(query.data or "")
    if data.startswith(CALLBACK_PREFIX + "language:"):
        code = data[len(CALLBACK_PREFIX + "language:"):]
        if code in enabled:
            await db.set_user_language(query.from_user.id, code)
    saved_user = await db.upsert_user(query.from_user.id, query.from_user.username or "")
    lang = await _language(update, saved_user, enabled)
    await query.answer(tr(lang, "channel_join_verified"))
    await _show_home(update, context)


async def _show_home(update, context):
    # Lazy import keeps the guard independent of the shop/payment modules.
    from handlers.user import show_start
    await show_start(update, context)
