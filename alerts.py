from utils import send_photo_rich, edit_caption_rich
from utils import send_rich
"""
alerts.py
بررسی دوره‌ای مصرف و تاریخ انقضای سرویس‌های VIP و اطلاع‌رسانی خودکار به کاربر
وقتی ۸۰٪/۹۰٪ حجم مصرف شده یا ۲ روز به پایان سرویس مانده است.
این هشدارها فقط مخصوص سرویس‌های VIP هستند (طبق درخواست کاربر).
"""
import logging

import crypto
import database as db
from text_catalog import text as t
from subscription import fetch_subscription_info, usage_bar, days_remaining, get_live_service_status, format_bytes
from keyboards import back_button, fair_use_keyboard, service_alert_80_90_keyboard, service_expired_alert_keyboard
import bot_info
import panels
from config import ADMIN_ID
from utils import send_notification_sticker, _repair_custom_emoji_entities, _sanitize_entities_for_text

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 1800  # هر ۳۰ دقیقه یک بار


async def check_usage_alerts(bot):
    """روی همه‌ی سرویس‌های VIP فعال حلقه می‌زند و در صورت لزوم هشدار می‌فرستد."""
    configs = db.get_active_vip_configs()
    for cfg in configs:
        try:
            await _check_single_config(bot, cfg)
        except Exception:
            logger.exception("خطا در بررسی هشدار مصرف برای سرویس %s", cfg.get("id"))


async def _check_single_config(bot, cfg):
    try:
        sub_link = crypto.decrypt_config(cfg["config"])
    except Exception:
        return
    if not sub_link.lower().startswith(("http://", "https://")):
        return

    usage = await fetch_subscription_info(sub_link)
    if not usage:
        return

    user = db.get_user_by_id(cfg["user_id"])
    if not user:
        return

    total = usage.get("total")
    used = (usage.get("upload") or 0) + (usage.get("download") or 0)
    expire_ts = usage.get("expire")

    # ابتدا پایان واقعی سرویس را بررسی می‌کنیم تا سرویس ۱۰۰٪ تمام‌شده در همان
    # چرخه همزمان هشدار ۹۰٪ و پایان نگیرد. غیرفعال‌شدن از خود پنل هم بررسی می‌شود.
    ended = bool(cfg.get("disabled"))
    if total:
        ended = ended or used >= total
    if expire_ts:
        try: ended = ended or int(expire_ts) <= int(__import__("time").time())
        except (TypeError,ValueError): pass
    if not ended:
        try: ended = (await get_live_service_status(cfg)) == "expired"
        except Exception: pass
    if ended:
        if not cfg.get("alert_expiry_sent") and await _send_expired_alert(bot,user,cfg):
            db.set_config_alert_sent(cfg["id"],"alert_expiry_sent")
            db.set_config_alert_sent(cfg["id"],"alert_80_sent")
            db.set_config_alert_sent(cfg["id"],"alert_90_sent")
        return

    if total:
        percent=min(100,int(used/total*100))
        if percent>=90 and not cfg.get("alert_90_sent"):
            if await _send_usage_alert(bot,user,cfg,percent):
                db.set_config_alert_sent(cfg["id"],"alert_90_sent")
                db.set_config_alert_sent(cfg["id"],"alert_80_sent")
        elif percent>=80 and not cfg.get("alert_80_sent"):
            if await _send_usage_alert(bot,user,cfg,percent):
                db.set_config_alert_sent(cfg["id"],"alert_80_sent")
    else:
        try: fair_gb=float(bot_info.get("fair_use_gb") or 0)
        except Exception: fair_gb=0
        if fair_gb>0 and used>=fair_gb*(1024**3) and not cfg.get("fair_use_alert_sent"):
            # Fair Use برای سرویس نامحدود: ابتدا سرویس واقعاً در پنل غیرفعال می‌شود،
            # سپس فقط یک‌بار هشدار برای کاربر ارسال می‌کنیم.
            disabled_ok = await _disable_fair_use_service(cfg)
            if not disabled_ok:
                logger.error("غیرفعال‌سازی سرویس %s در پنل برای Fair Use ناموفق بود", cfg.get("id"))
                return
            if await _send_fair_use_alert(bot,user,cfg,fair_gb):
                db.set_fair_use_alert_sent(cfg["id"],True)


