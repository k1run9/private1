import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
)
from aiogram.exceptions import TelegramBadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = int(os.getenv("PRIVATE_CHANNEL_ID"))
SUB_DAYS = int(os.getenv("SUB_DAYS", "30"))

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

DB_PATH = "access.db"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS members (
    user_id INTEGER PRIMARY KEY,
    expires_at INTEGER,
    plan TEXT NOT NULL
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE_SQL)
        await db.commit()

# --------------------- Утилиты доступа ---------------------

async def grant_access(user_id: int, days: int | None, plan: str):
    if days:
        until = datetime.now(timezone.utc) + timedelta(days=days)
        expire_ts = int(until.timestamp())
    else:
        expire_ts = None  # навсегда

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO members(user_id, expires_at, plan) VALUES(?,?,?)\n"
            "ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at, plan=excluded.plan",
            (user_id, expire_ts, plan),
        )
        await db.commit()

    # Создаём одноразовую ссылку-приглашение
    expire_arg = expire_ts if expire_ts else None
    invite = await bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        name=f"invite_{user_id}_{int(time.time())}",
        expire_date=expire_arg,
        member_limit=1,
        creates_join_request=False,
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👉 Войти в приватный канал", url=invite.invite_link)]])
    if expire_ts:
        until = datetime.fromtimestamp(expire_ts, tz=timezone.utc)
        text = f"Доступ выдан до <b>{until.strftime('%d.%m.%Y %H:%M UTC')}</b>."
    else:
        text = "Доступ выдан <b>навсегда</b>."
    await bot.send_message(user_id, text, reply_markup=kb, parse_mode="HTML")

async def revoke_if_expired():
    now_ts = int(datetime.now(timezone.utc).timestamp())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, expires_at FROM members WHERE expires_at IS NOT NULL AND expires_at < ?", (now_ts,)) as cur:
            rows = await cur.fetchall()
    for user_id, expires_at in rows:
        try:
            await bot.ban_chat_member(CHANNEL_ID, user_id)
            await bot.unban_chat_member(CHANNEL_ID, user_id)
            await bot.send_message(user_id, "Срок подписки истёк, доступ к каналу закрыт. Вы можете продлить подписку командой /buy.")
        except TelegramBadRequest:
            pass
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM members WHERE user_id=?", (user_id,))
            await db.commit()

scheduler.add_job(revoke_if_expired, "interval", minutes=15, id="revoke_job", replace_existing=True)

# --------------------- Команды бота ---------------------

@dp.message(Command("start"))
async def cmd_start(m: Message):
    text = (
        "Привет! Я бот доступа в приватный канал.\n\n"
        "Выберите тариф:\n"
        "• 20 ⭐ за месяц (30 дней)\n"
        "• 100 ⭐ навсегда\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💫 Месяц — 20 ⭐", callback_data="buy_month")],
        [InlineKeyboardButton(text="💎 Навсегда — 100 ⭐", callback_data="buy_forever")],
        [InlineKeyboardButton(text="ℹ️ Статус", callback_data="status")],
    ])
    await m.answer(text, reply_markup=kb)

@dp.message(Command("buy"))
async def cmd_buy(m: Message):
    await cmd_start(m)

@dp.callback_query(F.data == "status")
async def cb_status(c: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT expires_at, plan FROM members WHERE user_id=?", (c.from_user.id,)) as cur:
            row = await cur.fetchone()
    if row:
        exp, plan = row
        if exp:
            expires = datetime.fromtimestamp(exp, tz=timezone.utc)
            await c.message.answer(f"Ваша подписка ({plan}) активна до <b>{expires.strftime('%d.%m.%Y %H:%M UTC')}</b>.", parse_mode="HTML")
        else:
            await c.message.answer(f"У вас <b>навсегда</b> ({plan}).", parse_mode="HTML")
    else:
        await c.message.answer("Подписка не найдена. Нажмите /buy для оформления.")
    await c.answer()

# ---------------- Оплата Stars ----------------

MONTH_PRICE = 20  # XTR
FOREVER_PRICE = 100  # XTR

@dp.callback_query(F.data == "buy_month")
async def cb_buy_month(c: CallbackQuery):
    prices = [LabeledPrice(label="30 дней доступа", amount=MONTH_PRICE)]
    await bot.send_invoice(
        chat_id=c.from_user.id,
        title="Доступ в канал (месяц)",
        description="30 дней доступа в приватный канал",
        payload=json.dumps({"user_id": c.from_user.id, "plan": "month"}),
        provider_token="",  # для Stars пусто
        currency="XTR",
        prices=prices,
        start_parameter="month_plan",
    )
    await c.answer()

@dp.callback_query(F.data == "buy_forever")
async def cb_buy_forever(c: CallbackQuery):
    prices = [LabeledPrice(label="Навсегда", amount=FOREVER_PRICE)]
    await bot.send_invoice(
        chat_id=c.from_user.id,
        title="Доступ в канал (навсегда)",
        description="Неограниченный доступ в приватный канал",
        payload=json.dumps({"user_id": c.from_user.id, "plan": "forever"}),
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter="forever_plan",
    )
    await c.answer()

@dp.pre_checkout_query()
async def process_pre_checkout(pcq):
    await bot.answer_pre_checkout_query(pre_checkout_query_id=pcq.id, ok=True)

@dp.message(F.successful_payment)
async def got_payment(m: Message):
    sp = m.successful_payment
    meta = {}
    try:
        meta = json.loads(sp.invoice_payload or "{}")
    except json.JSONDecodeError:
        pass
    user_id = int(meta.get("user_id") or m.from_user.id)
    plan = meta.get("plan", "month")

    if plan == "month":
        await grant_access(user_id, SUB_DAYS, plan="month")
        await m.answer("Оплата получена ⭐. Доступ на месяц выдан!")
    elif plan == "forever":
        await grant_access(user_id, None, plan="forever")
        await m.answer("Оплата получена ⭐. Доступ навсегда выдан!")

    # --- уведомление админа ---
    try:
        await bot.send_message(
            ADMIN_ID,
            f"Новая покупка!\nПользователь: {m.from_user.full_name} (@{m.from_user.username})\nТариф: {plan}"
        )
    except TelegramBadRequest:
        pass

# ---------------- Админ-команды ----------------

@dp.message(Command("cancel"))
async def cmd_cancel(m: Message):
    try:
        await bot.ban_chat_member(CHANNEL_ID, m.from_user.id)
        await bot.unban_chat_member(CHANNEL_ID, m.from_user.id)
    except TelegramBadRequest:
        pass
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM members WHERE user_id=?", (m.from_user.id,))
        await db.commit()
    await m.answer("Вы удалены из канала. Возвращайтесь в любое время через /buy.")

# ---------------- Startup ----------------

async def main():
    await init_db()
    scheduler.start()
    print("Bot polling started...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
