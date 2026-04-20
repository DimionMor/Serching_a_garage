#!/usr/bin/env python3
"""
Авито Гараж Бот — Санкт-Петербург
Парсит RSS Авито, фильтрует гаражи, отправляет топ-5 в Telegram.
Запуск: через день, начиная с 18 апреля 2026 года.
"""

import json
import logging
import os
import re
import time
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import requests
import schedule

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

def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    if os.environ.get("TELEGRAM_TOKEN"):
        cfg["telegram_token"] = os.environ["TELEGRAM_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        cfg["telegram_chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    return cfg

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

SCHEDULE_START = date(2026, 4, 18)

def is_active_day():
    today = date.today()
    delta = (today - SCHEDULE_START).days
    return delta >= 0 and delta % 2 == 0

def build_rss_url(cfg):
    city = cfg["city"]
    pmax = cfg["price_max_capital"]
    params = {
        "s":       104,
        "owner[]": "private",
        "pmax":    pmax,
        "format":  "rss",
    }
    if cfg.get("area_min"):
        params["minArea"] = cfg["area_min"]
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return f"https://www.avito.ru/{city}/garazhi_i_mashinomesta?{qs}"

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
        full_text = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
        price    = extract_price(entry.get("
