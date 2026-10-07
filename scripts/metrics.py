"""Этап C: метрики и жёсткие отсечки по data/raw/<username>/channel.json.

python scripts/metrics.py <username> [<username> ...]

Прошедшие -> data/channels.csv, выброшенные -> data/rejected.csv с причиной.
Метрики канала -> data/raw/<username>/metrics.json.
Возраст поста считается от fetched_at, а не от сегодня: повторный расчёт
по кэшу даёт те же цифры.
"""
import argparse
import csv
import json
import re
import statistics
from datetime import datetime

from common import CHANNELS_CSV, RAW, add_rejected, clear_rejected, log

MIN_AGE_DAYS = 21          # правило возраста: моложе в метрики не идут
TREND_RECENT = (21, 110)
TREND_OLD = (111, 200)
TREND_MIN_POSTS = 3        # меньше постов в окне — trend unknown
# Пороги тренда — наш выбор, в CLAUDE.md не заданы:
# recent/old > 1.15 → растёт, < 0.85 → падает, иначе стоит.
TREND_UP, TREND_DOWN = 1.15, 0.85

SHOWCASE_WORDS = ["вакансии", "дайджест", "новости", "агентство", "школа",
                  "академия", "курсы от", "официальный канал"]

CYR = re.compile(r"[а-яё]", re.I)
LAT = re.compile(r"[a-z]", re.I)


def age_days(post, ref):
    return (ref - datetime.fromisoformat(post["date"])).total_seconds() / 86400


def median_views(posts):
    v = [p["views"] for p in posts if p["views"] is not None]
    return statistics.median(v) if v else None


def compute(ch):
    ref = datetime.fromisoformat(ch["fetched_at"])
    posts = [p for p in ch["posts"] if p.get("date")]
    for p in posts:
        p["_age"] = age_days(p, ref)

    aged = [p for p in posts if p["_age"] >= MIN_AGE_DAYS]
    med = median_views(aged)
    subs = ch.get("subscribers")
    er = round(med / subs, 4) if med is not None and subs else None

    last90 = [p for p in posts if p["_age"] <= 90]
    # ppw честен, только если история покрывает все 90 дней.
    covered = posts and max(p["_age"] for p in posts) >= 90
    ppw = round(len(last90) / (90 / 7), 2) if covered else None

    def window(lo, hi):
        return [p for p in posts if lo <= p["_age"] <= hi]

    w_new, w_old = window(*TREND_RECENT), window(*TREND_OLD)
    m_new, m_old = median_views(w_new), median_views(w_old)
    trend, trend_ratio = "unknown", None
    if (len(w_new) >= TREND_MIN_POSTS and len(w_old) >= TREND_MIN_POSTS
            and m_new is not None and m_old):
        trend_ratio = round(m_new / m_old, 3)
        trend = ("растёт" if trend_ratio > TREND_UP
                 else "падает" if trend_ratio < TREND_DOWN else "стоит")

    fwd_share = round(sum(p["forwarded"] for p in posts) / len(posts), 3) if posts else None
    own = [p for p in posts if not p["forwarded"]]
    avg_len = round(sum(len(p["text"]) for p in own) / len(own)) if own else None

    all_text = " ".join(p["text"] for p in posts)
    n_cyr, n_lat = len(CYR.findall(all_text)), len(LAT.findall(all_text))
    cyr_share = round(n_cyr / (n_cyr + n_lat), 3) if n_cyr + n_lat else None

    last_post_age = min((p["_age"] for p in posts), default=None)

    return {
        "username": ch["username"],
        "title": ch["title"],
        "subscribers": subs,
        "posts_total": len(posts),
        "posts_aged_21d": len(aged),
        "history_days": round(max((p["_age"] for p in posts), default=0)),
        "median_views": med,
        "er": er,
        "posts_per_week": ppw,
        "trend": trend,
        "trend_ratio": trend_ratio,
        "trend_median_21_110": m_new,
        "trend_median_111_200": m_old,
        "trend_posts_21_110": len(w_new),
        "trend_posts_111_200": len(w_old),
        "forwarded_share": fwd_share,
        # unknown: веб-превью t.me/s/ комментарии не отдаёт (проверено 2026-10-07)
        "comments_share": "unknown",
        "avg_len": avg_len,
        "cyrillic_share": cyr_share,
        "days_since_last_post": round(last_post_age, 1) if last_post_age is not None else None,
        "fetched_at": ch["fetched_at"],
    }


def hard_cuts(m, ch):
    """Список (reason, details). Пусто — канал прошёл."""
    reasons = []
    if m["days_since_last_post"] is None or m["days_since_last_post"] > 30:
        reasons.append(("мёртвый", f"последний пост {m['days_since_last_post']} дн. назад"))
    if m["median_views"] is not None and m["median_views"] < 800:
        reasons.append(("мелкий", f"median_views={m['median_views']}"))
    if m["forwarded_share"] is not None and m["forwarded_share"] > 0.5:
        reasons.append(("агрегатор", f"forwarded_share={m['forwarded_share']}"))
    if m["cyrillic_share"] is not None and m["cyrillic_share"] < 0.5:
        reasons.append(("не ru", f"cyrillic_share={m['cyrillic_share']}"))
    hay = f"{ch.get('title', '')} {ch.get('description', '')}".lower()
    hits = [w for w in SHOWCASE_WORDS if w in hay]
    if hits:
        reasons.append(("витрина", "в названии/описании: " + ", ".join(hits)))
    if (m["avg_len"] is not None and m["posts_per_week"] is not None
            and m["avg_len"] < 200 and m["posts_per_week"] > 14):
        reasons.append(("лента ссылок", f"avg_len={m['avg_len']}, ppw={m['posts_per_week']}"))
    return reasons


def upsert_channels_csv(row):
    rows = []
    if CHANNELS_CSV.exists():
        with CHANNELS_CSV.open(encoding="utf-8", newline="") as f:
            rows = [r for r in csv.DictReader(f) if r["username"] != row["username"]]
    rows.append(row)
    fields = list(row.keys())
    with CHANNELS_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def remove_from_channels_csv(username):
    if not CHANNELS_CSV.exists():
        return
    with CHANNELS_CSV.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        rows = [r for r in reader if r["username"] != username]
    with CHANNELS_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def process(username):
    username = username.strip().lstrip("@")
    path = RAW / username / "channel.json"
    if not path.exists():
        log(f"@{username}: нет channel.json, сначала fetch_channel.py")
        return None
    ch = json.loads(path.read_text(encoding="utf-8"))
    if ch.get("status") != "ok":
        log(f"@{username}: статус {ch.get('status')}, пропуск")
        return None
    m = compute(ch)
    reasons = hard_cuts(m, ch)
    m["verdict"] = "rejected" if reasons else "passed"
    m["reject_reasons"] = "; ".join(r for r, _ in reasons)
    (RAW / username / "metrics.json").write_text(
        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    if reasons:
        add_rejected(username, reasons, stage="C")
        remove_from_channels_csv(username)
    else:
        clear_rejected(username, stage="C")
        upsert_channels_csv(m)
    return m, reasons


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("usernames", nargs="+")
    args = ap.parse_args()
    for u in args.usernames:
        res = process(u)
        if res is None:
            continue
        m, reasons = res
        print(f"\n=== @{m['username']} — {m['title']}")
        for k, v in m.items():
            if k not in ("username", "title"):
                print(f"  {k:24} {v}")
        for r, d in reasons:
            print(f"  ОТСЕВ: {r} ({d})")


if __name__ == "__main__":
    main()
