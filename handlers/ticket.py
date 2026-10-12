from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext

import database as db
from utils import answer_rich, edit_rich, send_admin_task_message, forward_admin_task_message, send_rich
from text_catalog import text as t, RichText
from utils import show_menu_with_sticker
from states import UserStates, AdminStates
from config import ADMIN_ID
from keyboards import (
    back_button, support_menu, admin_tickets_menu, admin_ticket_list_keyboard, admin_ticket_detail_keyboard,
    my_tickets_list_keyboard, ticket_thread_user_keyboard, ticket_user_reply_button,
)

router = Router(name="ticket")


def _ticket_text(ticket_id: int) -> str:
    ticket=db.get_ticket(ticket_id)
    if not ticket: return t("admin_ticket_not_found")
    user=db.get_user(ticket.get("telegram_id")) or {}
    lines=[f"#{ticket_id}",f"کاربر: {user.get('name') or '-'}",f"Telegram ID: {ticket.get('telegram_id')}",f"وضعیت: {'بسته' if ticket.get('status')!='open' else ('پاسخ داده شد' if ticket.get('last_sender')=='admin' else 'باز')}",f"آخرین ارسال: {'کاربر' if ticket.get('last_sender')=='user' else 'پشتیبانی'}","", "تاریخچه:"]
    for m in db.get_ticket_messages(ticket_id, limit=20):
        who="کاربر" if m.get("sender_type")=="user" else "پشتیبانی"
        body=str(m.get("message") or "")
        lines.append(f"{who} ({m.get('created_at','-')}):\n{body[:1200]}")
    return "\n".join(lines)[:3900]


def _reply_to_user_text(message: types.Message, body: str):
    """متن پاسخ ادمین برای کاربر؛ اگر پاسخ متنی بود entityهای آن (Premium Emoji، بولد، ...) حفظ می‌شود."""
    prefix = str(t("ticket_user_reply_prefix")) + "\n\n"
    prefix_rich = t("ticket_user_reply_prefix")
    raw = message.text or ""
    ents = list(getattr(message, "entities", None) or [])
    if raw and ents and raw.strip() == body:
        from utils import serialize_message_entities
        shift = len(prefix.encode("utf-16-le")) // 2
        extra = [dict(e, offset=int(e.get("offset", 0)) + shift) for e in serialize_message_entities(ents)]
        base = list(getattr(prefix_rich, "entities", None) or [])
        return RichText(prefix + raw, base + extra)
    return prefix_rich + "\n\n" + body


def _user_thread_text(ticket: dict, messages: list):
    """مکالمه‌ی کامل تیکت برای کاربر؛ اگر طولانی شد، فقط آخرین پیام‌ها نشان داده می‌شود.

    خروجی RichText است تا Premium Emoji عنوان و برچسب‌های «شما/پشتیبانی» (که از ویرایشگر
    متن قابل تغییرند) هنگام ارسال حفظ شوند."""
    title = t("ticket_thread_title", ticket_id=ticket["id"])
    blocks = []
    for m in messages:
        who = t("ticket_sender_user") if m.get("sender_type") == "user" else t("ticket_sender_admin")
        blocks.append(who + f":\n{m.get('message') or ''}\n")
    out = []
    size = len(str(title)) + 2
    for b in reversed(blocks):
        if size + len(str(b)) + 1 > 3800:
            break
        out.append(b); size += len(str(b)) + 1
    result = title + "\n\n"
    for i, b in enumerate(reversed(out)):
        result = result + (b if i == 0 else "\n" + b)
    return result


@router.callback_query(F.data == "my_tickets")
async def my_tickets(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    tickets = db.list_user_tickets(callback.from_user.id)
    text = t("my_tickets_empty") if not tickets else t("my_tickets_title")
    await answer_rich(callback.message, text, reply_markup=my_tickets_list_keyboard(tickets))
    await callback.answer()


@router.callback_query(F.data.startswith("myticketview_"))
async def my_ticket_view(callback: types.CallbackQuery):
    try:
        ticket_id = int(callback.data.replace("myticketview_", ""))
    except ValueError:
        await callback.answer(str(t("ticket_not_found_user")), show_alert=True); return
    ticket = db.get_ticket(ticket_id)
    if not ticket or str(ticket.get("telegram_id")) != str(callback.from_user.id):
        await callback.answer(str(t("ticket_not_found_user")), show_alert=True); return
    messages = db.get_ticket_messages(ticket_id, limit=200)
    await answer_rich(callback.message, _user_thread_text(ticket, messages),
                      reply_markup=ticket_thread_user_keyboard(ticket_id, ticket["status"]))
    await callback.answer()


@router.callback_query(F.data.startswith("uticketreply_"))
async def user_ticket_reply_start(callback: types.CallbackQuery, state: FSMContext):
    try:
        ticket_id = int(callback.data.replace("uticketreply_", ""))
    except ValueError:
        await callback.answer(str(t("ticket_unavailable")), show_alert=True); return
    ticket = db.get_ticket(ticket_id)
    if not ticket or str(ticket.get("telegram_id")) != str(callback.from_user.id) or ticket.get("status") != "open":
        await callback.answer(str(t("ticket_unavailable")), show_alert=True); return
    await state.clear()
    await state.update_data(user_ticket_id=ticket_id)
    await state.set_state(UserStates.waiting_ticket_message)
    await answer_rich(callback.message, t("ticket_reply_prompt"))
    await callback.answer()


@router.callback_query(F.data == "support")
async def support_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await show_menu_with_sticker(callback.bot, callback.message.chat.id, "support", t("support_intro"), reply_markup=support_menu())
    except Exception:
        await callback.message.answer(t("support_error"))
    await callback.answer()


@router.callback_query(F.data == "ticket")
async def ticket_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "ticket_write", t("ticket_write"), reply_markup=back_button("back", t("back")))
    await state.set_state(UserStates.waiting_ticket_message)
    await callback.answer()


