"""Этап B: скачивание канала через веб-превью https://t.me/s/<username>.

python scripts/fetch_channel.py <username> [<username> ...] [--force]

Сырые страницы -> data/raw/<username>/page_*.html, результат -> channel.json.
Повторный запуск в течение 14 дней в сеть не ходит.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from common import (RAW, PoliteClient, StopStage, add_rejected, clear_rejected,
                    is_fresh, log, now_utc, parse_count)

# Граница истории. В CLAUDE.md для пагинации стоит 180 дней, но метрика
# trend (этап C) сравнивает окна 21–110 и 111–200 дней, поэтому качаем до 200,
# иначе второе окно всегда обрезано.
MAX_AGE_DAYS = 200
MAX_POSTS = 400


def text_of(el):
    if el is None:
        return ""
    for br in el.find_all("br"):
        br.replace_with("\n")
    return el.get_text().strip()


# Селекторы проверены 2026-10-07 на @senatorov_head (data/raw/_selftest/).
# Замечания по факту разметки на эту дату:
# - веб-превью t.me/s/ НЕ отдаёт ни ссылку на обсуждение, ни счётчик
#   комментариев (в HTML нет ничего с "comment"), даже когда комментарии
#   в канале открыты. Поэтому has_comments = None (unknown), не False.
# - альбом — один .tgme_widget_message, но каждое фото занимает свой id,
#   поэтому на странице бывает 6 постов вместо ~20. Пагинация по
#   data-before у .tme_messages_more, он равен минимальному id на странице.
# - просмотры приходят округлёнными до 3 значащих цифр ("1.48K").
def parse_page(html):
    soup = BeautifulSoup(html, "lxml")
    info = soup.select_one(".tgme_channel_info")
    subscribers = None
    for c in soup.select(".tgme_channel_info_counter"):
        ctype = c.select_one(".counter_type")
        value = c.select_one(".counter_value")
        if ctype and value and "subscriber" in ctype.get_text().lower():
            subscribers = parse_count(value.get_text())

    posts = []
    for m in soup.select(".tgme_widget_message[data-post]"):
        if "service_message" in m.get("class", []):
            continue
        data_post = m["data-post"]
        t = m.select_one(".tgme_widget_message_footer time[datetime]") or \
            m.select_one("time[datetime]")
        views = m.select_one(".tgme_widget_message_views")
        fwd = m.select_one(".tgme_widget_message_forwarded_from")
        fwd_from = None
        if fwd:
            a = fwd.select_one("a[href]")
            fwd_from = a["href"] if a else fwd.get_text(" ", strip=True)
        posts.append({
            "id": int(data_post.split("/")[-1]),
            "date": t["datetime"] if t else None,
            "text": text_of(m.select_one(".tgme_widget_message_text")),
            "views": parse_count(views.get_text()) if views else None,
            "forwarded": fwd is not None,
            "forwarded_from": fwd_from,
            "has_comments": None,  # unknown: веб-превью не отдаёт, см. выше
            "url": f"https://t.me/{data_post}",
        })

    more = soup.select_one(".tme_messages_more[data-before]")
    return {
        "has_info": info is not None,
        "title": text_of(soup.select_one(".tgme_channel_info_header_title")),
        "description": text_of(soup.select_one(".tgme_channel_info_description")),
        "subscribers": subscribers,
        "posts": posts,
        "before": int(more["data-before"]) if more else None,
    }


def load_page(client, username, before, out_dir, force=False):
    name = "page_latest.html" if before is None else f"page_before_{before}.html"
    path = out_dir / name
    if not force and is_fresh(path):
        return 200, path.read_text(encoding="utf-8"), path.name
    url = f"https://t.me/s/{username}" + ("" if before is None else f"?before={before}")
    r = client.get(url)
    # Канал без веб-превью редиректит с /s/<u> на /<u>.
    if r.status_code != 200 or "/s/" not in r.url:
        return r.status_code if r.status_code != 200 else 302, "", name
    path.write_text(r.text, encoding="utf-8")
    return 200, r.text, name


def mark_unavailable(username, out_dir, details):
    data = {"username": username, "status": "unavailable", "details": details,
            "fetched_at": now_utc().isoformat(timespec="seconds")}
    (out_dir / "channel.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    add_rejected(username, [("unavailable", details)], stage="B")
    log(f"@{username}: unavailable ({details})")
    return data


def fetch_channel(username, client, force=False):
    username = username.strip().lstrip("@").split("/")[-1]
    out_dir = RAW / username
    out_dir.mkdir(parents=True, exist_ok=True)
    cj = out_dir / "channel.json"
    if not force and is_fresh(cj):
        log(f"@{username}: из кэша")
        return json.loads(cj.read_text(encoding="utf-8"))

    fetched_at = now_utc()
    cutoff = fetched_at - timedelta(days=MAX_AGE_DAYS)
    meta, posts, before, pages = None, {}, None, 0
    while True:
        status, html, page_name = load_page(client, username, before, out_dir, force)
        if status != 200:
            if meta is None:
                return mark_unavailable(username, out_dir, f"HTTP {status} на {page_name}")
            log(f"@{username}: HTTP {status} на {page_name}, история обрезана")
            break
        parsed = parse_page(html)
        pages += 1
        if meta is None:
            if not parsed["has_info"] or not parsed["posts"]:
                return mark_unavailable(username, out_dir, "нет блока канала или сообщений")
            meta = parsed
        if not parsed["posts"]:
            break
        for p in parsed["posts"]:
            posts[p["id"]] = p
        oldest = min((p["date"] for p in parsed["posts"] if p["date"]), default=None)
        if oldest and datetime.fromisoformat(oldest) < cutoff:
            break
        if len(posts) >= MAX_POSTS or parsed["before"] is None:
            break
        before = parsed["before"]

    ordered = sorted(posts.values(), key=lambda p: p["id"], reverse=True)[:MAX_POSTS]
    data = {
        "username": username,
        "status": "ok",
        "title": meta["title"],
        "description": meta["description"],
        "subscribers": meta["subscribers"],
        "fetched_at": fetched_at.isoformat(timespec="seconds"),
        "pages": pages,
        "posts": ordered,
    }
    cj.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    clear_rejected(username, stage="B")
    log(f"@{username}: {len(ordered)} постов, {pages} стр.")
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("usernames", nargs="+")
    ap.add_argument("--force", action="store_true", help="игнорировать кэш")
    args = ap.parse_args()
    client = PoliteClient()
    try:
        for u in args.usernames:
            fetch_channel(u, client, force=args.force)
    except StopStage as e:
        log(f"ЭТАП B ОСТАНОВЛЕН: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
