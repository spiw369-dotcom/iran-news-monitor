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

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests

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

# فقط خبرهایی که در این بازه‌ی زمانی منتشر شدن در نظر گرفته می‌شن
# (برای جلوگیری از ارسال تکراری خبرهای قدیمی هر روز)
LOOKBACK_HOURS = 30


def is_recent(entry, cutoff):
    """بررسی می‌کنه که آیا این آیتم در بازه‌ی زمانی موردنظر منتشر شده یا نه."""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            published = datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
            return published >= cutoff
    # اگر فید تاریخ نداشت، محتاطانه برخورد کن و در نظرش بگیر
    return True


def matches_iran(entry):
    text = f"{entry.get('title', '')} {entry.get('summary', '')}"
    return bool(KEYWORD_PATTERN.search(text))


def collect_matches():
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
            if not link or link in seen_links:
                continue
            if not is_recent(entry, cutoff):
                continue
            if matches_iran(entry):
                matches.append({
                    "source": source_name,
                    "title": entry.get("title", "بدون عنوان").strip(),
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


def send_telegram(text, token, chat_id):
    # تلگرام محدودیت ۴۰۹۶ کاراکتر روی هر پیام داره؛ در صورت نیاز تکه‌تکه می‌فرستیم
    MAX_LEN = 4000
    chunks = [text[i:i + MAX_LEN] for i in range(0, len(text), MAX_LEN)] or [text]

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in chunks:
        resp = requests.post(url, data={
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        })
        if not resp.ok:
            print(f"⚠️  ارسال پیام تلگرام ناموفق بود: {resp.status_code} {resp.text}", file=sys.stderr)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("❌ TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID باید به‌عنوان متغیر محیطی ست بشن.", file=sys.stderr)
        sys.exit(1)

    matches = collect_matches()
    message = format_message(matches)
    send_telegram(message, token, chat_id)
    print(f"✅ انجام شد — {len(matches)} مورد پیدا و ارسال شد.")


if __name__ == "__main__":
    main()
