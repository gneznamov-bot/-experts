"""Этап A5: Хабр — авторы сильных статей профильных хабов с каналом в Telegram.

python scripts/discover.py habr --niche it [--top 30] [--hubs sql data_engineering]

Официального API нет, RSS не подошёл (проверено 2026-10-08):
- RSS хаба есть, ссылка в <link rel="alternate" type="application/rss+xml"> на
  странице хаба: https://habr.com/ru/rss/hubs/<hub>/articles/?fl=ru
  Но в нём только 40 последних статей (~месяц) и нет рейтинга. Не годится.
- Поэтому HTML списка хаба: https://habr.com/ru/hubs/<hub>/articles/pageN/
  (шаблон pageN взят из ссылок пагинации на странице хаба). Идём назад по
  страницам, пока статьи не станут старше полугода.

Селекторы (проверены 2026-10-08 на хабах sql и data_engineering):
- карточка статьи: article.tm-articles-list__item
  рейтинг .tm-votes-meter__value_rating, автор .tm-user-info__username,
  ссылка a.tm-title__link, дата time[datetime]
- страница статьи: текст #post-content-body; автор и его контакты — в
  JSON-состоянии страницы: "author":{... "contacts":[...],"authorContacts":[...]}
  контакт Telegram выглядит так: {"title":"Telegram","url":"https://telegram.me/<ник>"}
- страница автора https://habr.com/ru/users/<login>/: "contacts":[...] и
  "aboutHtml" в JSON-состоянии. Открываем, только если в статье и в контактах
  автора Telegram не нашёлся.

robots.txt (2026-10-08): для * стоит Crawl-delay: 10 и запреты /search/,
/*?*utm_, /ru/users/*/followers/ и т.п. Пауза = max(2 с, Crawl-delay).
urllib.robotparser не понимает * в путях, поэтому правила проверяем сами.
"""
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup

from common import RAW, UA as BROWSER_UA, is_fresh, log, now_utc

HABR = "https://habr.com"
MIN_PAUSE = 2.0
CACHE_DIR = RAW / "_habr"
HALF_YEAR = timedelta(days=183)
MAX_HUB_PAGES = 40

TG_RX = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})(?![\w/])")
# Ссылка из текста статьи засчитывается, только если рядом слова о своём канале
# (иначе ловим чужие каналы, которые автор просто цитирует).
OWN_CHANNEL_RX = re.compile(r"(мо[йеё]\w*|сво[йеё]\w*|веду|подпис\w*|канал\w*|телеграм\w*|"
                            r"telegram|tg\b|тг\b)", re.I)
SERVICE = {"joinchat", "addlist", "share", "proxy", "socks", "addstickers", "c", "s",
           "iv", "boost", "telegram", "durov"}


def strip_utm(url):
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.startswith("utm_")]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))


