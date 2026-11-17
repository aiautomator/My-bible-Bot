import os
import re
import time
import urllib.parse
from threading import Thread
import requests
from bs4 import BeautifulSoup
from flask import Flask
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ==========================================
# 1. CONFIGURATION & INITIALIZATION
# ==========================================
# Replace with your actual BotFather token or set it in Render Environment Variables
BOT_TOKEN  = "8825313159:AAFfZDCqJ79dyEg9dhok8c5s-WuJIHARUKc"
bot = telebot.TeleBot(BOT_TOKEN)

SUPPORTED_VERSIONS = ["KJV", "NKJV", "NIV", "NLT", "ESV", "AMP", "MSG", "TPT"]

# In-memory storage for user sessions, active passages, and search pagination
user_state = {}
user_search_results = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# ==========================================
# 2. RENDER KEEP-ALIVE WEB SERVER
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bible Bot is alive and running!"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    server_thread = Thread(target=run_web_server)
    server_thread.daemon = True
    server_thread.start()

# ==========================================
# 3. USER ACTIVITY LOGGING
# ==========================================
def log_activity(user, action):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    username = f"@{user.username}" if user.username else "NoUsername"
    log_entry = f"[{timestamp}] ID: {user.id} | Name: {user.first_name} ({username}) | Action: {action}"
    print(log_entry)
    try:
        with open("user_activity.log", "a", encoding="utf-8") as f:
            f.write(log_entry + "\n")
    except Exception as e:
        print(f"Log Error: {e}")

# ==========================================
# 4. BEAUTIFULSOUP SCRAPING FUNCTIONS
# ==========================================
def fetch_passage(reference, version="KJV"):
    encoded_ref = urllib.parse.quote_plus(reference)
    url = f"https://www.biblegateway.com/passage/?search={encoded_ref}&version={version}"

    try:
        response = requests.get(url, headers=HEADERS, timeout=12)
        if response.status_code != 200:
            return None, None

        soup = BeautifulSoup(response.content, "html.parser")
        passage_box = soup.find("div", class_="passage-text")
        if not passage_box:
            return None, None

        # Extract clean heading title
        heading_tag = soup.find("div", class_="dropdown-display-text")
        clean_title = heading_tag.get_text(strip=True) if heading_tag else reference.title()

        # Remove footnotes, cross-references, and chapter numbers that clutter output
        for unwanted in passage_box.find_all(["sup", "div", "h1", "h2", "h3", "h4"], class_=["footnote", "crossreference", "footnotes", "crossrefs", "passage-other-trans"]):
            unwanted.decompose()

        for sup in passage_box.find_all("sup", class_="versenum"):
            sup.replace_with(f" [{sup.get_text(strip=True)}] ")

        for chap in passage_box.find_all("span", class_="chapternum"):
            chap.replace_with("[1] ")

        text_paragraphs = []
        for p in passage_box.find_all("p"):
            para_text = p.get_text(" ", strip=True)
            if para_text:
                text_paragraphs.append(para_text)

        full_text = "\n\n".join(text_paragraphs)
        full_text = re.sub(r'\s+', ' ', full_text).replace(" \n\n ", "\n\n")

        if not full_text:
            return None, None

        return clean_title, full_text[:3800]  # Keep within Telegram's 4096-character limit
    except Exception as e:
        print(f"Passage Scraping Error: {e}")
        return None, None

