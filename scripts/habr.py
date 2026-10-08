"""Этап A5: Хабр — авторы сильных статей профильных хабов с каналом в Telegram.

Официального API нет. Читаем публичные страницы с соблюдением robots.txt:
User-Agent из HABR_USER_AGENT, пауза 2 с, один поток, кэш на диск 14 дней.

Шаблоны адресов (RSS хаба, страница автора) и селекторы фиксируются здесь
только после проверки на живой странице — см. комментарии с датой ниже.
"""
import hashlib
import os
import time
import urllib.robotparser

import requests

from common import RAW, UA as BROWSER_UA, is_fresh, log

HABR = "https://habr.com"
PAUSE = 2.0
CACHE_DIR = RAW / "_habr"


class HabrClient:
    def __init__(self):
        self.ua = os.environ.get("HABR_USER_AGENT")
        if not self.ua:
            log("HABR_USER_AGENT не задан — использую обычный браузерный User-Agent")
            self.ua = BROWSER_UA
        self.session = requests.Session()
        self.session.headers["User-Agent"] = self.ua
        self.last = 0.0
        self.robots = None
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _sleep(self):
        wait = PAUSE - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()

    def _load_robots(self):
        path = CACHE_DIR / "robots.txt"
        if is_fresh(path):
            text = path.read_text(encoding="utf-8")
        else:
            self._sleep()
            r = self.session.get(f"{HABR}/robots.txt", timeout=30)
            r.raise_for_status()
            text = r.text
            path.write_text(text, encoding="utf-8")
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(text.splitlines())
        self.robots = rp

    def allowed(self, url):
        if self.robots is None:
            self._load_robots()
        return self.robots.can_fetch(self.ua, url)

    def get(self, url):
        """Текст страницы из кэша или из сети. None, если robots.txt запрещает
        или страница не отдалась."""
        path = CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".html")
        if is_fresh(path):
            return path.read_text(encoding="utf-8")
        if not self.allowed(url):
            log(f"robots.txt запрещает: {url}")
            return None
        self._sleep()
        r = self.session.get(url, timeout=30)
        if r.status_code in (403, 429):
            log(f"HTTP {r.status_code} на {url} — пауза 10 минут, один повтор")
            time.sleep(600)
            self._sleep()
            r = self.session.get(url, timeout=30)
        if r.status_code != 200:
            log(f"HTTP {r.status_code} на {url}")
            return None
        path.write_text(r.text, encoding="utf-8")
        (path.with_suffix(".url")).write_text(url, encoding="utf-8")
        return r.text
