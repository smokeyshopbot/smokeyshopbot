"""Public stock-alert group information, available before the membership gate."""
import html
import logging
import re
import time

import database as db
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from utils.channel_gate_settings import required_chats, validate_channel_settings
from utils.group_notifications import settings as group_settings
from utils.i18n import normalize_lang, tr

logger = logging.getLogger(__name__)


async def _group_link(raw, context):
    current = group_settings(raw)
    target = current['group_notifications_chat_id']
    if not re.fullmatch(r'(?:@[A-Za-z][A-Za-z0-9_]{4,31}|-[1-9][0-9]*)', target):
        return ''
    public = target if target.startswith('@') else ''
    supplied = current.get('group_notifications_join_url') or ''
    if supplied or public:
        return validate_channel_settings(False, public, supplied)['required_channel_join_url']
    try:
        for chat in required_chats(raw):
            if chat['chat_id'] == target:
                return chat['join_url']
    except ValueError:
        pass  # A bad access setting must not prevent sharing a valid group link.
    cache = context.bot_data.setdefault('stock_group_link_cache', {})
    entry = cache.get(target)
    if entry and entry[0] > time.monotonic():
        return entry[1]
    chat = await context.bot.get_chat(chat_id=int(target),
        read_timeout=3, connect_timeout=3, write_timeout=3, pool_timeout=3)
    username = str(getattr(chat, 'username', '') or '')
    if getattr(chat, 'type', '') not in {'group', 'supergroup'} or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', username):
        return ''  # Private groups need an owner-supplied invite link.
    link = 'https://t.me/' + username
    if len(cache) >= 16:
        cache.clear()
    cache[target] = (time.monotonic() + 60, link)
    return link


async def cmd_group(update, context):
    user = update.effective_user
    if not user or user.is_bot or not update.effective_message:
        return
    saved = await db.upsert_user(user.id, user.username or '')
    enabled = await db.get_enabled_languages()
    lang = normalize_lang(str(saved.get('language') or saved.get('language_code') or user.language_code or 'en').replace('_', '-').split('-', 1)[0])
    if lang not in enabled:
        lang = 'en'
    if await db.is_blocked(user.id):
        await update.effective_message.reply_text(tr(lang, 'blocked'))
        return
    raw = await db.get_secret_settings()
    try:
        link = await _group_link(raw, context)
    except (ValueError, TelegramError) as error:
        logger.warning('Could not resolve stock notification group link (%s).', type(error).__name__)
        link = ''
    if not link:
        await update.effective_message.reply_text(tr(lang, 'stock_group_unavailable'))
        return
    path = link.removeprefix('https://t.me/')
    shown = '@' + path if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', path) else link
    text = tr(lang, 'stock_group_title') + '\n\n<code>' + html.escape(shown) + '</code>\n\n' + tr(lang, 'stock_group_description')
    await update.effective_message.reply_text(text, parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tr(lang, 'btn_stock_group'), url=link)]]))