def _config_package_name(cfg: dict) -> str:
    plan_key=str(cfg.get("plan_key") or "").strip()
    if plan_key:
        try:
            plan=db.get_effective_plan(plan_key)
            if plan and plan.get("name"): return str(plan["name"])
        except Exception: pass
    try:
        cid=int(cfg.get("category_id") or 0)
        plans=db.get_vip_plans(cid) if cid else []
        raw_candidate=str(cfg.get("plan") or "").strip()
        for plan in plans:
            if raw_candidate == str(plan.get("name") or "").strip(): return raw_candidate
        if len(plans)==1 and plans[0].get("name"): return str(plans[0]["name"])
    except Exception: pass
    raw=str(cfg.get("plan") or "سرویس")
    # سرویس‌های قدیمی نام کاربری را قبل از اولین | نگه می‌داشتند؛ برای آن‌ها
    # بخش بسته از قسمت‌های بعدی قابل تشخیص است.
    parts=[x.strip() for x in raw.split("|")]
    return " | ".join(parts[1:]) if len(parts)>1 else raw



def get_config_service_username(cfg: dict, panel_data: dict | None = None) -> str:
    """نام واقعی سرویس در پنل؛ برای لاگ/گزارش همیشه username اولویت دارد."""
    data = panel_data if isinstance(panel_data, dict) else {}
    return str(
        data.get("username")
        or cfg.get("service_id")
        or data.get("name")
        or cfg.get("_display_name")
        or "-"
    ).strip() or "-"


def get_config_package_name(cfg: dict) -> str:
    """نام دقیق پلن فروشگاه، بدون چسباندن username یا جزئیات تمدید."""
    return _config_package_name(cfg)


