import asyncio
import json
import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from faster_whisper import WhisperModel
from openai import AsyncOpenAI

load_dotenv()

TG_TOKEN = os.getenv("TG_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
MONTHLY_GOAL = int(os.getenv("MONTHLY_GOAL", "40000"))

DB_PATH = "tracker.db"

bot = Bot(token=TG_TOKEN)
dp = Dispatcher()

# Клиент OpenRouter для разбора естественной речи в JSON
llm = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# Лёгкая модель Whisper (работает на CPU любого VPS без видеокарты)
print("Загружаю модель Whisper для распознавания голоса...")
whisper = WhisperModel("tiny", device="cpu", compute_type="int8")


# ================= БАЗА ДАННЫХ (SQLite) =================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client TEXT NOT NULL,
                title TEXT NOT NULL,
                total_videos INTEGER DEFAULT 1,
                done_videos INTEGER DEFAULT 0,
                total_amount INTEGER DEFAULT 0,
                paid_amount INTEGER DEFAULT 0,
                deadline TEXT,
                status TEXT DEFAULT 'active',
                rem_12h INTEGER DEFAULT 0,
                rem_6h INTEGER DEFAULT 0,
                rem_3h INTEGER DEFAULT 0,
                rem_1h INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                deadline TEXT,
                status TEXT DEFAULT 'active',
                rem_12h INTEGER DEFAULT 0,
                rem_6h INTEGER DEFAULT 0,
                rem_3h INTEGER DEFAULT 0,
                rem_1h INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)


# ================= ИИ-ПАРСЕР СООБЩЕНИЙ =================
async def parse_user_intent(text: str) -> dict:
    """Превращает голосовую расшифровку или текст в структурированный JSON."""
    now_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M (%A)")
    system_prompt = f"""
Ты — точный парсер задач и заказов для видеомонтажёра.
Текущее время пользователя: {now_str}.

Определи, что хочет пользователь:
1. "order" — новый заказ на монтаж (упомянут клиент, оплата, пакет роликов или монтаж для кого-то).
2. "task" — личная или рабочая задача (например, "на завтра...", "смонтировать...", "купить...", "позвонить...").

Правила времени:
- Если сказано "завтра" без точного часа — ставь 12:00 завтрашнего дня.
- Если сказано "сдать в пятницу" без часа — ставь 18:00 указанного дня.
- Если время вообще не указано — ставь завтра в 12:00.
- Формат deadline строго: "YYYY-MM-DD HH:MM".

Верни СТРОГО валидный JSON без markdown-обёртки:
{{
  "type": "order" или "task",
  "client": "Имя клиента (для order, иначе пусто)",
  "title": "Суть заказа или задачи",
  "total_videos": число (по умолчанию 1),
  "total_amount": общая сумма в рублях числом (0 если не указано),
  "paid_amount": внесённая предоплата числом (0 если не указано),
  "deadline": "YYYY-MM-DD HH:MM"
}}
"""
    resp = await llm.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        temperature=0.1,
    )
    raw = resp.choices[0].message.content.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)


def transcribe_audio(file_path: str) -> str:
    segments, _ = whisper.transcribe(file_path, language="ru")
    return " ".join(seg.text.strip() for seg in segments).strip()


# ================= КЛАВИАТУРЫ =================
def main_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 Заказы"), KeyboardButton(text="📅 На завтра")],
            [KeyboardButton(text="📝 Все задачи"), KeyboardButton(text="💰 Касса")],
        ],
        resize_keyboard=True,
    )


def order_inline_kb(order_id: int, has_debt: bool):
    buttons = [
        [
            InlineKeyboardButton(text="🎬 +1 ролик", callback_data=f"ord_vid:{order_id}"),
            InlineKeyboardButton(text="✅ Сдан", callback_data=f"ord_done:{order_id}"),
        ]
    ]
    if has_debt:
        buttons[0].insert(1, InlineKeyboardButton(text="💸 Доплатил", callback_data=f"ord_pay:{order_id}"))
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def task_inline_kb(task_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Выполнено", callback_data=f"tsk_done:{task_id}")]
        ]
    )


# ================= ПРОВЕРКА ДОСТУПА =================
def is_owner(message: types.Message) -> bool:
    return ALLOWED_USER_ID == 0 or message.from_user.id == ALLOWED_USER_ID


# ================= ОБРАБОТЧИКИ КОМАНД И МЕНЮ =================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_owner(message):
        return
    await message.answer(
        "👋 **Личный трекер запущен.**\n\n"
        "Просто запиши **голосовое** или напиши текстом:\n"
        "• *«Новый заказ: Артём, 5 рилс за 7500, предоплата 3000, сдать в четверг в 18:00»*\n"
        "• *«На завтра в 11:00 подобрать музыку и нарезать A-roll»*\n\n"
        "Я сам занесу всё в базу и напомню за **12ч, 6ч, 3ч и 1ч** до дедлайна.",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown",
    )