def search_bible_word(query, version="KJV"):
    encoded_query = urllib.parse.quote_plus(query)
    url = f"https://www.biblegateway.com/quicksearch/?quicksearch={encoded_query}&version={version}&searchtype=keyword"

    try:
        response = requests.get(url, headers=HEADERS, timeout=12)
        if response.status_code != 200:
            return []

        soup = BeautifulSoup(response.content, "html.parser")
        results = []
        passage_links = soup.find_all("a", href=lambda href: href and "/passage/?search=" in href)

        seen_refs = set()
        for link in passage_links:
            reference = link.text.strip()
            if not reference or reference in seen_refs or "Next" in reference or "Context" in reference or "Full Chapter" in reference:
                continue
            seen_refs.add(reference)

            parent = link.find_parent("li") or link.find_parent("div", class_="bible-item") or link.find_parent("div")
            if parent:
                raw_snippet = parent.get_text(separator=" ", strip=True)
                snippet = raw_snippet.replace(reference, "").replace("In Context", "").replace("Full Chapter", "").replace("|", "").strip()
                results.append(f"📖 *{reference}*\n_{snippet}_")

        return results
    except Exception as e:
        print(f"Search Error: {e}")
        return []

# ==========================================
# 5. INLINE KEYBOARDS & HELPERS
# ==========================================
def adjust_chapter(reference, delta):
    """Increments or decrements the chapter number for Prev/Next navigation."""
    match = re.match(r"^([1-3]?\s?[A-Za-z\s]+?)\s+(\d+)(?::.*)?$", reference.strip())
    if match:
        book = match.group(1).strip()
        chapter = max(1, int(match.group(2)) + delta)
        return f"{book} {chapter}"
    return reference

def build_passage_keyboard(current_version):
    markup = InlineKeyboardMarkup()

    # Row 1: Chapter Navigation
    markup.row(
        InlineKeyboardButton("⬅️ Prev Chapter", callback_data="nav_prev"),
        InlineKeyboardButton("Next Chapter ➡️", callback_data="nav_next")
    )

    # Row 2 & 3: 8 Translation Switchers (4 buttons per row)
    row1, row2 = [], []
    for idx, ver in enumerate(SUPPORTED_VERSIONS):
        label = f"✅ {ver}" if ver == current_version else ver
        btn = InlineKeyboardButton(label, callback_data=f"ver_{ver}")
        if idx < 4:
            row1.append(btn)
        else:
            row2.append(btn)

    markup.row(*row1)
    markup.row(*row2)
    return markup

def send_search_page(chat_id, message_id=None):
    data = user_search_results.get(chat_id)
    if not data:
        return

    page = data["page"]
    results = data["results"]
    version = data.get("version", "KJV")

    start = page * 5
    end = start + 5
    page_results = results[start:end]

    text = (
        f"🔍 *Search:* '{data['query']}' ({version})\n"
        f"*Showing {start + 1} to {min(end, len(results))} of {len(results)} matches*\n\n"
    )
    text += "\n\n".join(page_results)

    markup = InlineKeyboardMarkup()
    nav_buttons = []

    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev 5", callback_data="search_prev"))
    if end < len(results):
        nav_buttons.append(InlineKeyboardButton("Next 5 ➡️", callback_data="search_next"))

    if nav_buttons:
        markup.row(*nav_buttons)

    if message_id:
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup, parse_mode="Markdown")
    else:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")

# ==========================================
# 6. TELEGRAM BOT HANDLERS (Private Chat Filter)
# ==========================================
@bot.message_handler(commands=["start", "help"], chat_types=["private"])
def handle_start(message):
    log_activity(message.from_user, "/start")
    welcome_text = (
        "🙏 *Welcome to the Multi-Version Bible Bot!*\n\n"
        "📖 *Read Scriptures:*\n"
        "Simply type any Bible reference (e.g., `John 3:16`, `Psalm 23`, or `Romans 8:28-31`).\n\n"
        "🔍 *Keyword & Phrase Search:*\n"
        "Use `/search <words>` to find verses matching a collection of words!\n"
        "Example: `/search peace in the storm` or `/search faith hope love`\n\n"
        "🌐 *Supported Translations:*\n"
        "`KJV`, `NKJV`, `NIV`, `NLT`, `ESV`, `AMP`, `MSG`, `TPT`"
    )
    bot.reply_to(message, welcome_text, parse_mode="Markdown")