class Robots:
    """Группа правил для нашего UA или '*'. Поддерживает * и $ в путях,
    побеждает самое длинное совпадение, Allow при равной длине."""

    def __init__(self, text, ua):
        groups, cur, agents = {}, None, []
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            k, v = (x.strip() for x in line.split(":", 1))
            k = k.lower()
            if k == "user-agent":
                if cur is not None and cur["rules"]:
                    agents = []
                agents.append(v.lower())
                cur = groups.setdefault(tuple(agents), {"rules": [], "delay": None})
                for a in agents:
                    groups[(a,)] = cur
            elif cur is not None and k in ("allow", "disallow") and v:
                rx = "^" + re.escape(v).replace(r"\*", ".*").replace(r"\$", "$")
                cur["rules"].append((k == "allow", len(v), re.compile(rx)))
            elif cur is not None and k == "crawl-delay":
                try:
                    cur["delay"] = float(v)
                except ValueError:
                    pass
        token = ua.split("/")[0].lower()
        self.group = groups.get((token,)) or groups.get(("*",)) or {"rules": [], "delay": None}

    @property
    def delay(self):
        return self.group["delay"]

    def allowed(self, url):
        p = urlsplit(url)
        path = p.path + ("?" + p.query if p.query else "")
        best = None
        for allow, length, rx in self.group["rules"]:
            if rx.match(path) and (best is None or length > best[1]
                                   or (length == best[1] and allow)):
                best = (allow, length)
        return True if best is None else best[0]


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
        self.pause = MIN_PAUSE
        self.requests = 0
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _sleep(self):
        wait = self.pause - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()

    def _fetch(self, url):
        for attempt in range(3):
            self._sleep()
            self.requests += 1
            try:
                r = self.session.get(url, timeout=30)
            except requests.RequestException as e:
                log(f"сеть: {e.__class__.__name__} на {url}, попытка {attempt + 1}")
                continue
            if r.status_code in (403, 429):
                if attempt == 0:
                    log(f"HTTP {r.status_code} на {url} — пауза 10 минут, один повтор")
                    time.sleep(600)
                    continue
            return r
        return None

    def _load_robots(self):
        path = CACHE_DIR / "robots.txt"
        if is_fresh(path):
            text = path.read_text(encoding="utf-8")
        else:
            r = self._fetch(f"{HABR}/robots.txt")
            if r is None or r.status_code != 200:
                raise RuntimeError("robots.txt Хабра не отдался — не продолжаю")
            text = r.text
            path.write_text(text, encoding="utf-8")
        self.robots = Robots(text, self.ua)
        self.pause = max(MIN_PAUSE, self.robots.delay or 0)
        log(f"Хабр: пауза между запросами {self.pause:g} с (Crawl-delay из robots.txt)")

    def get(self, url):
        """Текст страницы из кэша или из сети. None — запрещено robots.txt
        или страница не отдалась."""
        url = strip_utm(url)
        if self.robots is None:
            self._load_robots()
        path = CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".html")
        if is_fresh(path):
            return path.read_text(encoding="utf-8")
        if not self.robots.allowed(url):
            log(f"robots.txt запрещает: {url}")
            return None
        r = self._fetch(url)
        if r is None or r.status_code != 200:
            log(f"HTTP {r.status_code if r is not None else 'нет ответа'} на {url}")
            return None
        path.write_text(r.text, encoding="utf-8")
        path.with_suffix(".url").write_text(url, encoding="utf-8")
        return r.text


def parse_rating(s):
    s = (s or "").strip().replace("–", "-").replace("−", "-")
    m = re.match(r"^([+-]?)(\d+)", s)
    if not m:
        return None
    return -int(m.group(2)) if m.group(1) == "-" else int(m.group(2))


def hub_articles(client, hub, since):
    """Статьи хаба новее since: [{id, url, title, author, date, rating}]."""
    out = []
    for page in range(1, MAX_HUB_PAGES + 1):
        url = f"{HABR}/ru/hubs/{hub}/articles/" + (f"page{page}/" if page > 1 else "")
        html = client.get(url)
        if not html:
            break
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select("article.tm-articles-list__item")
        if not cards:
            break
        oldest = None
        for c in cards:
            t = c.select_one("a.tm-title__link")
            d = c.select_one("time[datetime]")
            u = c.select_one(".tm-user-info__username")
            r = c.select_one(".tm-votes-meter__value_rating")
            if not (t and d):
                continue
            date = datetime.fromisoformat(d["datetime"].replace("Z", "+00:00"))
            oldest = date if oldest is None or date < oldest else oldest
            if date < since:
                continue
            out.append({"id": c.get("id"), "url": HABR + t["href"],
                        "title": t.get_text(" ", strip=True),
                        "author": u.get_text(strip=True) if u else None,
                        "date": date.isoformat(),
                        "rating": parse_rating(r.get_text() if r else None),
                        "company": "/companies/" in t["href"], "hub": hub})
        if oldest and oldest < since:
            break
    return out


def json_after(html, key, start_hint=None):
    """Значение JSON после "key": в состоянии страницы (первое вхождение
    после start_hint, если задан)."""
    i = html.find(start_hint) if start_hint else 0
    if i < 0:
        return None
    j = html.find(f'"{key}":', i)
    if j < 0:
        return None
    try:
        val, _ = json.JSONDecoder().raw_decode(html, j + len(key) + 3)
        return val
    except json.JSONDecodeError:
        return None


