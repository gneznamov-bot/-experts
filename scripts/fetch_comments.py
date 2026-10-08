"""Этап B2: комментарии через публичный виджет обсуждения.

python scripts/fetch_comments.py <username> [<username> ...] [--force]

Веб-превью t.me/s/ комментарии не отдаёт, но виджет обсуждения
https://t.me/<u>/<id>?embed=1&discussion=1 отдаёт их без логина
(проверено 2026-10-08):
- комментарии открыты -> в странице TWidgetDiscussion.init({"comments_cnt":N,...})
  и сами комментарии как .tgme_widget_message (до comments_limit штук);
- закрыты -> текст "Discussion is not available" и init без comments_cnt.

Один пост = один запрос, поэтому берём выборку до SAMPLE постов:
лучшие по просмотрам (для досье) + равномерно по времени (для доли).
Доля открытых комментариев считается по выборке и помечается как оценка.

Приватность: от комментаторов сохраняем только текст и дату, без имён и
ссылок. Отмечаем лишь, написал ли комментарий сам канал (from_owner).
"""
import argparse
import json
import re
import sys
from datetime import datetime

from bs4 import BeautifulSoup

from common import RAW, PoliteClient, StopStage, is_fresh, log, now_utc

SAMPLE = 15
TOP_BY_VIEWS = 5
MIN_AGE_DAYS = 3      # под совсем свежими постами обсуждение ещё не набралось
COMMENTS_LIMIT = 100

INIT_RE = re.compile(r"TWidgetDiscussion\.init\((\{.*?\})\)")


def pick_posts(ch):
    ref = datetime.fromisoformat(ch["fetched_at"])
    own = [p for p in ch["posts"] if not p["forwarded"] and p.get("date")]
    for p in own:
        p["_age"] = (ref - datetime.fromisoformat(p["date"])).days
    pool = [p for p in own if p["_age"] >= MIN_AGE_DAYS]
    top = sorted([p for p in pool if p["_age"] >= 21 and p["views"] is not None],
                 key=lambda p: -p["views"])[:TOP_BY_VIEWS]
    chosen = {p["id"] for p in top}
    rest = [p for p in pool if p["id"] not in chosen]
    need = SAMPLE - len(chosen)
    if rest and need > 0:
        step = max(1, len(rest) / need)
        chosen |= {rest[int(i * step)]["id"] for i in range(min(need, len(rest)))}
    return sorted(chosen, reverse=True)


def parse_discussion(html, username):
    m = INIT_RE.search(html)
    init = {}
    if m:
        try:
            init = json.loads(m.group(1))
        except json.JSONDecodeError:
            init = {}
    if "comments_cnt" in init:
        status = "open"
    elif "Discussion is not available" in html:
        status = "closed"
    else:
        status = "unknown"
    soup = BeautifulSoup(html, "lxml")
    owner_href = f"https://t.me/{username}".lower()
    comments = []
    for c in soup.select(".tgme_widget_message[data-post-id]"):
        t = c.select_one(".tgme_widget_message_bubble > .tgme_widget_message_text")
        tm = c.select_one("time[datetime]")
        a = c.select_one(".tgme_widget_message_author_name")
        href = (a.get("href") or "").lower() if a else ""
        if t:
            for br in t.find_all("br"):
                br.replace_with("\n")
        comments.append({
            "id": int(c["data-post-id"]),
            "date": tm["datetime"] if tm else None,
            "text": t.get_text().strip() if t else "",
            "from_owner": href == owner_href,
        })
    return {"status": status, "comments_cnt": init.get("comments_cnt"),
            "comments": comments}


def fetch_comments(username, client, force=False):
    username = username.strip().lstrip("@")
    base = RAW / username
    cj = base / "channel.json"
    out = base / "comments.json"
    if not cj.exists():
        log(f"@{username}: нет channel.json")
        return None
    if not force and is_fresh(out):
        log(f"@{username}: комментарии из кэша")
        return json.loads(out.read_text(encoding="utf-8"))
    ch = json.loads(cj.read_text(encoding="utf-8"))
    if ch.get("status") != "ok":
        return None
    cdir = base / "comments"
    cdir.mkdir(exist_ok=True)
    sampled = []
    for pid in pick_posts(ch):
        path = cdir / f"post_{pid}.html"
        if not force and is_fresh(path):
            html = path.read_text(encoding="utf-8")
        else:
            r = client.get(f"https://t.me/{username}/{pid}?embed=1&discussion=1"
                           f"&comments_limit={COMMENTS_LIMIT}")
            if r.status_code != 200:
                sampled.append({"post_id": pid, "status": "unknown",
                                "comments_cnt": None, "comments": [],
                                "error": f"HTTP {r.status_code}"})
                continue
            html = r.text
            path.write_text(html, encoding="utf-8")
        d = parse_discussion(html, username)
        d["post_id"] = pid
        d["url"] = f"https://t.me/{username}/{pid}"
        sampled.append(d)
    data = {"username": username, "fetched_at": now_utc().isoformat(timespec="seconds"),
            "sampled": sampled}
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    n_open = sum(s["status"] == "open" for s in sampled)
    n_com = sum(len(s["comments"]) for s in sampled)
    log(f"@{username}: выборка {len(sampled)} постов, открыты {n_open}, комментариев {n_com}")
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("usernames", nargs="+")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    client = PoliteClient()
    try:
        for u in args.usernames:
            fetch_comments(u, client, force=args.force)
    except StopStage as e:
        log(f"ЭТАП B2 ОСТАНОВЛЕН: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