def expiry_text_from_panel_data(panel_data: dict | None, fallback: str | None = None) -> str:
    """انقضای واقعی ثبت‌شده در پنل را به تاریخ تهران تبدیل می‌کند."""
    data = panel_data if isinstance(panel_data, dict) else {}
    if "expire" in data:
        expire = data.get("expire")
        if not expire:
            return "نامحدود"
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            return datetime.fromtimestamp(int(expire), tz=ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d")
        except Exception:
            pass
    return str(fallback or "نامحدود")


async def log_renewal_to_channel(bot, user: dict, cfg: dict, panel_data: dict | None, amount: int | float, added_volume: float = 0, added_days: int = 0, payment_method: str = ""):
    """لاگ تمدید با قالب قابل ویرایش ادمین و روش پرداخت."""
    # 🆕 متغیر {panel_name} (🛡 پنل) دقیقاً مثل لاگ خرید: نام نمونه‌ی پنلی که سرویس روی آن ساخته شده.
    panel_name = None
    try:
        if cfg.get("panel_id"):
            panel_obj = db.get_vpn_panel(cfg.get("panel_id"))
            panel_name = (panel_obj or {}).get("name") or None
    except Exception:
        logger.exception("خواندن نام پنل برای لاگ تمدید ناموفق بود")
    await log_order_to_channel(
        bot,
        order_label="🔁 تمدید سرویس",
        user=user,
        username=None,
        service_id=cfg.get("service_id"),
        service_name=get_config_service_username(cfg, panel_data),
        package_text=get_config_package_name(cfg),
        amount_text=f"{int(amount or 0):,} تومان" if amount else "رایگان",
        expiry_text=expiry_text_from_panel_data(panel_data, cfg.get("expiry")),
        renewal_details=_renewal_log_details(added_volume, added_days),
        payment_method=payment_method or "-",
        panel_name=panel_name,
    )

    # گزارش ادمین (فرمت جدید) — خطای آن نباید تمدید یا لاگ کانال را خراب کند.
    try:
        await send_admin_order_report(
            bot,
            order_label="🔁 تمدید سرویس",
            payment_method=payment_method or "-",
            user=user,
            service_name=get_config_service_username(cfg, panel_data),
            package_text=get_config_package_name(cfg),
            amount_text=f"{int(amount or 0):,} تومان" if amount else "رایگان",
            panel_name=panel_name,
        )
    except Exception:
        logger.exception("ارسال گزارش تمدید برای ادمین ناموفق بود")


async def _send_usage_alert(bot, user, cfg, percent):
    bar = usage_bar(percent)
    key = "notif_usage_90" if percent >= 90 else "notif_usage_80"
    text = t(key, plan=_config_package_name(cfg), percent=percent, bar=bar)
    return await _safe_send(bot, user, cfg, text, sticker_key=key, reply_markup=service_alert_80_90_keyboard(cfg["id"]))


async def send_fair_use_request_to_admin(bot, user, cfg, fair_gb, usage=None):
    """ارسال درخواست کاربر برای ادامه مصرف منصفانه به ادمین اصلی، بدون دکمه."""
    usage = usage or {}
    used_bytes = (usage.get("upload") or 0) + (usage.get("download") or 0)
    service_id = str(cfg.get("service_id") or "-")
    service_name = get_config_service_username(cfg, usage)
    package_name = get_config_package_name(cfg)
    username = str(user.get("username") or "-").lstrip("@")
    customer_name = str(user.get("name") or "کاربر")
    expiry = expiry_text_from_panel_data(usage, cfg.get("expiry"))
    text = t(
        "fair_use_admin_request",
        customer_name=customer_name,
        username=username,
        telegram_id=user.get("telegram_id") or "-",
        plan=package_name,
        service_name=service_name,
        service_id=service_id,
        used=format_bytes(used_bytes),
        fair_use_gb=f"{float(fair_gb):g}",
        expiry=expiry,
    )
    await send_rich(bot, int(ADMIN_ID), text)
    return True


async def _disable_fair_use_service(cfg: dict) -> bool:
    """سرویس نامحدود را در همان پنلی که از آن ساخته شده غیرفعال می‌کند."""
    panel_id = cfg.get("panel_id")
    service_id = str(cfg.get("service_id") or "").strip()
    if not panel_id or not service_id:
        logger.error("سرویس %s برای Fair Use پنل/شناسه سرویس ندارد", cfg.get("id"))
        return False
    try:
        panel = db.get_vpn_panel(int(panel_id))
        if not panel:
            logger.error("پنل %s برای سرویس %s پیدا نشد", panel_id, cfg.get("id"))
            return False
        ok, msg = await panels.disable_service(panel, service_id)
        if not ok:
            logger.error("غیرفعال‌سازی Fair Use برای سرویس %s ناموفق بود: %s", cfg.get("id"), msg)
            return False
        logger.info("سرویس %s به دلیل رسیدن به Fair Use در پنل %s غیرفعال شد", cfg.get("id"), panel.get("name"))
        return True
    except Exception:
        logger.exception("خطا در غیرفعال‌سازی Fair Use سرویس %s", cfg.get("id"))
        return False

async def _send_fair_use_alert(bot, user, cfg, fair_gb):
    text=t("notif_fair_use",plan=_config_package_name(cfg),fair_use_gb=f"{fair_gb:g}")
    try:
        await send_notification_sticker(bot,int(user["telegram_id"]),"notif_usage_90")
        await send_rich(bot,int(user["telegram_id"]),text,reply_markup=fair_use_keyboard(cfg["id"]))
        return True
    except Exception:
        logger.exception("ارسال هشدار مصرف منصفانه ناموفق بود برای %s",user.get("telegram_id"))
        return False


async def _send_expired_alert(bot, user, cfg):
    text=t("notif_expiry", plan=_config_package_name(cfg), days_text="به پایان رسید")
    return await _safe_send(bot,user,cfg,text,sticker_key="notif_expiry",reply_markup=service_expired_alert_keyboard(cfg["id"]))


async def _safe_send(bot, user, cfg, text, sticker_key: str | None = None, reply_markup=None):
    try:
        if sticker_key:
            await send_notification_sticker(bot, int(user["telegram_id"]), sticker_key)
        await send_rich(bot, 
            int(user["telegram_id"]), text,
            reply_markup=reply_markup or back_button(f"viewconfig_{cfg['id']}", t("notif_view_service")),
        )
        return True
    except Exception:
        logger.exception("ارسال هشدار مصرف به کاربر %s ناموفق بود", user.get("telegram_id"))
        return False


# ---------------------------------------------------------------------------
# 🛎 لاگ همه‌ی سفارش‌های نهایی‌شده (خرید/تمدید/تست رایگان/سرویس سفارشی) در
# کانال «اعتماد»، با قالب ثابت.
# ---------------------------------------------------------------------------
async def fetch_username(bot, telegram_id) -> str:
    """نام کاربری تلگرام را بدون اینکه خطای API جلوی ثبت لاگ را بگیرد برمی‌گرداند."""
    try:
        chat = await bot.get_chat(int(telegram_id))
        return str(getattr(chat, "username", None) or "-").strip() or "-"
    except Exception:
        return "-"


def _mask_telegram_id(telegram_id) -> str:
    """آیدی عددی را برای حفظ حریم خصوصی، در پیام کانال اعتماد به‌شکل ماسک‌شده
    نمایش می‌دهد؛ مثلاً 6512345515 → 65*****515 (۲ رقم اول + ۳ رقم آخر باقی می‌مانند)."""
    s = str(telegram_id or "-")
    if len(s) <= 5:
        return s
    return s[:2] + "*" * (len(s) - 5) + s[-3:]


def _fix_unlimited_typo(value: str) -> str:
    """رفع تایپوی احتمالی «نامدود» (بجای «نامحدود») در متن‌های لاگ سفارش."""
    s = str(value or "")
    return s.replace("نامدود", "نامحدود") if "نامدود" in s else s


def _renewal_log_details(added_volume: float, added_days: int) -> str:
    parts = []
    if added_volume:
        parts.append(f"+{float(added_volume):g} گیگ")
    if added_days:
        parts.append(f"+{int(added_days)} روز")
    return " | ".join(parts) if parts else "بدون تغییر"


async def log_order_to_channel(
    bot,
    *,
    order_label: str,
    user: dict,
    username: str | None,
    service_id: str | None,
    service_name: str | None,
    package_text: str,
    amount_text: str,
    expiry_text: str,
    renewal_details: str | None = None,
    payment_method: str | None = None,
    panel_name: str | None = None,
    template_key: str | None = None,
):
    """ارسال لاگ سفارش با قالب‌های قابل ویرایش و پشتیبانی از Premium Emoji.

    template_key: اگر داده شود (مثلاً order_log_custom برای سرویس سفارشی)
    همان قالب استفاده می‌شود؛ وگرنه از روی order_label تشخیص داده می‌شود."""
    from utils import now_tehran

    package_text = _fix_unlimited_typo(package_text)
    expiry_text = _fix_unlimited_typo(expiry_text)
    if template_key:
        pass
    elif "تست" in order_label:
        template_key = "order_log_test"
    elif "تمدید" in order_label:
        template_key = "order_log_renewal"
    else:
        template_key = "order_log_purchase"

    values = {
        "order_label": order_label,
        "customer_name": user.get("name", "-"),
        "telegram_id": _mask_telegram_id(user.get("telegram_id")),
        "service_id": service_id or "-",
        "service_name": service_name or "-",
        "panel_name": panel_name or "-",
        "package_name": package_text or "-",
        "amount": amount_text or "-",
        "expiry": expiry_text or "-",
        "time": now_tehran().strftime("%Y-%m-%d %H:%M"),
        "payment_method": payment_method or "-",
        "renewal_details": renewal_details or "",
        "username": username or "-",
    }
    text = t(template_key, **values)

    # سفارش‌ها مسیر جداگانه‌ای برای ارسال دارند. برای اطمینان از اینکه
    # Premium Emoji تنظیم‌شده برای کلید order_log_* حتی اگر در entities_json
    # قالب به‌علت جایگزینی متغیرها از دست رفته باشد، دوباره روی fallback صحیح
    # خودش قرار بگیرد، ID ذخیره‌شده را از تنظیمات می‌خوانیم و alt واقعی آن را
    # از Telegram می‌گیریم. Custom Emoji باید دقیقاً یک کاراکتر fallback معتبر
    # را پوشش دهد؛ در غیر این صورت Telegram entity را نادیده می‌گیرد.
    try:
        saved_emoji_id = db.get_button_custom_emoji_id(template_key)
        if saved_emoji_id:
            raw_text = str(text)
            entities = list(getattr(text, "entities", None) or [])
            has_same_id = any(
                str(e.get("type")) == "custom_emoji"
                and str(e.get("custom_emoji_id")) == str(saved_emoji_id)
                for e in entities
                if isinstance(e, dict)
            )
            if not has_same_id:
                stickers = await bot.get_custom_emoji_stickers(
                    custom_emoji_ids=[str(saved_emoji_id)]
                )
                sticker = (stickers or [None])[0]
                alt = getattr(sticker, "emoji", None) if sticker else None

                if alt:
                    # پیدا کردن همان fallback در متن؛ نه «اولین ایموجی» به‌صورت
                    # حدسی. این تفاوت مهم است چون قالب لاگ چندین ایموجی دارد.
                    pos = raw_text.find(str(alt))
                    if pos >= 0:
                        offset = len(raw_text[:pos].encode("utf-16-le")) // 2
                        length = len(str(alt).encode("utf-16-le")) // 2
                        entities.append({
                            "type": "custom_emoji",
                            "offset": offset,
                            "length": length,
                            "custom_emoji_id": str(saved_emoji_id),
                        })
                        # RichText لازم نیست؛ خود send_message لیست entityها را
                        # مستقیماً دریافت می‌کند.
                        text_entities_for_log = entities
                    else:
                        text_entities_for_log = entities
                else:
                    text_entities_for_log = entities
            else:
                text_entities_for_log = entities
        else:
            text_entities_for_log = list(getattr(text, "entities", None) or [])
    except Exception:
        logger.exception("بازیابی Premium Emoji لاگ سفارش ناموفق بود")
        text_entities_for_log = list(getattr(text, "entities", None) or [])

    try:
        order_log_channel_id = bot_info.get("order_log_channel_id")
        if order_log_channel_id and str(order_log_channel_id) != "0":
            await _send_order_log_message(bot, order_log_channel_id, str(text), text_entities_for_log, template_key)
        else:
            logger.warning("کانال لاگ سفارش تنظیم نشده است (order_log_channel_id=%r)", order_log_channel_id)
    except Exception:
        logger.exception("ارسال لاگ سفارش به کانال/گروه اعتماد ناموفق بود")


def _count_custom_emoji(entities) -> int:
    n = 0
    for e in entities or []:
        typ = e.get("type") if isinstance(e, dict) else getattr(e, "type", None)
        typ = getattr(typ, "value", typ)
        if str(typ) == "custom_emoji":
            n += 1
    return n


async def _send_order_log_message(bot, chat_id, text: str, entities, template_key: str):
    """ارسال لاگ سفارش با Premium Emoji، همراه با تشخیص و fallback امن.

    - اگر تلگرام entityها را رد کند، یک بار بعد از repair و در نهایت به‌صورت متن ساده
      ارسال می‌شود تا لاگ هرگز گم نشود.
    - بعد از ارسال، entityهای پیام برگشتی از تلگرام بررسی می‌شود. اگر custom_emoji ارسال
      کرده باشیم ولی در پیام نهایی نباشد، دلیلش در لاگ ثبت می‌شود.
    """
    normalized = _sanitize_entities_for_text(text, entities) if entities else []
    sent_custom = _count_custom_emoji(normalized)
    logger.info("لاگ سفارش %s: %d entity (%d custom_emoji) به chat_id=%s ارسال می‌شود",
                template_key, len(normalized), sent_custom, chat_id)
    if entities and not normalized:
        logger.warning("لاگ سفارش %s: همه‌ی entityها بعد از اعتبارسنجی حذف شدند (offset نامعتبر)", template_key)

    attempts = []
    if normalized:
        attempts.append(normalized)
        try:
            repaired = await _repair_custom_emoji_entities(bot, text, entities)
            if repaired and repaired != normalized:
                attempts.append(repaired)
        except Exception:
            logger.exception("repair Premium Emoji لاگ سفارش ناموفق بود")

    last_exc = None
    for ents in attempts:
        try:
            msg = await bot.send_message(chat_id=chat_id, text=text, entities=ents or None, parse_mode=None)
            got = _count_custom_emoji(getattr(msg, "entities", None))
            if sent_custom and not got:
                logger.warning(
                    "لاگ سفارش %s: %d Premium Emoji فرستاده شد ولی تلگرام در پیام نهایی هیچ custom_emoji "
                    "برنگرداند. یعنی تلگرام آن‌ها را برای این چت (chat_id=%s) نادیده گرفته است "
                    "(نه اینکه کد entity را نفرستاده باشد).", template_key, sent_custom, chat_id)
            else:
                logger.info("لاگ سفارش %s ارسال شد؛ custom_emoji در پیام نهایی: %d", template_key, got)
            return msg
        except Exception as exc:
            last_exc = exc
            logger.warning("ارسال لاگ سفارش %s با entity ناموفق بود: %s", template_key, exc)

    # آخرین راه: متن ساده، تا لاگ حتماً به گروه/کانال برسد.
    if last_exc is not None:
        logger.error("لاگ سفارش %s به‌صورت متن ساده (بدون Premium Emoji) ارسال شد. خطای اصلی: %s", template_key, last_exc)
    return await bot.send_message(chat_id=chat_id, text=text, parse_mode=None)


def _gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """تبدیل تاریخ میلادی به شمسی (بدون وابستگی به کتابخانه‌ی خارجی)."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def jalali_now_text() -> str:
    """زمان فعلی تهران به‌صورت شمسی، مثل 1405-07-07 03:36"""
    from utils import now_tehran
    now = now_tehran()
    jy, jm, jd = _gregorian_to_jalali(now.year, now.month, now.day)
    return f"{jy:04d}-{jm:02d}-{jd:02d} {now.strftime('%H:%M')}"


async def send_admin_order_report(
    bot,
    *,
    order_label: str,
    payment_method: str | None,
    user: dict,
    service_name: str | None,
    package_text: str | None,
    amount_text: str | None,
    panel_name: str | None = None,
):
    """گزارش نهایی خرید/تست/تمدید برای ادمین اصلی (با دکمه‌ی مدیریت کاربر).
    این گزارش بعد از پیام «پاسخ پنل» ارسال می‌شود و جایگزین همه‌ی گزارش‌های قبلی ادمین است."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    label = str(order_label or "")
    if "تست" in label:
        kind = "تست"
    elif "تمدید" in label:
        kind = "تمدید"
    else:
        kind = "خرید"

    if panel_name:
        if kind == "تمدید":
            action_line = f"به صورت خودکار از پنل {panel_name} تمدید شد ✅"
        else:
            action_line = f"به صورت خودکار از پنل {panel_name} ساخته و ارسال شد ✅"
    else:
        action_line = "تمدید شد ✅" if kind == "تمدید" else "ارسال شد ✅"

    telegram_id = str(user.get("telegram_id") or "-")
    username = await fetch_username(bot, telegram_id) if telegram_id != "-" else "-"
    name = str(user.get("name") or "-")
    who = f"{name}-@{username}" if username and username != "-" else name

    text = (
        f"🛒 {kind}-{payment_method or '-'}\n"
        f"{action_line}\n\n"
        f"👤 {who}\n"
        f"🆔ایدی عددی:{telegram_id}\n"
        f"📌 {service_name or '-'}\n\n"
        f"💰 {amount_text or '-'}\n"
        f"📦 {_fix_unlimited_typo(package_text or '-')}\n"
        f"⏰ {jalali_now_text()} (به وقت تهران)"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👤 مدیریت کاربر", callback_data=f"useropen_{telegram_id}", style="primary")
    ]]) if telegram_id != "-" else None
    await send_rich(bot, int(ADMIN_ID), text, reply_markup=keyboard)