@router.message(AdminStates.waiting_ticket_reply)
async def admin_reply_send(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID and not db.sub_admin_has_permission(str(message.from_user.id), "tickets"):
        return
    data=await state.get_data(); ticket_id=data.get("ticket_id")
    if not ticket_id and data.get("reply_target"):
        body=(message.text or message.caption or "").strip()
        if not body: return
        try:
            await send_rich(message.bot,int(data["reply_target"]),t("ticket_user_reply_prefix") + "\n\n" + body)
            await answer_rich(message,t("ticket_reply_sent"))
        except Exception:
            await answer_rich(message,t("ticket_reply_failed"))
        await state.clear(); return
    ticket=db.get_ticket(ticket_id) if ticket_id else None
    if not ticket or ticket.get("status")!="open":
        await answer_rich(message,t("admin_ticket_not_found")); await state.clear(); return
    body=(message.text or message.caption or "").strip()
    if not body: return
    if not db.add_ticket_message(int(ticket_id),"admin",str(message.from_user.id),body):
        await answer_rich(message,t("admin_ticket_not_found")); await state.clear(); return
    try:
        await send_rich(message.bot,int(ticket["telegram_id"]),_reply_to_user_text(message, body),
                        reply_markup=ticket_user_reply_button(int(ticket_id)))
        await answer_rich(message,t("ticket_reply_sent"))
    except Exception:
        await answer_rich(message,t("ticket_reply_failed"))
    await state.clear()


@router.callback_query(F.data.startswith("replyticket_"))
async def legacy_admin_reply_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    target_uid=callback.data.replace("replyticket_", "")
    await state.update_data(reply_target=target_uid)
    await state.set_state(AdminStates.waiting_ticket_reply)
    await callback.message.answer(f"پاسخ خود را برای کاربر {target_uid} بنویسید:")
    await callback.answer()


@router.callback_query(F.data.startswith("ticketreply_"))
async def admin_reply_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID and not db.sub_admin_has_permission(str(callback.from_user.id), "tickets"):
        await callback.answer("⛔ دسترسی ندارید.",show_alert=True); return
    ticket_id=callback.data.replace("ticketreply_","")
    try: ticket_id=int(ticket_id)
    except ValueError: await callback.answer(t("admin_ticket_not_found"),show_alert=True); return
    ticket=db.get_ticket(ticket_id)
    if not ticket or ticket.get("status")!="open": await callback.answer(t("admin_ticket_not_found"),show_alert=True); return
    await state.update_data(ticket_id=ticket_id)
    await state.set_state(AdminStates.waiting_ticket_reply)
    await callback.message.answer(t("admin_ticket_reply_prompt"))
    await callback.answer()


@router.callback_query(F.data.startswith("ticketclose_"))
async def admin_ticket_close(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID and not db.sub_admin_has_permission(str(callback.from_user.id), "tickets"):
        await callback.answer("⛔ دسترسی ندارید.",show_alert=True); return
    try: tid=int(callback.data.replace("ticketclose_",""))
    except ValueError: await callback.answer(t("admin_ticket_not_found"),show_alert=True); return
    if not db.set_ticket_status(tid,"closed"): await callback.answer(t("admin_ticket_not_found"),show_alert=True); return
    ticket=db.get_ticket(tid)
    try:
        await send_rich(callback.bot,int(ticket["telegram_id"]),t("ticket_closed_user",ticket_id=tid))
    except Exception:
        pass
    await edit_rich(callback.message,_ticket_text(tid),reply_markup=admin_ticket_detail_keyboard(ticket))
    await callback.answer(t("admin_ticket_closed_notice"))


@router.callback_query(F.data.startswith("ticketreopen_"))
async def admin_ticket_reopen(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID and not db.sub_admin_has_permission(str(callback.from_user.id), "tickets"):
        await callback.answer("⛔ دسترسی ندارید.",show_alert=True); return
    try: tid=int(callback.data.replace("ticketreopen_",""))
    except ValueError: await callback.answer(t("admin_ticket_not_found"),show_alert=True); return
    if not db.set_ticket_status(tid,"open"): await callback.answer(t("admin_ticket_not_found"),show_alert=True); return
    ticket=db.get_ticket(tid)
    await edit_rich(callback.message,_ticket_text(tid),reply_markup=admin_ticket_detail_keyboard(ticket))
    await callback.answer(t("admin_ticket_reopened_notice"))


@router.callback_query(F.data == "admin_tickets")
async def admin_tickets_open(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID and not db.sub_admin_has_permission(str(callback.from_user.id), "tickets"):
        await callback.answer("⛔ دسترسی ندارید.",show_alert=True); return
    await edit_rich(callback.message,"مدیریت تیکت",reply_markup=admin_tickets_menu(db.get_ticket_counts())); await callback.answer()

@router.message(F.text == t("admin_tickets"))
async def admin_tickets_reply_menu(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID and not db.sub_admin_has_permission(str(message.from_user.id), "tickets"): return
    await state.clear(); await answer_rich(message,t("admin_tickets"),reply_markup=admin_tickets_menu(db.get_ticket_counts()))

@router.callback_query(F.data.startswith("admintickets_"))
async def admin_ticket_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID and not db.sub_admin_has_permission(str(callback.from_user.id), "tickets"):
        await callback.answer("⛔ دسترسی ندارید.",show_alert=True); return
    parts=callback.data.split("_")   # admintickets_<filter>[_<offset>]
    kind=parts[1] if len(parts)>1 else ""
    try: offset=max(0,int(parts[2])) if len(parts)>2 else 0
    except ValueError: offset=0
    page=10
    if kind=="open": kw={"status":"open"}
    elif kind=="unanswered": kw={"unanswered":True}
    elif kind=="closed": kw={"status":"closed"}
    else:
        await callback.answer(t("admin_ticket_not_found"),show_alert=True); return
    total=db.count_tickets(**kw)
    items=db.list_tickets(limit=page,offset=offset,**kw)
    title=t({"open":"admin_tickets_open","unanswered":"admin_tickets_unanswered","closed":"admin_tickets_closed"}[kind],count=total)
    if not items:
        await edit_rich(callback.message,title+"\n\n"+t("admin_ticket_empty"),reply_markup=admin_tickets_menu(db.get_ticket_counts())); await callback.answer(); return
    await edit_rich(callback.message,title+"\n\n"+t("admin_ticket_list_hint"),
                    reply_markup=admin_ticket_list_keyboard(items,f"admintickets_{kind}",offset,offset+page<total,page)); await callback.answer()

@router.callback_query(F.data.startswith("adminticket_"))
async def admin_ticket_detail(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID and not db.sub_admin_has_permission(str(callback.from_user.id), "tickets"):
        await callback.answer("⛔ دسترسی ندارید.",show_alert=True); return
    try: tid=int(callback.data.replace("adminticket_",""))
    except ValueError: await callback.answer(t("admin_ticket_not_found"),show_alert=True); return
    ticket=db.get_ticket(tid)
    if not ticket: await callback.answer(t("admin_ticket_not_found"),show_alert=True); return
    await edit_rich(callback.message,_ticket_text(tid),reply_markup=admin_ticket_detail_keyboard(ticket)); await callback.answer()


@router.message(UserStates.waiting_ticket_message)
async def ticket_message(message: types.Message, state: FSMContext):
    body=(message.text or message.caption or "").strip()
    if not body and (message.photo or message.document or message.video or message.voice):
        body="[فایل/رسانه ارسال شد]"
    if not body:
        return
    user=db.get_user(message.from_user.id)
    data=await state.get_data()
    ticket=None
    tid_hint=data.get("user_ticket_id")
    if tid_hint:
        cand=db.get_ticket(int(tid_hint))
        if cand and str(cand.get("telegram_id"))==str(message.from_user.id) and cand.get("status")=="open":
            ticket=cand
    if ticket is None:
        # پیام‌های بعدی کاربر روی همان تیکت باز اضافه می‌شود؛ فقط اگر تیکت بازی نبود تیکت جدید ساخته می‌شود.
        ticket=db.get_open_ticket_for_telegram(message.from_user.id)
    is_new=ticket is None
    if is_new:
        ticket_id=db.create_ticket(str(message.from_user.id),user["id"] if user else None,body)
    else:
        ticket_id=int(ticket["id"])
        if not db.add_ticket_message(ticket_id,"user",str(message.from_user.id),body):
            ticket_id=db.create_ticket(str(message.from_user.id),user["id"] if user else None,body)
            is_new=True
    admin_text=t("admin_ticket_new_notice" if is_new else "admin_ticket_followup_notice",
                 ticket_id=ticket_id,name=message.from_user.full_name,telegram_id=message.from_user.id,body=body)
    from keyboards import admin_ticket_detail_keyboard
    await send_admin_task_message(message.bot,ADMIN_ID,"tickets",admin_text,reply_markup=admin_ticket_detail_keyboard(db.get_ticket(ticket_id)))
    if message.photo or message.document or message.video or message.voice:
        try: await forward_admin_task_message(message.bot,ADMIN_ID,"tickets",message.chat.id,message.message_id)
        except Exception: pass
    await answer_rich(message,t("ticket_sent") if is_new else t("ticket_added",ticket_id=ticket_id))
    await state.clear()
