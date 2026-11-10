import re
import urllib.parse
import requests
from uuid import uuid4
from bs4 import BeautifulSoup
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    filters,
    ContextTypes,
)
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# Reads token from cloud settings, or falls back to your local token
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")

# Supported Bible Translations (Code -> Full Name)
TRANSLATIONS = {
    "KJV": "King James Version",
    "NIV": "New International Version",
    "NLT": "New Living Translation",
    "ESV": "English Standard Version",
    "NKJV": "New King James Version",
    "AMP": "Amplified Bible",
    "MSG": "The Message",
    "TPT": "The Passion Translation",
}

# Remembers each user's active Bible translation
user_translations = {}


def parse_reference_details(ref_str: str):
    """Extracts Book, Chapter, First Verse, and Last Verse for Prev/Next navigation"""
    match = re.match(r"^(\d?\s*[A-Za-z\s]+?)\s+(\d+)(?::(\d+)(?:-(\d+))?)?$", ref_str.strip())
    if match:
        book = match.group(1).strip()
        chapter = int(match.group(2))
        first_v = int(match.group(3)) if match.group(3) else 1
        last_v = int(match.group(4)) if match.group(4) else first_v
        return book, chapter, first_v, last_v
    return None, None, None, None


def fetch_passage_data(reference: str, translation: str = "NIV"):
    """Fetches any Bible translation (TPT, MSG, AMP, NIV, NLT, KJV, etc.) from BibleGateway"""
    translation = translation.upper()
    encoded_ref = urllib.parse.quote(reference)
    url = f"https://www.biblegateway.com/passage/?search={encoded_ref}&version={translation}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        passage_div = soup.find("div", class_="passage-text")
        if not passage_div:
            return None

        # Get official Reference Title (e.g. "John 3:16" or "Psalm 23")
        bcv = soup.find("div", class_="dropdown-display-text")
        clean_ref = bcv.get_text(strip=True) if bcv else reference.title()

        # Remove footnotes, cross-references, and section headings so text is clean
        for junk in passage_div.select(".footnotes, .crossrefs, .passage-other-trans, .full-chap-link, h3, h4, sup.footnote, sup.crossreference"):
            junk.decompose()

        # Format verse numbers cleanly like [16]
        for vnum in passage_div.find_all("sup", class_="versenum"):
            num_text = vnum.get_text(strip=True)
            vnum.replace_with(f" [{num_text}] ")

        for cnum in passage_div.find_all("span", class_="chapternum"):
            cnum.replace_with("[1] ")

        paragraphs = []
        for p in passage_div.find_all("p"):
            text = " ".join(p.get_text().split())
            if text:
                paragraphs.append(text)

        full_text = "\n\n".join(paragraphs)
        if not full_text:
            return None

        # Keep inside Telegram's 4096-character limit
        if len(full_text) > 3700:
            full_text = full_text[:3700] + "...\n\n*(Passage truncated for length)*"

        trans_full = TRANSLATIONS.get(translation, translation)
        message_text = f"📖 *{clean_ref} ({translation})*\n_{trans_full}_\n\n{full_text}"

        book, chapter, first_v, last_v = parse_reference_details(clean_ref)

        return {
            "text": message_text,
            "book": book,
            "chapter": chapter,
            "first_verse": first_v,
            "last_verse": last_v,
            "reference": clean_ref,
            "translation": translation,
        }
    except Exception:
        return None