@dp.message(F.text.in_({"📦 Заказы", "/orders"}))
async def show_orders(message: types.Message):
    if not is_owner(message):
        return
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, client, title, total_videos, done_videos, total_amount, paid_amount, deadline "
            "FROM orders WHERE status = 'active' ORDER BY deadline ASC"
        ).fetchall()

    if not rows:
        await message.answer("Активных заказов пока нет. Надиктуй новый голосом!")
        return

    await message.answer(f"📦 **Активные заказы ({len(rows)}):**", parse_mode="Markdown")
    for r in rows:
        oid, client, title, tot_v, done_v, tot_a, paid_a, dl = r
        debt = max(0, tot_a - paid_a)
        dl_fmt = datetime.strptime(dl, "%Y-%m-%d %H:%M").strftime("%d.%m в %H:%M") if dl else "без срока"
        text = (
            f"👤 **{client}** — {title}\n"
            f"🎬 Прогресс: `{done_v}/{tot_v}` роликов\n"
            f"💵 Оплата: `{paid_a:,} / {tot_a:,} ₽` (остаток: `{debt:,} ₽`)\n"
            f"⏰ Дедлайн: `{dl_fmt}`"
        ).replace(",", " ")
        await message.answer(text, reply_markup=order_inline_kb(oid, debt > 0), parse_mode="Markdown")


@dp.message(F.text.in_({"📅 На завтра", "/tomorrow"}))
async def show_tomorrow(message: types.Message):
    if not is_owner(message):
        return
    tomorrow_date = (datetime.now(TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        tasks = conn.execute(
            "SELECT id, title, deadline FROM tasks WHERE status = 'active' AND deadline LIKE ? ORDER BY deadline ASC",
            (f"{tomorrow_date}%",),
        ).fetchall()
        orders = conn.execute(
            "SELECT client, title, deadline FROM orders WHERE status = 'active' AND deadline LIKE ? ORDER BY deadline ASC",
            (f"{tomorrow_date}%",),
        ).fetchall()

    if not tasks and not orders:
        await message.answer(f"На завтра ({tomorrow_date}) пока пусто. Надиктуй задачи голосом!")
        return

    lines = [f"📅 **План на завтра ({(datetime.now(TZ) + timedelta(days=1)).strftime('%d.%m')}):**\n"]
    for c, t, dl in orders:
        tm = dl.split(" ")[1]
        lines.append(f"🔥 **[СДАЧА ЗАКАЗА]** `{tm}` — {c}: {t}")
    for tid, t, dl in tasks:
        tm = dl.split(" ")[1]
        lines.append(f"▫️ `{tm}` — {t}")

    await message.answer("\n".join(lines), parse_mode="Markdown")


@dp.message(F.text.in_({"📝 Все задачи", "/tasks"}))
async def show_all_tasks(message: types.Message):
    if not is_owner(message):
        return
    with sqlite3.connect(DB_PATH) as conn:
        tasks = conn.execute(
            "SELECT id, title, deadline FROM tasks WHERE status = 'active' ORDER BY deadline ASC"
        ).fetchall()

    if not tasks:
        await message.answer("Активных задач нет — всё закрыто!")
        return

    await message.answer(f"📝 **Список задач ({len(tasks)}):**", parse_mode="Markdown")
    for tid, title, dl in tasks:
        dl_fmt = datetime.strptime(dl, "%Y-%m-%d %H:%M").strftime("%d.%m в %H:%M") if dl else "—"
        await message.answer(
            f"▫️ **{title}**\n⏰ `{dl_fmt}`",
            reply_markup=task_inline_kb(tid),
            parse_mode="Markdown",
        )


@dp.message(F.text.in_({"💰 Касса", "/money"}))
async def show_money(message: types.Message):
    if not is_owner(message):
        return
    month_prefix = datetime.now(TZ).strftime("%Y-%m")
    with sqlite3.connect(DB_PATH) as conn:
        # Получено за текущий месяц (по дате создания/обновления)
        earned = conn.execute(
            "SELECT COALESCE(SUM(paid_amount), 0) FROM orders WHERE created_at LIKE ?",
            (f"{month_prefix}%",),
        ).fetchone()[0]

        # Ожидается доплат по активным заказам
        pending_debt = conn.execute(
            "SELECT COALESCE(SUM(total_amount - paid_amount), 0) FROM orders WHERE status = 'active'"
        ).fetchone()[0]

        active_count = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status = 'active'"
        ).fetchone()[0]

    pct = min(100, round((earned / max(MONTHLY_GOAL, 1)) * 100))
    filled = round(pct / 10)
    bar = "█" * filled + "░" * (10 - filled)

    text = (
        f"💰 **Финансы за {datetime.now(TZ).strftime('%m.%Y')}**\n\n"
        f"🎯 Цель: `{MONTHLY_GOAL:,} ₽`\n"
        f"💵 В кассе: `{earned:,} ₽` (**{pct}%**)\n"
        f"`[{bar}]`\n\n"
        f"⏳ Ожидается доплат по активным заказам: `{pending_debt:,} ₽`\n"
        f"📦 Заказов в работе: `{active_count}`"
    ).replace(",", " ")
    await message.answer(text, parse_mode="Markdown")


# ================= INLINE-КНОПКИ ДЕЙСТВИЙ =================
@dp.callback_query(F.data.startswith("ord_"))
async def handle_order_callbacks(call: types.CallbackQuery):
    action, oid_str = call.data.split(":")
    oid = int(oid_str)

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT client, total_videos, done_videos, total_amount, paid_amount FROM orders WHERE id = ?",
            (oid,),
        ).fetchone()
        if not row:
            await call.answer("Заказ не найден")
            return

        client, tot_v, done_v, tot_a, paid_a = row

        if action == "ord_vid":
            new_done = min(tot_v, done_v + 1)
            conn.execute("UPDATE orders SET done_videos = ? WHERE id = ?", (new_done, oid))
            await call.answer(f"Прогресс: {new_done}/{tot_v}")
            await call.message.edit_text(
                f"✅ Обновлено: **{client}** — готово `{new_done}/{tot_v}` роликов.",
                parse_mode="Markdown",
            )
        elif action == "ord_pay":
            conn.execute("UPDATE orders SET paid_amount = ? WHERE id = ?", (tot_a, oid))
            await call.answer("Оплата закрыта полностью!")
            await call.message.edit_text(
                f"💸 **{client}** закрыл остаток! Всего получено `{tot_a:,} ₽`.".replace(",", " "),
                parse_mode="Markdown",
            )
        elif action == "ord_done":
            conn.execute("UPDATE orders SET status = 'done', done_videos = ? WHERE id = ?", (tot_v, oid))
            await call.answer("Заказ перенесён в выполненные!")
            await call.message.edit_text(f"🎉 Заказ для **{client}** полностью сдан!", parse_mode="Markdown")