@bot.message_handler(commands=["search"], chat_types=["private"])
def handle_search(message):
    query = message.text.replace("/search", "", 1).strip()
    chat_id = message.chat.id
    log_activity(message.from_user, f"/search {query}")

    if not query:
        bot.reply_to(
            message,
            "Please provide a word or phrase to search!\nExample: `/search faith hope love`",
            parse_mode="Markdown"
        )
        return

    version = user_state.get(chat_id, {}).get("version", "KJV")
    status_msg = bot.reply_to(message, f"🔍 Searching *{version}* scriptures for *'{query}'*...", parse_mode="Markdown")

    results_list = search_bible_word(query, version=version)

    if not results_list:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text=f"❌ No results found for *'{query}'*. Try different keywords.",
            parse_mode="Markdown"
        )
        return

    user_search_results[chat_id] = {
        "query": query,
        "results": results_list,
        "page": 0,
        "version": version
    }

    send_search_page(chat_id, message_id=status_msg.message_id)

@bot.message_handler(func=lambda m: True, chat_types=["private"])
def handle_passage_request(message):
    chat_id = message.chat.id
    reference = message.text.strip()
    log_activity(message.from_user, f"Passage: {reference}")

    current_version = user_state.get(chat_id, {}).get("version", "KJV")
    status_msg = bot.reply_to(message, f"📖 Fetching *{reference}* ({current_version})...", parse_mode="Markdown")

    clean_title, passage_text = fetch_passage(reference, current_version)

    if not passage_text:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text="❌ Could not find that passage. Check your spelling (e.g., `John 3:16`) or try `/search <words>`.",
            parse_mode="Markdown"
        )
        return

    user_state[chat_id] = {
        "reference": clean_title,
        "version": current_version
    }

    response_text = f"📖 *{clean_title} ({current_version})*\n\n{passage_text}"
    markup = build_passage_keyboard(current_version)

    bot.edit_message_text(
        chat_id=chat_id,
        message_id=status_msg.message_id,
        text=response_text,
        reply_markup=markup,
        parse_mode="Markdown"
    )

# ==========================================
# 7. CALLBACK QUERY HANDLERS (BUTTON CLICKS)
# ==========================================
@bot.callback_query_handler(func=lambda call: call.data in ["search_next", "search_prev"])
def handle_search_pagination(call):
    chat_id = call.message.chat.id

    if chat_id not in user_search_results:
        bot.answer_callback_query(call.id, "Search session expired. Please run /search again.")
        return

    if call.data == "search_next":
        user_search_results[chat_id]["page"] += 1
    elif call.data == "search_prev":
        user_search_results[chat_id]["page"] = max(0, user_search_results[chat_id]["page"] - 1)

    send_search_page(chat_id, call.message.message_id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith(("ver_", "nav_")))
def handle_passage_buttons(call):
    chat_id = call.message.chat.id
    state = user_state.get(chat_id)

    if not state:
        bot.answer_callback_query(call.id, "Session expired. Please type a Bible verse again.")
        return

    reference = state["reference"]
    version = state["version"]

    if call.data.startswith("ver_"):
        version = call.data.split("_")[1]
    elif call.data == "nav_prev":
        reference = adjust_chapter(reference, -1)
    elif call.data == "nav_next":
        reference = adjust_chapter(reference, 1)

    bot.answer_callback_query(call.id, f"Loading {reference} ({version})...")

    clean_title, passage_text = fetch_passage(reference, version)
    if not passage_text:
        bot.answer_callback_query(call.id, "Could not load that chapter or translation.")
        return

    user_state[chat_id] = {
        "reference": clean_title,
        "version": version
    }

    response_text = f"📖 *{clean_title} ({version})*\n\n{passage_text}"
    markup = build_passage_keyboard(version)

    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=response_text,
            reply_markup=markup,
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Edit Message Notice: {e}")

# ==========================================
# 8. START BOT & SERVER
# ==========================================
if __name__ == "__main__":
    print("Starting Render Keep-Alive Web Server...")
    keep_alive()
    print("Bible Bot is polling...")
    bot.infinity_polling(skip_pending=True)
