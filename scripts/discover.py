"""Этап A: сбор кандидатов -> data/candidates.csv.

python scripts/discover.py snowball [<username> ...]   # A2, офлайн по data/raw
python scripts/discover.py addlist <t.me/addlist/...> [...]   # A1, одна страница на папку

Формат строки: username, title_guess, source, source_url, found_at.
Уже виденные (config/seen.json, CRM) и уже записанные в candidates.csv не
добавляются повторно.

Пока реализованы только источники на t.me. A3–A7 (TGStat, поиск, Хабр,
менторские платформы, конференции) требуют доступа к этим сайтам в
сетевой политике окружения и проверки их robots.txt.
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
        w = csv.DictWriter(f, fieldnames=FIELDS)
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


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snowball")
    s.add_argument("usernames", nargs="*")
    a = sub.add_parser("addlist")
    a.add_argument("urls", nargs="+")
    args = ap.parse_args()
    try:
        if args.cmd == "snowball":
            added, _ = snowball(args.usernames or None)
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