@dp.callback_query(F.data.startswith("tsk_done:"))
async def handle_task_done(call: types.CallbackQuery):
    tid = int(call.data.split(":")[1])
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
    await call.answer("Задача выполнена!")
    await call.message.edit_text("✅ Задача выполнена и убрана из списка.")


# ================= ПРИЁМ ГОЛОСА И ТЕКСТА =================
async def process_natural_input(message: types.Message, raw_text: str):
    status = await message.answer("⏳ Разбираю и записываю...")
    try:
        data = await parse_user_intent(raw_text)
        now_created = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
        dl = data.get("deadline")
        dl_pretty = datetime.strptime(dl, "%Y-%m-%d %H:%M").strftime("%d.%m.%Y в %H:%M") if dl else "не указан"

        with sqlite3.connect(DB_PATH) as conn:
            if data.get("type") == "order":
                client = data.get("client") or "Клиент"
                title = data.get("title") or "Монтаж Reels"
                tot_v = int(data.get("total_videos") or 1)
                tot_a = int(data.get("total_amount") or 0)
                paid_a = int(data.get("paid_amount") or 0)

                conn.execute(
                    "INSERT INTO orders (client, title, total_videos, total_amount, paid_amount, deadline, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (client, title, tot_v, tot_a, paid_a, dl, now_created),
                )
                reply = (
                    f"📦 **Записал новый заказ!**\n"
                    f"👤 Клиент: **{client}**\n"
                    f"🎬 Задача: {title} (`{tot_v}` шт.)\n"
                    f"💵 Сумма: `{tot_a:,} ₽` (предоплата: `{paid_a:,} ₽`)\n"
                    f"⏰ Дедлайн: `{dl_pretty}`\n\n"
                    f"🔔 Напомню за 12ч, 6ч, 3ч и 1ч."
                ).replace(",", " ")
            else:
                title = data.get("title") or raw_text
                conn.execute(
                    "INSERT INTO tasks (title, deadline, created_at) VALUES (?, ?, ?)",
                    (title, dl, now_created),
                )
                reply = (
                    f"📝 **Задача добавлена!**\n"
                    f"▫️ {title}\n"
                    f"⏰ Напомнить к: `{dl_pretty}`\n\n"
                    f"🔔 Пуши придут за 12ч, 6ч, 3ч и 1ч."
                )

        await status.edit_text(reply, parse_mode="Markdown")
    except Exception as e:
        await status.edit_text(f"Не удалось разобрать сообщение: {e}")