import time as _time

UNIQUEPAY_ALERT_COOLDOWN_SECONDS = 30 * 60  # حداقل ۳۰ دقیقه بین دو هشدار مشابه
UNIQUEPAY_FAILURE_RATE_THRESHOLD = 0.5      # اگر بیش از ۵۰٪ چک‌های یک چرخه fail شوند
UNIQUEPAY_MIN_SAMPLE = 3                    # حداقل تعداد نمونه برای معنادار بودن نرخ

_uniquepay_state = {
    "last_check_alert_at": 0.0,
    "last_create_alert_at": 0.0,
    "create_fail_streak": 0,
}


async def report_uniquepay_check_cycle(bot, admin_id, checked: int, failed: int):
    """بعد از هر چرخه‌ی کامل پولر (بررسی همه‌ی اینوویس‌های در انتظار) صدا زده
    می‌شود. اگر نرخ خطا از آستانه بیشتر باشد، یک هشدار (با cooldown) می‌فرستد."""
    if checked < UNIQUEPAY_MIN_SAMPLE or failed == 0:
        return
    rate = failed / checked
    if rate < UNIQUEPAY_FAILURE_RATE_THRESHOLD:
        return

    now = _time.time()
    if now - _uniquepay_state["last_check_alert_at"] < UNIQUEPAY_ALERT_COOLDOWN_SECONDS:
        return
    _uniquepay_state["last_check_alert_at"] = now

    text = (
        "⚠️ هشدار درگاه پرداخت آنلاین (یونیک‌پی)\n\n"
        f"در آخرین چرخه‌ی بررسی، {failed} از {checked} چک وضعیت اینوویس ({round(rate * 100)}٪) "
        "با خطا مواجه شد.\n\n"
        "احتمالاً یونیک‌پی قطعی یا کند شده. تا رفع مشکل، بهتره کاربرها رو به پرداخت "
        "کارت‌به‌کارت یا کیف پول راهنمایی کنی.\n\n"
        "(این هشدار حداکثر هر ۳۰ دقیقه یک‌بار فرستاده می‌شود.)"
    )
    try:
        await bot.send_message(admin_id, text)
    except Exception:
        logger.exception("ارسال هشدار قطعی یونیک‌پی به ادمین ناموفق بود")


