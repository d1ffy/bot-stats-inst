import asyncio
import os
import re
from datetime import datetime
import requests
from dotenv import load_dotenv
import instaloader
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import FSInputFile
from fpdf import FPDF
from rapidocr_onnxruntime import RapidOCR

# Загружаем переменные из .env
load_dotenv()

TG_TOKEN = os.getenv("TG_TOKEN")
IG_USERNAME = os.getenv("IG_USERNAME", "d1ffy.reels")
PROXY_URL = os.getenv("PROXY_URL", "").strip()

FONT_REGULAR_URL = os.getenv(
    "FONT_REGULAR_URL",
    "https://github.com/google/fonts/raw/main/ofl/firasans/FiraSans-Regular.ttf",
)
FONT_BOLD_URL = os.getenv(
    "FONT_BOLD_URL",
    "https://github.com/google/fonts/raw/main/ofl/firasans/FiraSans-Bold.ttf",
)

FONT_REGULAR_PATH = os.path.join("fonts", "Regular.ttf")
FONT_BOLD_PATH = os.path.join("fonts", "Bold.ttf")

if not TG_TOKEN:
    raise ValueError("Не задан TG_TOKEN в файле .env")

bot = Bot(token=TG_TOKEN)
dp = Dispatcher()
ocr = RapidOCR()

user_sessions = {}
album_buffers = {}
album_tasks = {}


def ensure_fonts():
    """Скачивает шрифты из интернета в папку fonts/, если их ещё нет локально."""
    os.makedirs("fonts", exist_ok=True)
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

    for url, path in [(FONT_REGULAR_URL, FONT_REGULAR_PATH), (FONT_BOLD_URL, FONT_BOLD_PATH)]:
        if not os.path.exists(path):
            print(f"Скачиваю шрифт: {url} ...")
            resp = requests.get(url, proxies=proxies, timeout=20)
            resp.raise_for_status()
            with open(path, "wb") as f:
                f.write(resp.content)


def scrape_public_stats():
    """Шаг 1: Парсит публичную статистику профиля и последних Reels."""
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        save_metadata=False,
        compress_json=False,
    )

    if PROXY_URL:
        L.context._session.proxies = {
            "http": PROXY_URL,
            "https": PROXY_URL,
        }

    profile = instaloader.Profile.from_username(L.context, IG_USERNAME)

    acc_info = {
        "username": profile.username,
        "followers": profile.followers,
        "mediacount": profile.mediacount,
    }

    reels_data = []
    checked = 0
    for post in profile.get_posts():
        checked += 1
        if post.is_video:
            raw_cap = (post.caption or "Без описания").replace("\n", " ").strip()
            title = (raw_cap[:42] + "...") if len(raw_cap) > 42 else raw_cap
            reels_data.append({
                "date": post.date_local.strftime("%d.%m.%Y"),
                "title": title,
                "views": post.video_view_count or 0,
                "likes": post.likes or 0,
                "comments": post.comments or 0,
            })
        if len(reels_data) >= 8 or checked >= 20:
            break

    return acc_info, reels_data


def parse_metric_number(raw: str) -> int:
    s = raw.lower().replace("\xa0", " ").strip()
    mult = 1
    if "тыс" in s or "k" in s:
        mult = 1000
    elif "млн" in s or "m" in s:
        mult = 1_000_000

    num_match = re.search(r"(\d+(?:[.,]\d+)?)", s.replace(" ", ""))
    if not num_match:
        return 0
    try:
        return int(float(num_match.group(1).replace(",", ".")) * mult)
    except ValueError:
        return 0


def extract_private_metrics(image_path: str) -> dict:
    """Шаг 2: Вытаскивает со скриншотов закрытые метрики."""
    result, _ = ocr(image_path)
    if not result:
        return {}

    lines = [item[1].strip() for item in result if item[1].strip()]
    patterns = {
        "reach": [r"охваченные аккаунты", r"общий охват", r"охват", r"accounts reached", r"reach"],
        "profile_visits": [r"посещения профиля", r"визиты в профиль", r"действия в профиле", r"profile visits", r"profile activity"],
        "link_clicks": [r"нажатия на внешнюю ссылку", r"клики по ссылке", r"external link taps", r"website taps"],
        "shares": [r"репосты", r"пересылки", r"shares"],
        "saves": [r"сохранения", r"saved", r"saves"],
    }

    extracted = {}
    for i, line in enumerate(lines):
        low = line.lower()
        for key, kw_list in patterns.items():
            if key in extracted:
                continue
            if any(re.search(kw, low) for kw in kw_list):
                candidates = [line]
                if i + 1 < len(lines):
                    candidates.append(lines[i + 1])
                if i - 1 >= 0:
                    candidates.append(lines[i - 1])

                for cand in candidates:
                    if re.search(r"\d", cand):
                        val = parse_metric_number(cand)
                        if val > 0:
                            extracted[key] = val
                            break
    return extracted


