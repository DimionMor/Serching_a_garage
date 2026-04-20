#!/usr/bin/env python3
"""
Авито Гараж Бот — Санкт-Петербург
Парсит RSS Авито, фильтрует гаражи по параметрам, отправляет топ-5 в Telegram.
Запуск: через день, начиная с 18 апреля 2026 года.
"""

import json
import logging
import re
import time
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import requests
import schedule

# ── Логирование ──────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

BASE_DIR     = Path(__file__).parent
CONFIG_PATH  = BASE_DIR / "config.json"
SEEN_PATH    = BASE_DIR / "seen_ids.json"
HISTORY_PATH = BASE_DIR / "price_history.json"

# ════════════════════════════════════════════════════════════════════════
# Конфиг и хранилища
# ════════════════════════════════════════════════════════════════════════

def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

def load_seen():
    if SEEN_PATH.exists():
        with open(SEEN_PATH, encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(list(seen)[-5000:], f, ensure_ascii=False)

def load_price_history():
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_price_history(history):
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

# ════════════════════════════════════════════════════════════════════════
# Расписание: через день с 18.04.2026
# ════════════════════════════════════════════════════════════════════════

SCHEDULE_START = date(2026, 4, 18)

def is_active_day():
    today = date.today()
    delta = (today - SCHEDULE_START).days
    return delta >= 0 and delta % 2 == 0

# ════════════════════════════════════════════════════════════════════════
# RSS URL
# ════════════════════════════════════════════════════════════════════════

def build_rss_url(cfg):
    city = cfg["city"]
    pmax = cfg["price_max_capital"]
    params = {
        "s":        104,
        "owner[]":  "private",
        "pmax":     pmax,
        "format":   "rss",
    }
    if cfg.get("area_min"):
        params["minArea"] = cfg["area_min"]
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return f"https://www.avito.ru/{city}/garazhi_i_mashinomesta?{qs}"

# ════════════════════════════════════════════════════════════════════════
# Парсинг RSS
# ════════════════════════════════════════════════════════════════════════

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

def fetch_listings(rss_url):
    try:
        resp = requests.get(rss_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Ошибка загрузки RSS: %s", e)
        return []
    feed = feedparser.parse(resp.content)
    listings = []
    for entry in feed.entries:
        full_text = (entry.get("title","") + " " + entry.get("summary","")).lower()
        price    = extract_price(entry.get("title",""))
        area     = extract_area(full_text)
        pub_date = None
        if entry.get("published"):
            try:
                pub_date = parsedate_to_datetime(entry["published"])
            except Exception:
                pass
        listings.append({
            "id":       entry.get("id", entry.get("link","")),
            "title":    entry.get("title","Без названия"),
            "link":     entry.get("link",""),
            "price":    price,
            "area":     area,
            "pub_date": pub_date,
            "text":     full_text,
        })
    log.info("Загружено из RSS: %d объявлений", len(listings))
    return listings

def extract_price(text):
    m = re.search(r"([\d\s]+)\s*[₽р]", text)
    if m:
        return int(re.sub(r"\s","", m.group(1)))
    return None

def extract_area(text):
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:м²|кв\.?\s*м|m2)", text, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",","."))
    return None

# ════════════════════════════════════════════════════════════════════════
# Ключевые слова
# ════════════════════════════════════════════════════════════════════════

METAL_KW      = ["металлич", "ракушка", "разборн"]
CAPITAL_KW    = ["капитал", "бетон", "кирпич", "бокс", "гск", "железобет"]
DISTRICT_KW   = ["калининск", "красногвардейск", "приморск", "выборгск"]
AGENCY_KW     = ["агентств", "риэлтор", "агент ", "посредник", "брокер"]
SNOS_KW       = ["снос", "под снос", "реновац", "изъятие", "освобождение участка"]
NO_DOCS_KW    = ["без документов", "неоформлен", "без оформлен", "нет докум", "самострой"]
ELECTRIC_KW   = ["электр", "свет", "освещен", "розетк", "220", "380"]
SECURITY_KW   = ["охран", "видеонаблюдени", "шлагбаум", "консьерж", "сторож"]

MARKET_METAL    = 200_000
MARKET_CAPITAL  = 500_000

def garage_type(text):
    if any(k in text for k in METAL_KW):
        return "metal"
    if any(k in text for k in CAPITAL_KW):
        return "capital"
    return "unknown"

# ════════════════════════════════════════════════════════════════════════
# Фильтрация
# ════════════════════════════════════════════════════════════════════════

def filter_listings(listings, cfg):
    result = []
    f = cfg["filters"]
    for item in listings:
        t = item["text"]

        if not any(d in t for d in DISTRICT_KW):
            continue
        if f.get("exclude_agencies") and any(k in t for k in AGENCY_KW):
            continue
        if any(k in t for k in SNOS_KW):
            continue
        if any(k in t for k in NO_DOCS_KW):
            continue
        if cfg.get("area_min") and item["area"] and item["area"] < cfg["area_min"]:
            continue

        gtype = garage_type(t)
        if item["price"]:
            if gtype == "metal" and item["price"] > cfg["price_max_metal"]:
                continue
            if gtype in ("capital","unknown") and item["price"] > cfg["price_max_capital"]:
                continue

        if f.get("electricity_required") and not any(k in t for k in ELECTRIC_KW):
            continue

        item["_gtype"]        = gtype
        item["_has_security"] = any(k in t for k in SECURITY_KW)
        item.setdefault("_price_drop_pct", None)
        item.setdefault("_old_price", None)
        result.append(item)

    log.info("После фильтрации: %d объявлений", len(result))
    return result

# ════════════════════════════════════════════════════════════════════════
# История цен и снижение
# ════════════════════════════════════════════════════════════════════════

def find_price_drops(all_listings, seen, history, min_drop_pct):
    """Ищет старые объявления со значительным снижением цены."""
    drops = []
    for item in all_listings:
        lid = item["id"]
        if lid not in seen or not item["price"]:
            continue
        if lid not in history:
            continue
        old_p = history[lid]["price"]
        if old_p and item["price"] < old_p:
            pct = (old_p - item["price"]) / old_p * 100
            if pct >= min_drop_pct:
                item["_price_drop_pct"] = round(pct, 1)
                item["_old_price"]      = old_p
                item["_gtype"]          = garage_type(item["text"])
                item["_has_security"]   = any(k in item["text"] for k in SECURITY_KW)
                drops.append(item)
                log.info("Снижение цены %.1f%% — %s", pct, item["title"])
    return drops

def update_price_history(listings, history):
    today = date.today().isoformat()
    for item in listings:
        if not item["price"]:
            continue
        lid = item["id"]
        if lid not in history:
            history[lid] = {"price": item["price"], "date": today}
        elif item["price"] < history[lid]["price"]:
            history[lid] = {"price": item["price"], "date": today}
    return history

# ════════════════════════════════════════════════════════════════════════
# Ранжирование
# ════════════════════════════════════════════════════════════════════════

def score(item):
    s = 0.0
    if item["pub_date"]:
        try:
            age_h = (datetime.now(item["pub_date"].tzinfo) - item["pub_date"]).total_seconds() / 3600
            s += max(0, 40 - age_h * 0.5)
        except Exception:
            pass
    if item["price"]:
        market = MARKET_METAL if item.get("_gtype") == "metal" else MARKET_CAPITAL
        below_pct = (market - item["price"]) / market * 100
        s += min(50, max(0, below_pct))
    if item["area"]:
        s += min(item["area"] - 15, 20)
    if item.get("_has_security"):
        s += 10
    if item.get("_price_drop_pct"):
        s += 15
    if item.get("_gtype") == "capital":
        s += 5
    return s

def rank_and_pick(listings, top_n=5):
    return sorted(listings, key=score, reverse=True)[:top_n]

# ════════════════════════════════════════════════════════════════════════
# Форматирование карточки
# ════════════════════════════════════════════════════════════════════════

TYPE_LABEL = {
    "metal":   "🔩 Металлический гараж",
    "capital": "🏗 Капитальный / бокс",
    "unknown": "🏠 Гараж",
}

def fmt_price(p):
    if p is None:
        return "цена не указана"
    return f"{p:,}".replace(",", " ") + " ₽"

def market_line(item):
    if not item["price"]:
        return ""
    market = MARKET_METAL if item.get("_gtype") == "metal" else MARKET_CAPITAL
    diff = market - item["price"]
    if diff > 0:
        return f"📉 Ниже рынка на {diff/market*100:.0f}% (экономия {fmt_price(diff)})"
    elif diff < 0:
        return f"📈 Выше рынка на {abs(diff)/market*100:.0f}%"
    return "≈ По рынку"

def format_card(i, item):
    lines = [f"<b>#{i} {TYPE_LABEL.get(item.get('_gtype','unknown'))}</b>"]
    lines.append(f"<b>💰 {fmt_price(item['price'])}</b>")
    if item.get("_price_drop_pct"):
        lines.append(f"🔻 Снижение на {item['_price_drop_pct']}% (было {fmt_price(item['_old_price'])})")
    ml = market_line(item)
    if ml:
        lines.append(ml)
    lines.append(f"📐 Площадь: {item['area']:.0f} м²" if item["area"] else "📐 Площадь: не указана")
    if item.get("_has_security"):
        lines.append("🔒 Охрана / видеонаблюдение")
    if item["pub_date"]:
        lines.append(f"🗓 {item['pub_date'].strftime('%d.%m.%Y %H:%M')}")

    # Краткий анализ
    verdict = []
    market = MARKET_METAL if item.get("_gtype") == "metal" else MARKET_CAPITAL
    if item["price"] and item["price"] < market * 0.85:
        verdict.append("цена заметно ниже рынка")
    if item.get("_has_security"):
        verdict.append("есть охрана")
    if item.get("_price_drop_pct"):
        verdict.append(f"цена упала на {item['_price_drop_pct']}%")
    if verdict:
        lines.append(f"\n✅ <i>Сильный вариант: {', '.join(verdict)}</i>")

    lines.append(f"\n🔗 <a href=\"{item['link']}\">Открыть объявление</a>")
    return "\n".join(l for l in lines if l)

def format_message(listings, run_time):
    header = (
        f"🏠 <b>Топ-{len(listings)} гаражей — Санкт-Петербург</b>\n"
        f"📅 {run_time.strftime('%d.%m.%Y %H:%M')}\n"
        f"🏘 Районы: Калининский · Красногвардейский · Приморский · Выборгский\n"
        f"{'─'*32}\n"
    )
    if not listings:
        return header + "\nНовых подходящих объявлений нет."
    return header + "\n" + "\n\n".join(format_card(i, item) for i, item in enumerate(listings, 1))

# ════════════════════════════════════════════════════════════════════════
# Telegram
# ════════════════════════════════════════════════════════════════════════

def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("Ошибка Telegram: %s", e)
        return False

# ════════════════════════════════════════════════════════════════════════
# Главная задача
# ════════════════════════════════════════════════════════════════════════

def job():
    log.info("▶ Проверка (%s)", date.today().isoformat())
    cfg     = load_config()
    seen    = load_seen()
    history = load_price_history()

    rss_url      = build_rss_url(cfg)
    all_listings = fetch_listings(rss_url)

    new_listings = [x for x in all_listings if x["id"] not in seen]
    log.info("Новых: %d", len(new_listings))

    min_drop = cfg["filters"].get("min_price_drop_pct", 10)
    price_drops  = find_price_drops(all_listings, seen, history, min_drop)
    filtered_new = filter_listings(new_listings, cfg)

    combined = filtered_new + price_drops
    top = rank_and_pick(combined, top_n=cfg.get("top_n", 5))

    if top:
        msg = format_message(top, datetime.now())
        if send_telegram(cfg["telegram_token"], cfg["telegram_chat_id"], msg):
            log.info("✅ Отправлено: %d объявлений", len(top))
    else:
        log.info("Нет подходящих предложений, молчим.")

    history = update_price_history(all_listings, history)
    save_price_history(history)
    seen.update(x["id"] for x in all_listings)
    save_seen(seen)

def maybe_run_job():
    if is_active_day():
        job()
    else:
        log.info("Не активный день, пропускаем.")

def main():
    cfg = load_config()
    run_time = cfg.get("schedule_time", "10:00")
    log.info("🤖 Бот запущен. Запуск через день в %s (с 18.04.2026)", run_time)
    schedule.every().day.at(run_time).do(maybe_run_job)
    job()  # первый запуск сразу
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    main()