def build_action_keyboard(info: dict) -> InlineKeyboardMarkup:
    """Builds Navigation row + 2 rows of Translation buttons (including TPT, MSG, AMP)"""
    book = info["book"]
    chapter = info["chapter"]
    first_v = info["first_verse"]
    last_v = info["last_verse"]
    trans = info["translation"].upper()
    ref = info["reference"]

    keyboard = []

    # Row 1: Prev | Chapter | Next (if Book & Chapter were parsed)
    if book and chapter:
        nav_row = []
        if first_v and first_v > 1:
            prev_ref = f"{book} {chapter}:{first_v - 1}"
            nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"v|{prev_ref}|{trans}"))

        chapter_ref = f"{book} {chapter}"
        nav_row.append(InlineKeyboardButton("📖 Chapter", callback_data=f"v|{chapter_ref}|{trans}"))

        if last_v:
            next_ref = f"{book} {chapter}:{last_v + 1}"
            nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"v|{next_ref}|{trans}"))

        keyboard.append(nav_row)

    # Row 2: Classic & Modern Standard Versions
    row_a_versions = ["KJV", "NKJV", "NIV", "NLT"]
    row_a = [
        InlineKeyboardButton(
            f"✅ {v}" if v == trans else v,
            callback_data=f"v|{ref}|{v}"
        )
        for v in row_a_versions
    ]
    keyboard.append(row_a)

    # Row 3: Expressive & Study Versions (ESV, AMP, MSG, TPT)
    row_b_versions = ["ESV", "AMP", "MSG", "TPT"]
    row_b = [
        InlineKeyboardButton(
            f"✅ {v}" if v == trans else v,
            callback_data=f"v|{ref}|{v}"
        )
        for v in row_b_versions
    ]
    keyboard.append(row_b)

    return InlineKeyboardMarkup(keyboard)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "👋 *Welcome to Your Multi-Version Bible Bot!*\n\n"
        "Send any reference like `John 3:16` or `Psalm 23:1-4`.\n"
        "You can also include a version code at the end, like:\n"
        "• `John 3:16 TPT`\n"
        "• `Romans 8:28 MSG`\n"
        "• `Philippians 4:6-7 AMP`\n\n"
        "Or tap any of the *8 version buttons* below a verse to switch instantly!"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def handle_verse_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    default_trans = user_translations.get(user_id, "NIV")

    raw_query = " ".join(context.args) if context.args else update.message.text.strip()
    if not raw_query:
        await update.message.reply_text("Try sending a verse like `John 3:16 TPT`", parse_mode="Markdown")
        return

    # Check if the user typed a version at the end (e.g., "John 3:16 TPT" or "Psalm 23 MSG")
    words = raw_query.split()
    if len(words) > 1 and words[-1].upper() in TRANSLATIONS:
        translation = words[-1].upper()
        query = " ".join(words[:-1])
        user_translations[user_id] = translation
    else:
        translation = default_trans
        query = raw_query

    info = fetch_passage_data(query, translation)
    if not info:
        await update.message.reply_text(
            "❌ Verse not found. Try a format like `John 3:16`, `Rom 8:28 TPT`, or `Psalm 23 MSG`.",
            parse_mode="Markdown",
        )
        return

    keyboard = build_action_keyboard(info)
    await update.message.reply_text(info["text"], parse_mode="Markdown", reply_markup=keyboard)


async def handle_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")
    if len(parts) != 3:
        return

    _, ref, trans = parts
    user_translations[query.from_user.id] = trans

    info = fetch_passage_data(ref, trans)
    if not info:
        await query.answer("⚠️ Verse not available in this translation or end of chapter reached.", show_alert=True)
        return

    keyboard = build_action_keyboard(info)
    try:
        await query.edit_message_text(
            text=info["text"], parse_mode="Markdown", reply_markup=keyboard
        )
    except Exception:
        pass


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_query = update.inline_query.query.strip()
    if not raw_query:
        return

    user_id = update.inline_query.from_user.id
    default_trans = user_translations.get(user_id, "NIV")

    words = raw_query.split()
    if len(words) > 1 and words[-1].upper() in TRANSLATIONS:
        translation = words[-1].upper()
        query_str = " ".join(words[:-1])
    else:
        translation = default_trans
        query_str = raw_query

    info = fetch_passage_data(query_str, translation)
    if not info:
        return

    clean_preview = info["text"].split("\n\n", 1)[-1][:95]
    results = [
        InlineQueryResultArticle(
            id=str(uuid4()),
            title=f"📖 {info['reference']} ({info['translation']})",
            description=clean_preview,
            input_message_content=InputTextMessageContent(info["text"], parse_mode="Markdown"),
            reply_markup=build_action_keyboard(info),
        )
    ]
    await update.inline_query.answer(results, cache_time=60)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bible Bot is alive!")

def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()
if __name__ == "__main__":
    print("Multi-Version Bible Bot (with TPT, MSG, AMP) is running! Press Ctrl+C to stop.")
    threading.Thread(target=start_health_server, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("get", handle_verse_request))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_verse_request))
    app.add_handler(CallbackQueryHandler(handle_button_click))
    app.add_handler(InlineQueryHandler(inline_query))
    app.run_polling()
