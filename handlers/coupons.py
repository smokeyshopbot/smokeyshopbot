"""Private coupon redemption; financial and delivery work survives bot restarts."""
import logging
import re
import time
import database as db
from utils.coupons import redeem_async, recover_async, coupon_id
from utils.i18n import tr

logger = logging.getLogger(__name__)


def services():
    return {'pending_quantity': db.get_pending_stock_quantity, 'external_stock': db.get_external_supplier_available_stock,
            'fulfill': db.fulfill_product_items,
            'record_delivery': db.record_order_items_delivered_in_ledger}


async def cmd_redeem(update, context, *, code=None):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if update.effective_chat.type != 'private':
        profile = await db.get_user(user.id) or {}
        lang = profile.get('language', 'en')
        await message.reply_text(tr(lang, 'coupon_private'))
        return
    await db.upsert_user(user.id, user.username or '')
    profile = await db.get_user(user.id) or {}
    lang = profile.get('language', 'en')
    if profile.get('blocked'):
        return
    if code is None and len(context.args or []) != 1:
        context.user_data['coupon_waiting'] = True
        await message.reply_text(tr(lang, 'coupon_usage'))
        return
    code = code if code is not None else context.args[0]
    context.user_data.pop('coupon_waiting', None)
    current = time.monotonic()
    attempts = [at for at in context.user_data.get('coupon_attempts', []) if current - at < 60]
    if len(attempts) >= 6:
        await message.reply_text(tr(lang, 'coupon_rate_limit'))
        return
    context.user_data['coupon_attempts'] = attempts + [current]
    try:
        result = await redeem_async(db.get_db(), code, user.id, services())
    except Exception:
        logger.exception('Coupon redemption interrupted for user=%s', user.id)
        await message.reply_text(tr(lang, 'coupon_retry'))
        return
    status = result.get('status', 'processing')
    if status == 'invalid':
        context.user_data['coupon_waiting'] = True
    key = {'completed': 'coupon_success', 'already': 'coupon_already', 'used_other': 'coupon_used_other', 'invalid': 'coupon_invalid',
           'expired': 'coupon_expired', 'exhausted': 'coupon_exhausted', 'out_of_stock': 'coupon_out_of_stock',
           'unavailable': 'coupon_unavailable', 'pending_stock': 'coupon_pending', 'review': 'coupon_review'}.get(status, 'coupon_processing')
    await message.reply_text(tr(lang, key))


async def handle_coupon_input(update, context):
    """Accept prompted codes and known coupon codes pasted alone."""
    if not update.effective_user or not update.effective_message or update.effective_chat.type != 'private':
        return False
    text = str(update.effective_message.text or '').strip()
    if not text or text.startswith('/'):
        return False
    waiting = context.user_data.get('coupon_waiting')
    if not waiting and not re.fullmatch(r'SMO-[A-Z0-9_-]{4,60}', text, re.I):
        try:
            identifier = coupon_id(text)
        except ValueError:
            return False
        if not await db.get_db().coupons.find_one({'_id': identifier}, {'_id': 1}):
            return False
    await cmd_redeem(update, context, code=text)
    return True


async def recover_pending_coupons():
    database = db.get_db()
    if await database.coupons.find_one({'pending_count': {'$gt': 0}}, {'_id': 1}):
        return await recover_async(database, services())
    return 0