class ReportPDF(FPDF):
    def footer(self):
        self.set_y(-14)
        self.set_font("MainFont", "", 8)
        self.set_text_color(142, 142, 147)
        self.cell(0, 10, f"Гибридный отчёт • @{IG_USERNAME}   |   Стр. {self.page_no()}", align="R")


def build_final_pdf(session: dict, filename: str):
    acc = session["acc"]
    reels = session["reels"]
    priv = session["private"]

    pdf = ReportPDF(orientation="P", unit="mm", format="A4")
    pdf.add_font("MainFont", "", FONT_REGULAR_PATH)
    pdf.add_font("MainFont", "B", FONT_BOLD_PATH)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    today = datetime.now().strftime("%d.%m.%Y")
    total_views = sum(r["views"] for r in reels)
    total_likes = sum(r["likes"] for r in reels)
    total_comments = sum(r["comments"] for r in reels)

    reach = priv.get("reach", 0)
    visits = priv.get("profile_visits", 0)
    clicks = priv.get("link_clicks", 0)
    shares = priv.get("shares", 0)
    saves = priv.get("saves", 0)

    # Шапка
    pdf.set_font("MainFont", "B", 20)
    pdf.set_text_color(29, 29, 31)
    pdf.cell(120, 10, f"Аналитика @{acc['username']}", ln=0)

    pdf.set_font("MainFont", "", 10)
    pdf.set_text_color(134, 134, 139)
    pdf.cell(0, 10, f"Дата: {today}", align="R", ln=1)

    pdf.set_font("MainFont", "", 10)
    pdf.cell(0, 5, "Объединённая сводка: публичные метрики Reels + внутренняя воронка профиля", ln=1)
    pdf.ln(6)

    # 4 карточки KPI
    cards = [
        ("ПОДПИСЧИКИ", f"{acc['followers']:,}".replace(",", " ")),
        ("ОХВАТ АККАУНТА", f"{reach:,}".replace(",", " ") if reach else "—"),
        ("ВИЗИТЫ В ПРОФИЛЬ", f"{visits:,}".replace(",", " ") if visits else "—"),
        ("КЛИКИ В TELEGRAM", f"{clicks:,}".replace(",", " ") if clicks else "—"),
    ]
    card_w = 44.5
    gap = 4
    y_cards = pdf.get_y()

    for idx, (label, val) in enumerate(cards):
        cx = 10 + idx * (card_w + gap)
        pdf.set_fill_color(245, 245, 247)
        pdf.rect(cx, y_cards, card_w, 22, style="F")

        pdf.set_xy(cx + 3, y_cards + 3)
        pdf.set_font("MainFont", "", 6.8)
        pdf.set_text_color(110, 110, 115)
        pdf.cell(card_w - 6, 4, label)

        pdf.set_xy(cx + 3, y_cards + 9)
        pdf.set_font("MainFont", "B", 14)
        pdf.set_text_color(29, 29, 31)
        pdf.cell(card_w - 6, 8, val)

    pdf.set_y(y_cards + 28)

    # Плашка воронки
    ctr_str = f"{round((clicks / visits) * 100, 1)}%" if (visits > 0 and clicks > 0) else "н/д"
    pdf.set_fill_color(240, 247, 255)
    pdf.rect(10, pdf.get_y(), 190, 12, style="F")
    pdf.set_xy(14, pdf.get_y() + 3)
    pdf.set_font("MainFont", "", 9.5)
    pdf.set_text_color(0, 85, 179)
    pdf.cell(
        0, 6,
        f"Конверсия визит -> клик в ТГ: {ctr_str}   |   Со скринов: {shares} репостов, {saves} сохранений"
    )
    pdf.ln(17)

    # Таблица спарсенных Reels
    pdf.set_font("MainFont", "B", 13)
    pdf.set_text_color(29, 29, 31)
    pdf.cell(0, 8, f"Спарсенная статистика последних {len(reels)} Reels", ln=1)
    pdf.ln(2)

    col_widths = [92, 28, 22, 24, 24]
    headers = ["Ролик", "Просмотры", "Лайки", "Комменты", "ER (от просм.)"]

    pdf.set_font("MainFont", "B", 8.5)
    pdf.set_text_color(134, 134, 139)
    for i, h in enumerate(headers):
        align = "L" if i == 0 else "R"
        pdf.cell(col_widths[i], 7, h, align=align)
    pdf.ln(7)
    pdf.set_draw_color(229, 229, 234)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())

    for r in reels:
        y_row = pdf.get_y() + 2
        er = round(((r["likes"] + r["comments"]) / max(r["views"], 1)) * 100, 1)

        pdf.set_xy(10, y_row)
        pdf.set_font("MainFont", "", 7.5)
        pdf.set_text_color(142, 142, 147)
        pdf.cell(col_widths[0], 4, r["date"], ln=2)

        pdf.set_font("MainFont", "B", 9)
        pdf.set_text_color(29, 29, 31)
        pdf.cell(col_widths[0], 5, r["title"])

        pdf.set_xy(10 + col_widths[0], y_row + 2)
        pdf.set_font("MainFont", "", 9.5)
        pdf.cell(col_widths[1], 6, f"{r['views']:,}".replace(",", " "), align="R")
        pdf.cell(col_widths[2], 6, str(r["likes"]), align="R")
        pdf.cell(col_widths[3], 6, str(r["comments"]), align="R")

        pdf.set_font("MainFont", "B", 9.5)
        pdf.set_text_color(0, 102, 204)
        pdf.cell(col_widths[4], 6, f"{er}%", align="R")

        pdf.set_y(y_row + 11)
        pdf.set_draw_color(242, 242, 247)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())

    pdf.ln(4)
    pdf.set_font("MainFont", "B", 9.5)
    pdf.set_text_color(29, 29, 31)
    pdf.cell(
        0, 6,
        f"Итого по {len(reels)} роликам: {total_views:,} просмотров • {total_likes} лайков • {total_comments} комментариев".replace(",", " "),
        align="R"
    )

    pdf.output(filename)


