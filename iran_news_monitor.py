#!/usr/bin/env python3
"""
Iran News Monitor
------------------
هر روز صبح ۱۵ منبع خبری/تحلیلی معتبر جهان (روزنامه، سایت خبری، اندیشکده) رو
چک می‌کنه، هر آیتمی (خبر/یادداشت/مقاله/تحلیل) که درباره‌ی ایران باشه رو پیدا
می‌کنه و لینکش رو از طریق یک بات تلگرام می‌فرسته.

نحوه‌ی اجرا:
    export TELEGRAM_BOT_TOKEN="..."
    export TELEGRAM_CHAT_ID="..."
    python iran_news_monitor.py

این اسکریپت برای اجرای خودکار روزانه در GitHub Actions طراحی شده
(فایل .github/workflows/daily-iran-news.yml رو ببین).
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from deep_translator import GoogleTranslator

SEEN_FILE = "seen_links.json"
MAX_SEEN_ENTRIES = 2000  # جلوگیری از بزرگ‌شدن بی‌نهایت فایل حافظه

# ---------------------------------------------------------------------------
# ۱) منابع خبری. هر منبع یک لینک RSS داره.
#    این‌ها معتبرترین/پرکاربردترین فیدهایی هستن که در دسترس عمومی‌اند.
#    اگر یکی از این آدرس‌ها در آینده تغییر کرد یا از کار افتاد، اسکریپت
#    فقط همون یک منبع رو رد می‌کنه (skip) و بقیه رو اجرا می‌کنه.
# ---------------------------------------------------------------------------
FEEDS = [
    # --- رسانه‌های خبری بین‌المللی ---
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("The Guardian - World", "https://www.theguardian.com/world/rss"),
    ("New York Times - World", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
    ("Reuters - World", "https://reutersbest.com/region/global/feed/"),
    ("Axios - World", "https://www.axios.com/feed/world"),
    ("Times of Israel", "https://www.timesofisrael.com/feed/"),
    # --- رسانه‌های تخصصی خاورمیانه ---
    ("Al-Monitor", "https://www.al-monitor.com/rss"),
    ("Middle East Eye", "https://www.middleeasteye.net/rss"),
    # --- مجلات و اندیشکده‌های سیاست خارجی ---
    ("Foreign Affairs", "https://www.foreignaffairs.com/rss.xml"),
    ("Foreign Policy", "https://foreignpolicy.com/feed/"),
    ("Council on Foreign Relations (Middle East)", "https://feeds.cfr.org/region/middle_east"),
    ("Brookings", "https://www.brookings.edu/feed/"),
    ("Atlantic Council", "https://www.atlanticcouncil.org/feed/"),
    ("Institute for the Study of War (ISW)", "https://www.understandingwar.org/rss.xml"),
]

# ---------------------------------------------------------------------------
# ۲) کلیدواژه‌هایی که برای تشخیص «مرتبط با ایران» چک می‌شن.
#    می‌تونی این لیست رو هر وقت خواستی گسترش بدی (اسم افراد خاص، اصطلاحات و...).
# ---------------------------------------------------------------------------
KEYWORDS = [
    r"\biran\b", r"\biranian[s]?\b", r"\btehran\b", r"\birgc\b",
    r"\bislamic republic\b", r"\bkhamenei\b", r"\bpezeshkian\b",
    r"\bayatollah\b", r"\bpersian gulf\b", r"\bjcpoa\b",
]
KEYWORD_PATTERN = re.compile("|".join(KEYWORDS), re.IGNORECASE)

# فقط خبرهایی که در این بازه‌ی زمانی منتشر شدن در نظر گرفته می‌شن.
# چون هر ساعت اجرا می‌شه، بازه رو کوتاه‌تر گذاشتیم (با کمی همپوشانی برای اطمینان).
# جلوگیری از ارسال تکراری واقعی رو فایل seen_links.json انجام می‌ده.
LOOKBACK_HOURS = 6


def load_seen_links():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen_links(seen_links):
    # فقط جدیدترین‌ها رو نگه می‌داریم تا فایل بی‌نهایت بزرگ نشه
    trimmed = list(seen_links)[-MAX_SEEN_ENTRIES:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False, indent=2)


def is_recent(entry, cutoff):
    """بررسی می‌کنه که آیا این آیتم در بازه‌ی زمانی موردنظر منتشر شده یا نه."""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            published = datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
            return published >= cutoff
    # اگر فید تاریخ نداشت، محتاطانه برخورد کن و در نظرش بگیر
    return True


def translate_to_persian(text):
    try:
        return GoogleTranslator(source="auto", target="fa").translate(text)
    except Exception as e:
        print(f"⚠️  ترجمه ناموفق بود، عنوان اصلی نگه داشته شد: {e}", file=sys.stderr)
        return text


def matches_iran(entry):
    text = f"{entry.get('title', '')} {entry.get('summary', '')}"
    return bool(KEYWORD_PATTERN.search(text))


def collect_matches(already_sent):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    matches = []
    seen_links = set()

    for source_name, url in FEEDS:
        try:
            feed = feedparser.parse(url)
            if feed.bozo and not feed.entries:
                print(f"⚠️  خطا در خوندن فید {source_name}: {feed.bozo_exception}", file=sys.stderr)
                continue
        except Exception as e:
            print(f"⚠️  خطا در خوندن فید {source_name}: {e}", file=sys.stderr)
            continue

        for entry in feed.entries:
            link = entry.get("link", "")
            if not link or link in seen_links or link in already_sent:
                continue
            if not is_recent(entry, cutoff):
                continue
            if matches_iran(entry):
                original_title = entry.get("title", "بدون عنوان").strip()
                matches.append({
                    "source": source_name,
                    "title": translate_to_persian(original_title),
                    "original_title": original_title,
                    "link": link,
                })
                seen_links.add(link)

    return matches


def format_message(matches):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not matches:
        return f"📰 *گزارش روزانه‌ی ایران* — {today}\n\nامروز مورد مرتبطی در ۱۵ منبع پیدا نشد."

    lines = [f"📰 *گزارش روزانه‌ی ایران* — {today}", f"({len(matches)} مورد)", ""]
    for i, m in enumerate(matches, 1):
        lines.append(f"{i}. *{m['source']}*\n{m['title']}\n{m['link']}\n")
    return "\n".join(lines)


def send_telegram(text, token, chat_ids):
    # تلگرام محدودیت ۴۰۹۶ کاراکتر روی هر پیام داره؛ در صورت نیاز تکه‌تکه می‌فرستیم
    MAX_LEN = 4000
    chunks = [text[i:i + MAX_LEN] for i in range(0, len(text), MAX_LEN)] or [text]

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in chat_ids:
        for chunk in chunks:
            resp = requests.post(url, data={
                "chat_id": chat_id.strip(),
                "text": chunk,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            })
            if not resp.ok:
                print(f"⚠️  ارسال پیام تلگرام به {chat_id} ناموفق بود: {resp.status_code} {resp.text}", file=sys.stderr)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id_raw:
        print("❌ TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID باید به‌عنوان متغیر محیطی ست بشن.", file=sys.stderr)
        sys.exit(1)

    # می‌تونی چند تا chat id رو با کاما از هم جدا کنی، مثلاً: "111111,222222,333333"
    chat_ids = [c for c in chat_id_raw.split(",") if c.strip()]

    already_sent = load_seen_links()
    matches = collect_matches(already_sent)

    # فقط وقتی خبر جدیدی پیدا شده پیام بفرست (برای اجرای ساعتی، پیام "خبری نبود" لازم نیست)
    if matches:
        message = format_message(matches)
        send_telegram(message, token, chat_ids)

    for m in matches:
        already_sent.add(m["link"])
    save_seen_links(already_sent)

    print(f"✅ انجام شد — {len(matches)} مورد جدید پیدا و ارسال شد.")


if __name__ == "__main__":
    main()