async def report_uniquepay_create_failure(bot, admin_id):
    """هر بار که ساخت اینوویس (create_invoice) برای یک کاربر شکست بخورد صدا
    زده می‌شود. بعد از ۳ شکست پشت‌سرهم (بدون هیچ موفقیت میانی)، یک هشدار
    می‌فرستد؛ با موفقیت بعدی، شمارنده صفر می‌شود."""
    _uniquepay_state["create_fail_streak"] += 1
    if _uniquepay_state["create_fail_streak"] < 3:
        return

    now = _time.time()
    if now - _uniquepay_state["last_create_alert_at"] < UNIQUEPAY_ALERT_COOLDOWN_SECONDS:
        return
    _uniquepay_state["last_create_alert_at"] = now

    text = (
        "⚠️ هشدار درگاه پرداخت آنلاین (یونیک‌پی)\n\n"
        f"{_uniquepay_state['create_fail_streak']} کاربر پشت‌سرهم موفق به ساخت لینک پرداخت آنلاین نشدند "
        "و به کارت‌به‌کارت هدایت شدند.\n\n"
        "احتمالاً یونیک‌پی قطعی یا کند شده. بد نیست پنل یونیک‌پی رو چک کنی.\n\n"
        "(این هشدار حداکثر هر ۳۰ دقیقه یک‌بار فرستاده می‌شود.)"
    )
    try:
        await bot.send_message(admin_id, text)
    except Exception:
        logger.exception("ارسال هشدار قطعی یونیک‌پی به ادمین ناموفق بود")


def report_uniquepay_create_success():
    _uniquepay_state["create_fail_streak"] = 0
