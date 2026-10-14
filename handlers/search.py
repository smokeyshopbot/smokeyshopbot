"""Customer product search with localized descriptions and paged results."""
import unicodedata
import database as db
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from utils.i18n import tr

PAGE_SIZE = 8
_search_flow = {}


def clear_search_flow(user_id):
    try: _search_flow.pop(int(user_id), None)
    except (ValueError, TypeError): pass


def _fold(text):
    return "".join(char for char in unicodedata.normalize("NFKD", str(text or "").casefold()) if not unicodedata.combining(char))


def matches(product, keyword, lang):
    if product.get("enabled") is False: return False
    haystack = _fold(str(product.get("name", "")) + " " + str(product.get("description_" + lang) or product.get("description_en") or product.get("description") or ""))
    return all(word in haystack for word in _fold(keyword).split())


def _product_helpers():
    from handlers.user import _product_button_label, _product_callback
    return _product_button_label, _product_callback


async def _display(update, text, markup):
    if update.callback_query:
        from telegram.error import BadRequest
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode=None)
        except BadRequest as error:
            if "message is not modified" not in str(error).lower():
                await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=None)
    else: await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=None)


async def _prompt(update):
    uid = update.effective_user.id
    lang = await db.get_user_language(uid)
    _search_flow[uid] = {"awaiting": True}
    await _display(update, tr(lang, "search_prompt"), InlineKeyboardMarkup([[InlineKeyboardButton(tr(lang, "btn_back_shop"), callback_data="nav:shop")]]))


async def _results(update, keyword, page=0):
    uid = update.effective_user.id
    lang = await db.get_user_language(uid)
    if not keyword.strip() or len(keyword) > 100:
        await _display(update, tr(lang, "search_invalid"), InlineKeyboardMarkup([[InlineKeyboardButton(tr(lang, "btn_search"), callback_data="search:new")]]))
        _search_flow[uid] = {"awaiting": True}
        return
    products = [product for product in await db.get_all_products_with_availability() if matches(product, keyword, lang)]
    products = await db.apply_effective_prices_to_products_for_user(products, uid)
    settings = await db.get_payment_settings()
    pages = max(1, (len(products) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    label, callback = _product_helpers()
    rows = [[InlineKeyboardButton(label(product, settings, lang), callback_data=callback("product", product))] for product in products[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
    navigation = []
    if page: navigation.append(InlineKeyboardButton(tr(lang, "btn_prev"), callback_data=f"search:page:{page - 1}"))
    if page + 1 < pages: navigation.append(InlineKeyboardButton(tr(lang, "btn_next"), callback_data=f"search:page:{page + 1}"))
    if navigation: rows.append(navigation)
    rows.append([InlineKeyboardButton(tr(lang, "search_again"), callback_data="search:new")])
    rows.append([InlineKeyboardButton(tr(lang, "btn_back_shop"), callback_data="nav:shop")])
    _search_flow[uid] = {"awaiting": False, "keyword": keyword}
    text = tr(lang, "search_results", keyword=keyword, count=len(products), page=page + 1, pages=pages) if products else tr(lang, "search_empty", keyword=keyword)
    await _display(update, text, InlineKeyboardMarkup(rows))


async def cmd_search(update, context):
    await db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    if await db.is_blocked(update.effective_user.id): return
    keyword = " ".join(context.args or []).strip()
    if keyword: await _results(update, keyword)
    else: await _prompt(update)


async def handle_search_callback(update, context):
    query = update.callback_query
    await query.answer()
    if await db.is_blocked(query.from_user.id): return
    data = str(query.data or "")
    if data.startswith("search:page:"):
        keyword = _search_flow.get(query.from_user.id, {}).get("keyword")
        if not keyword: return await _prompt(update)
        try: page = int(data.split(":")[-1])
        except ValueError: page = 0
        return await _results(update, keyword, page)
    await _prompt(update)


async def handle_search_input(update, context):
    uid = update.effective_user.id
    if not _search_flow.get(uid, {}).get("awaiting"): return False
    text = str(update.effective_message.text or "")
    if text.startswith("/"): return False
    await _results(update, text.strip())
    return True