def tg_from_contacts(contacts):
    found = []
    for c in contacts or []:
        for field in ("url", "value"):
            found += TG_RX.findall(str(c.get(field, "")))
    return [u for u in dict.fromkeys(found) if u.lower() not in SERVICE]


def article_links(client, art):
    """Telegram из текста статьи (рядом слова о своём канале) и из контактов автора."""
    html = client.get(art["url"])
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    body = soup.select_one("#post-content-body")
    body_tg = []
    if body and not art["company"]:
        # В блогах компаний ссылки в тексте — обычно канал компании, их не берём.
        for a in body.select("a[href]"):
            for u in TG_RX.findall(a["href"]):
                ctx = (a.find_parent(["p", "li", "div"]) or a).get_text(" ", strip=True)
                if OWN_CHANNEL_RX.search(ctx) and u.lower() not in SERVICE:
                    body_tg.append((u, ctx[:200]))
    # В странице два "author": schema.org (без контактов) и состояние
    # приложения — его узнаём по "author":{"id":
    i = html.find('"author":{"id":')
    author = {}
    if i >= 0:
        try:
            author, _ = json.JSONDecoder().raw_decode(html, i + len('"author":'))
        except json.JSONDecodeError:
            author = {}
    contacts = (author.get("contacts") or []) + (author.get("authorContacts") or []) \
        if isinstance(author, dict) else []
    return {"body": body_tg, "contacts": tg_from_contacts(contacts),
            "fullname": author.get("fullname") if isinstance(author, dict) else None,
            "alias": author.get("alias") if isinstance(author, dict) else art["author"]}


def profile_links(client, login):
    html = client.get(f"{HABR}/ru/users/{login}/")
    if not html:
        return []
    found = tg_from_contacts(json_after(html, "contacts", f'"alias":"{login}"') or [])
    about = json_after(html, "aboutHtml", f'"alias":"{login}"') or ""
    found += [u for u in TG_RX.findall(about) if u.lower() not in SERVICE]
    return list(dict.fromkeys(found))


def discover(hubs, top=30, min_rating=5):
    """Кандидаты из Хабра: [{username, title_guess, source, source_url, found_at, ...}]."""
    client = HabrClient()
    since = now_utc() - HALF_YEAR
    found_at = now_utc().date().isoformat()
    rows, stats = [], {"articles": 0, "checked": 0, "authors_with_tg": 0}
    seen_authors = set()
    for hub in hubs:
        arts = hub_articles(client, hub, since)
        stats["articles"] += len(arts)
        best = sorted([a for a in arts if (a["rating"] or 0) >= min_rating],
                      key=lambda a: -(a["rating"] or 0))
        picked = []
        for a in best:  # по одной лучшей статье на автора в хабе
            if a["author"] and a["author"] not in seen_authors and len(picked) < top:
                seen_authors.add(a["author"])
                picked.append(a)
        log(f"Хабр/{hub}: статей за полгода {len(arts)}, с рейтингом ≥{min_rating}: "
            f"{len(best)}, берём авторов: {len(picked)}")
        for a in picked:
            stats["checked"] += 1
            links = article_links(client, a)
            if links is None:
                continue
            tg = [(u, "контакты автора на Хабре") for u in links["contacts"]] + \
                 [(u, "ссылка в статье: " + ctx) for u, ctx in links["body"]]
            if not tg and a["author"]:
                tg = [(u, "профиль автора на Хабре") for u in profile_links(client, a["author"])]
            if tg:
                stats["authors_with_tg"] += 1
            for u, where in dict.fromkeys(tg):
                rows.append({"username": u.lower(),
                             "title_guess": links["fullname"] or a["author"] or "",
                             "source": "A5:habr", "source_url": a["url"],
                             "found_at": found_at,
                             "_hub": hub, "_rating": a["rating"], "_title": a["title"],
                             "_author": a["author"], "_where": where})
    log(f"Хабр: статей {stats['articles']}, проверено авторов {stats['checked']}, "
        f"с Telegram {stats['authors_with_tg']}, запросов в сеть {client.requests}")
    return rows
