"""Раздел 3: проверка парсера на одном живом канале. Делать перед каждой пачкой.

python scripts/selftest.py <username>
Качает одну страницу t.me/s/<username> в data/raw/_selftest/ и печатает поля.
Ненулевой код выхода, если хоть одно поле пустое.
"""
import sys

from common import RAW, PoliteClient
from fetch_channel import parse_page


def main():
    username = sys.argv[1].lstrip("@")
    out = RAW / "_selftest"
    out.mkdir(parents=True, exist_ok=True)
    r = PoliteClient().get(f"https://t.me/s/{username}")
    print("HTTP", r.status_code, r.url)
    (out / f"{username}_page_1.html").write_text(r.text, encoding="utf-8")
    d = parse_page(r.text)
    posts = sorted(d["posts"], key=lambda p: p["id"], reverse=True)
    print("Название:  ", d["title"])
    print("Описание:  ", d["description"])
    print("Подписчики:", d["subscribers"])
    print("Постов на странице:", len(posts), "| before:", d["before"])
    for p in posts[:5]:
        print("-" * 60)
        print(p["url"], p["date"], "views:", p["views"], "fwd:", p["forwarded"])
        print(p["text"][:300] or "<нет текста>")
    empty = [k for k in ("title", "description", "subscribers") if not d[k]]
    if not posts:
        empty.append("posts")
    elif any(p["date"] is None or p["views"] is None for p in posts[:5]):
        empty.append("date/views")
    print("=" * 60, "\nПУСТЫЕ ПОЛЯ:", empty or "нет")
    sys.exit(1 if empty else 0)


if __name__ == "__main__":
    main()
