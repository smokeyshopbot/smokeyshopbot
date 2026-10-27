"""Paged, personalized API catalogue; copyable IDs stay in the reseller area."""
import html
from utils.product_emojis import label as product_emoji_label
import re

import database as db
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from utils.i18n import tr

PAGE_SIZE = 6


def public_id(product):
    value = re.sub(r'[^a-z0-9_-]+', '-', str(product.get('api_product_id') or '').strip().lower())
    value = re.sub(r'-{2,}', '-', value).strip('-_')[:80]
    return value or str(product.get('_id') or '')


async def display(update, context, page=0):
    uid = update.effective_user.id
    lang = await db.get_user_language(uid)
    summary = await db.get_user_api_summary(uid)
    if not update.effective_chat or update.effective_chat.type != 'private':
        text = tr(lang, 'api_private')
        rows = []
    elif summary.get('maintenance'):
        text = tr(lang, 'api_maintenance')
        rows = [[InlineKeyboardButton(tr(lang, 'api_title_plain'), callback_data='nav:api')]]
    elif await db.is_blocked(uid) or summary.get('disabled'):
        text = tr(lang, 'api_disabled')
        rows = [[InlineKeyboardButton(tr(lang, 'api_title_plain'), callback_data='nav:api')]]
    elif not summary.get('enabled'):
        text = tr(lang, 'api_catalog_generate')
        rows = [[InlineKeyboardButton(tr(lang, 'api_title_plain'), callback_data='nav:api')]]
    else:
        keyword = str(context.user_data.get('api_catalog_query') or '').casefold()
        products = [p for p in await db.get_all_products_with_availability()
                    if p.get('enabled') is not False and p.get('api_enabled') is not False
                    and (not keyword or keyword in (str(p.get('name', '')) + ' ' + public_id(p)).casefold())]
        products = await db.apply_effective_prices_to_products_for_user(products, uid)
        pages = max(1, (len(products) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(page, 0), pages - 1)
        lines = [tr(lang, 'api_catalog_title', page=page + 1, pages=pages), tr(lang, 'api_catalog_hint')]
        for product in products[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            lines.extend(['', html.escape(product_emoji_label(product, str(product.get('name', ''))[:200])),
                          tr(lang, 'api_catalog_id') + ' <code>' + html.escape(public_id(product)) + '</code>',
                          tr(lang, 'api_catalog_price', price=f"{max(0, float(product.get('price_usdt') or 0)):.6f}"),
                          tr(lang, 'api_catalog_stock', stock=int(product.get('available_stock') or 0))])
        if not products:
            lines.append(tr(lang, 'api_catalog_empty'))
        text = '\n'.join(lines)
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton(tr(lang, 'btn_prev'), callback_data=f'api_catalog:page:{page-1}'))
        if page + 1 < pages:
            navigation.append(InlineKeyboardButton(tr(lang, 'btn_next'), callback_data=f'api_catalog:page:{page+1}'))
        rows = [navigation] if navigation else []
        rows.extend([[InlineKeyboardButton(tr(lang, 'api_catalog_refresh'), callback_data=f'api_catalog:page:{page}')],
                     [InlineKeyboardButton(tr(lang, 'api_title_plain'), callback_data='nav:api')]])
    markup = InlineKeyboardMarkup(rows) if rows else None
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode='HTML', reply_markup=markup)
        except BadRequest as error:
            if 'message is not modified' not in str(error).lower():
                raise
    else:
        await update.effective_message.reply_text(text, parse_mode='HTML', reply_markup=markup)


async def cmd_api_products(update, context):
    await db.upsert_user(update.effective_user.id, update.effective_user.username or '')
    context.user_data['api_catalog_query'] = ' '.join(context.args or [])[:100]
    await display(update, context)


async def handle_api_catalog(update, context):
    await update.callback_query.answer()
    if update.callback_query.data == 'api_catalog:open':
        context.user_data['api_catalog_query'] = ''
        page = 0
    else:
        try:
            page = int(update.callback_query.data.rsplit(':', 1)[1])
        except ValueError:
            page = 0
    await display(update, context, page)