@dp.message(F.voice)
async def handle_voice(message: types.Message):
    if not is_owner(message):
        return
    os.makedirs("tmp_voice", exist_ok=True)
    ogg_path = f"tmp_voice/{message.voice.file_unique_id}.ogg"

    try:
        await bot.download(message.voice, destination=ogg_path)
        recognized = await asyncio.to_thread(transcribe_audio, ogg_path)
        if not recognized:
            await message.answer("Не удалось расслышать голос, попробуй ещё раз.")
            return
        await message.answer(f"🗣 *Распознал:* «{recognized}»", parse_mode="Markdown")
        await process_natural_input(message, recognized)
    finally:
        if os.path.exists(ogg_path):
            os.remove(ogg_path)


@dp.message(F.text)
async def handle_text(message: types.Message):
    if not is_owner(message):
        return
    await process_natural_input(message, message.text)


# ================= ПЛАНИРОВЩИК НАПОМИНАНИЙ (12ч / 6ч / 3ч / 1ч) =================
async def check_deadlines():
    if ALLOWED_USER_ID == 0:
        return

    now = datetime.now(TZ)
    thresholds = [
        (720, "rem_12h", "12 часов"),
        (360, "rem_6h", "6 часов"),
        (180, "rem_3h", "3 часа"),
        (60, "rem_1h", "1 час"),
    ]

    with sqlite3.connect(DB_PATH) as conn:
        # 1. Проверяем заказы
        orders = conn.execute(
            "SELECT id, client, title, done_videos, total_videos, deadline, rem_12h, rem_6h, rem_3h, rem_1h "
            "FROM orders WHERE status = 'active' AND deadline IS NOT NULL"
        ).fetchall()

        for row in orders:
            oid, client, title, done_v, tot_v, dl_str, r12, r6, r3, r1 = row
            dl = datetime.strptime(dl_str, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            mins_left = (dl - now).total_seconds() / 60.0
            if mins_left <= 0:
                continue

            flags = {"rem_12h": r12, "rem_6h": r6, "rem_3h": r3, "rem_1h": r1}
            for limit_mins, col_name, label in thresholds:
                if mins_left <= limit_mins and flags[col_name] == 0:
                    conn.execute(f"UPDATE orders SET {col_name} = 1 WHERE id = ?", (oid,))
                    # Отправляем уведомление только если мы попали в актуальное окно (не спамим прошедшими этапами)
                    prev_limit = {720: 360, 360: 180, 180: 60, 60: 0}[limit_mins]
                    if mins_left > prev_limit:
                        await bot.send_message(
                            ALLOWED_USER_ID,
                            f"🔥 **Дедлайн заказа через {label}!**\n"
                            f"👤 **{client}** — {title}\n"
                            f"🎬 Готово: `{done_v}/{tot_v}` | Сдать до `{dl.strftime('%H:%M')}`",
                            parse_mode="Markdown",
                        )

        # 2. Проверяем задачи
        tasks = conn.execute(
            "SELECT id, title, deadline, rem_12h, rem_6h, rem_3h, rem_1h "
            "FROM tasks WHERE status = 'active' AND deadline IS NOT NULL"
        ).fetchall()

        for row in tasks:
            tid, title, dl_str, r12, r6, r3, r1 = row
            dl = datetime.strptime(dl_str, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            mins_left = (dl - now).total_seconds() / 60.0
            if mins_left <= 0:
                continue

            flags = {"rem_12h": r12, "rem_6h": r6, "rem_3h": r3, "rem_1h": r1}
            for limit_mins, col_name, label in thresholds:
                if mins_left <= limit_mins and flags[col_name] == 0:
                    conn.execute(f"UPDATE tasks SET {col_name} = 1 WHERE id = ?", (tid,))
                    prev_limit = {720: 360, 360: 180, 180: 60, 60: 0}[limit_mins]
                    if mins_left > prev_limit:
                        await bot.send_message(
                            ALLOWED_USER_ID,
                            f"⏰ **Напоминание (осталось {label}):**\n"
                            f"▫️ {title}\n"
                            f"Срок: `{dl.strftime('%d.%m в %H:%M')}`",
                            parse_mode="Markdown",
                        )


async def main():
    init_db()
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(check_deadlines, "interval", seconds=60)
    scheduler.start()

    print("Личный трекер и напоминалка запущены!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
