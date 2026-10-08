"""Этап A: сбор кандидатов -> data/candidates.csv.

python scripts/discover.py snowball [<username> ...]   # A2, офлайн по data/raw
python scripts/discover.py addlist <t.me/addlist/...> [...]   # A1, одна страница на папку
python scripts/discover.py habr --niche it [--hubs sql ...] [--top 30]   # A5, Хабр
python scripts/discover.py profiles [--source A5:habr]   # канал из описания личного аккаунта

Формат строки: username, title_guess, source, source_url, found_at.
Уже виденные (config/seen.json, CRM) и уже записанные в candidates.csv не
добавляются повторно.

Реализованы: A1 (папки), A2 (снежный ком), A5 (Хабр, scripts/habr.py).
A3 (TGStat) — следующий; A4, A6, A7 — ещё нет.
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

from bs4 import BeautifulSoup

from common import DATA, RAW, PoliteClient, StopStage, exclusions, log, now_utc

CANDIDATES_CSV = DATA / "candidates.csv"
FIELDS = ["username", "title_guess", "source", "source_url", "found_at"]
SNOWBALL_DAYS = 180

# @ник не после буквы/точки (чтобы не ловить почту), t.me/ник, но не t.me/+…,
# t.me/joinchat, t.me/c/…, t.me/addlist (папки — отдельный источник).
MENTION_RX = re.compile(r"(?<![\w.])@([A-Za-z][A-Za-z0-9_]{3,31})(?!\w)")
LINK_RX = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})(?![\w])")
ADDLIST_RX = re.compile(r"(?:https?://)?t\.me/addlist/([A-Za-z0-9_-]+)")
SERVICE = {"joinchat", "addlist", "share", "proxy", "socks", "setlanguage",
           "addstickers", "addemoji", "addtheme", "iv", "boost", "c", "s",
           "telegram", "durov", "contact", "login", "confirmphone", "invoice"}


def bad_username(u, own):
    ul = u.lower()
    return ul == own.lower() or ul.endswith("bot") or ul in SERVICE


def load_candidates():
    if not CANDIDATES_CSV.exists():
        return []
    with CANDIDATES_CSV.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def append_candidates(new):
    rows = load_candidates()
    have = {r["username"].lower() for r in rows}
    skip = exclusions()
    added = []
    for r in new:
        u = r["username"].lower()
        if u in have or u in skip:
            continue
        have.add(u)
        added.append(r)
    DATA.mkdir(exist_ok=True)
    with CANDIDATES_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows + added)
    return added


def snowball(usernames=None):
    """A2: упоминания @ник, ссылки t.me/ник и пересылки в постах за 180 дней.
    Личные аккаунты отсеются на этапе B как unavailable (у них нет t.me/s/)."""
    dirs = [RAW / u for u in usernames] if usernames else \
        [d for d in RAW.iterdir() if d.is_dir() and not d.name.startswith("_")]
    refs = defaultdict(list)          # username -> [(source_channel, post_url)]
    addlists = set()
    for d in dirs:
        cj = d / "channel.json"
        if not cj.exists():
            continue
        ch = json.loads(cj.read_text(encoding="utf-8"))
        if ch.get("status") != "ok":
            continue
        # Рекомендации берём только у прошедших отсев: у витрин и
        # файлопомоек в постах контакты HR и сетки своих же каналов.
        mj = d / "metrics.json"
        if mj.exists() and json.loads(mj.read_text(encoding="utf-8"))["verdict"] != "passed":
            continue
        own = ch["username"]
        ref = datetime.fromisoformat(ch["fetched_at"])
        for p in ch["posts"]:
            if (ref - datetime.fromisoformat(p["date"])).days > SNOWBALL_DAYS:
                continue
            names = set(MENTION_RX.findall(p["text"])) | set(LINK_RX.findall(p["text"]))
            if p.get("forwarded_from"):
                names |= set(LINK_RX.findall(p["forwarded_from"]))
            addlists |= {f"https://t.me/addlist/{c}" for c in ADDLIST_RX.findall(p["text"])}
            for n in names:
                if not bad_username(n, own):
                    refs[n.lower()].append((own, p["url"]))
    found_at = now_utc().date().isoformat()
    # Чаще упомянутые — первыми: больше шансов, что это канал из ниши.
    order = sorted(refs, key=lambda u: -len({s for s, _ in refs[u]}) * 1000 - len(refs[u]))
    new = [{"username": u, "title_guess": "", "source": "A2:snowball",
            "source_url": refs[u][0][1], "found_at": found_at} for u in order]
    added = append_candidates(new)
    stats = Counter(len({s for s, _ in refs[u]}) for u in refs)
    log(f"снежный ком: {len(refs)} упомянутых, добавлено новых {len(added)}; "
        f"упомянуты в 1/2/3+ каналах: {stats[1]}/{stats[2]}/"
        f"{sum(v for k, v in stats.items() if k >= 3)}")
    if addlists:
        log("найдены папки (запустить addlist): " + " ".join(sorted(addlists)))
    return added, sorted(addlists)


def addlist(urls):
    """A1: страница папки t.me/addlist/<code> -> каналы из неё.
    Если страница каналов не отдала — помечаем как ручной источник."""
    client = PoliteClient()
    found_at = now_utc().date().isoformat()
    manual, new = [], []
    for url in urls:
        url = url if url.startswith("http") else "https://" + url
        r = client.get(url)
        out_dir = RAW / "_addlist"
        out_dir.mkdir(parents=True, exist_ok=True)
        code = url.rstrip("/").split("/")[-1]
        (out_dir / f"{code}.html").write_text(r.text, encoding="utf-8")
        soup = BeautifulSoup(r.text, "lxml")
        names = []
        for a in soup.select("a[href]"):
            for n in LINK_RX.findall(a["href"]):
                if not bad_username(n, ""):
                    title = a.get_text(" ", strip=True)[:80]
                    names.append((n, title))
        names = list(dict.fromkeys(names))
        if not names:
            manual.append(url)
            continue
        new += [{"username": n.lower(), "title_guess": t, "source": "A1:addlist",
                 "source_url": url, "found_at": found_at} for n, t in names]
    added = append_candidates(new)
    log(f"папки: добавлено {len(added)}")
    if manual:
        log("ПАПКИ НЕ ОТДАЛИ СПИСОК — открыть руками и прислать каналы: " + " ".join(manual))
    return added, manual


def habr_source(niche, hubs=None, top=30, min_rating=5):
    import habr
    from common import load_yaml
    hubs = hubs or load_yaml("niches.yaml")[niche].get("habr_hubs") or []
    if not hubs:
        log(f"Хабр: для ниши {niche} не заданы habr_hubs")
        return []
    rows = habr.discover(hubs, top=top, min_rating=min_rating)
    # Подробности (статья, рейтинг, где нашлась ссылка) — для разбора
    (RAW / "_habr").mkdir(parents=True, exist_ok=True)
    (RAW / "_habr" / "found.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    added = append_candidates(rows)
    log(f"Хабр: найдено ссылок {len(rows)}, новых кандидатов {len(added)}")
    return added


def profiles(source=None):
    """Кандидаты, оказавшиеся личными аккаунтами (этап B: unavailable), —
    открываем их публичную страницу t.me/<ник> и берём каналы из описания.
    Это то, что человек сам опубликовал в профиле (правило 7).

    Разметка проверена 2026-10-08 на t.me/antipov_d: имя .tgme_page_title,
    описание .tgme_page_description, кнопка «Send Message» = личный аккаунт.
    """
    client = PoliteClient()
    found_at = now_utc().date().isoformat()
    rows = [r for r in load_candidates() if not source or r["source"] == source]
    new, checked, with_link = [], 0, 0
    for r in rows:
        u = r["username"]
        cj = RAW / u / "channel.json"
        if not cj.exists() or json.loads(cj.read_text(encoding="utf-8")).get("status") != "unavailable":
            continue
        path = RAW / u / "profile.html"
        if path.exists():
            html = path.read_text(encoding="utf-8")
        else:
            resp = client.get(f"https://t.me/{u}")
            if resp.status_code != 200:
                continue
            html = resp.text
            path.write_text(html, encoding="utf-8")
        checked += 1
        soup = BeautifulSoup(html, "lxml")
        title = soup.select_one(".tgme_page_title")
        desc = soup.select_one(".tgme_page_description")
        if not desc:
            continue
        text = desc.get_text(" ", strip=True)
        names = set(MENTION_RX.findall(text)) | set(LINK_RX.findall(text))
        for a in desc.select("a[href]"):
            names |= set(LINK_RX.findall(a["href"]))
        names = [n for n in names if not bad_username(n, u)]
        if names:
            with_link += 1
        for n in names:
            new.append({"username": n.lower(),
                        "title_guess": title.get_text(" ", strip=True) if title else "",
                        "source": f"{r['source']}+profile", "source_url": f"https://t.me/{u}",
                        "found_at": found_at})
            log(f"@{u}: в описании профиля @{n} — «{text[:120]}»")
    added = append_candidates(new)
    log(f"профили: проверено {checked}, со ссылкой в описании {with_link}, "
        f"новых кандидатов {len(added)}")
    return added


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snowball")
    s.add_argument("usernames", nargs="*")
    a = sub.add_parser("addlist")
    a.add_argument("urls", nargs="+")
    pr = sub.add_parser("profiles")
    pr.add_argument("--source")
    h = sub.add_parser("habr")
    h.add_argument("--niche", default="it")
    h.add_argument("--hubs", nargs="*")
    h.add_argument("--top", type=int, default=30, help="авторов на хаб")
    h.add_argument("--min-rating", type=int, default=5)
    args = ap.parse_args()
    try:
        if args.cmd == "snowball":
            added, _ = snowball(args.usernames or None)
        elif args.cmd == "profiles":
            added = profiles(args.source)
        elif args.cmd == "habr":
            added = habr_source(args.niche, args.hubs, args.top, args.min_rating)
        else:
            added, _ = addlist(args.urls)
    except StopStage as e:
        log(f"ЭТАП A ОСТАНОВЛЕН: {e}")
        sys.exit(2)
    for r in added[:40]:
        print(r["username"], r["source"], r["source_url"])
    if len(added) > 40:
        print(f"... и ещё {len(added) - 40}")


if __name__ == "__main__":
    main()