@dp.message(Command("start", "report"))
async def cmd_start_report(message: types.Message):
    user_id = message.from_user.id
    msg = await message.answer("⏳ Шаг 1/2: Парсю публичную статистику профиля и последних Reels...")

    try:
        acc_info, reels_data = await asyncio.to_thread(scrape_public_stats)
        user_sessions[user_id] = {
            "acc": acc_info,
            "reels": reels_data,
            "private": {},
        }

        total_v = sum(r["views"] for r in reels_data)
        prompt_text = (
            f"✅ **Публичные данные собраны!**\n"
            f"• Подписчики: `{acc_info['followers']}`\n"
            f"• Найдено Reels: `{len(reels_data)}` (суммарно `{total_v:,}` просм.)\n\n"
            f"📸 **Шаг 2/2: Кинь мне скриншоты того, что скрыто от парсера:**\n"
            f"1️⃣ **Профессиональная панель → Охваченные аккаунты** (общий охват и визиты в профиль)\n"
            f"2️⃣ **Профессиональная панель → Взаимодействия** (клики по ссылке в Telegram, репосты и сохранения)\n"
            f"3️⃣ *(По желанию)* **Статистика топового Reels** (его репосты и сохранения)\n\n"
            f"Кидай скрины прямо сюда (можно сразу альбомом) или нажми /skip, чтобы собрать PDF без них."
        )
        await msg.edit_text(prompt_text, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"Ошибка парсинга: {e}\nПроверь настройки VPN/DNS или ник `{IG_USERNAME}` в `.env`.")


@dp.message(Command("skip"))
async def cmd_skip(message: types.Message):
    user_id = message.from_user.id
    session = user_sessions.pop(user_id, None)
    if not session:
        await message.answer("Сначала запусти сбор командой /report")
        return

    pdf_path = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    await asyncio.to_thread(build_final_pdf, session, pdf_path)
    await message.answer_document(
        document=FSInputFile(pdf_path),
        caption="📊 Готово! Собрал PDF на основе спарсенных данных."
    )
    os.remove(pdf_path)


async def finalize_with_screenshots(user_id: int, chat_id: int):
    await asyncio.sleep(2.5)
    paths = album_buffers.pop(user_id, [])
    album_tasks.pop(user_id, None)

    session = user_sessions.pop(user_id, None)
    if not session:
        acc_info, reels_data = await asyncio.to_thread(scrape_public_stats)
        session = {"acc": acc_info, "reels": reels_data, "private": {}}

    status = await bot.send_message(chat_id, f"🔍 Распознаю {len(paths)} скрин(а) и склеиваю с данными парсера...")
    try:
        for p in paths:
            metrics = await asyncio.to_thread(extract_private_metrics, p)
            for k, v in metrics.items():
                session["private"][k] = max(session["private"].get(k, 0), v)

        pdf_path = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        await asyncio.to_thread(build_final_pdf, session, pdf_path)

        await bot.send_document(
            chat_id=chat_id,
            document=FSInputFile(pdf_path),
            caption="📊 Полный гибридный отчёт готов (парсер + данные со скриншотов)."
        )
        await status.delete()
        os.remove(pdf_path)
    except Exception as e:
        await status.edit_text(f"Ошибка при сборке отчёта: {e}")
    finally:
        for p in paths:
            if os.path.exists(p):
                os.remove(p)


@dp.message(F.photo)
async def handle_screenshots(message: types.Message):
    user_id = message.from_user.id
    photo = message.photo[-1]

    os.makedirs("tmp_screens", exist_ok=True)
    img_path = f"tmp_screens/{user_id}_{photo.file_unique_id}.jpg"
    await bot.download(photo, destination=img_path)

    album_buffers.setdefault(user_id, []).append(img_path)
    if user_id in album_tasks:
        album_tasks[user_id].cancel()
    album_tasks[user_id] = asyncio.create_task(finalize_with_screenshots(user_id, message.chat.id))


async def main():
    ensure_fonts()
    print(f"Гибридный бот для @{IG_USERNAME} запущен! Напиши /report в Telegram.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())