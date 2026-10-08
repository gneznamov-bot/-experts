"""Этап F: shortlist.md, досье на топ-10, rejected.csv для пачки.

python scripts/report.py --niche it <username> [<username> ...]
  [--out out/batch-YYYY-MM-DD-<ниша>] [--size 30] [--dossiers 10]

Берёт только прошедших отсев (metrics.json verdict=passed) с score.json.
Все цифры и цитаты — из сохранённых файлов, ничего не дописывается.
"""
import argparse
import csv
import json
import re
import statistics
from datetime import datetime
from pathlib import Path

from common import RAW, REJECTED_CSV, ROOT, now_utc

STRONG_RATIO = 1.5      # «сильный пост» — просмотры ≥ 1.5× медианы на тот момент
STRONG_WINDOW = 45      # медиана на тот момент: посты 21+ дней в ±45 дней от поста
GROUP_TITLES = {
    "has_product": "Уже продаёт (has_product)", "burned": "Обжёгся (burned)",
    "pragmatic": "Прагматик (pragmatic)", "gold": "Спрос на обучение (gold)",
    "burnout": "Беглец из найма (burnout)", "missionary": "Миссионер (missionary)",
    "limits": "Пределы и нагрузка (limits)", "audience_care": "Дорожит каналом (audience_care)",
}
FLAG_TITLES = {"infobiz": "Инфобиз-лексика", "active_course_sale": "Активно продаёт курс",
               "we_not_i": "Канал от «мы», а не от «я»", "ads_majority": "Рекламы больше, чем своего"}


def load(u, name):
    p = RAW / u / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def ddmmyyyy(iso):
    return datetime.fromisoformat(iso).strftime("%d.%m.%Y") if iso else "unknown"


def short_url(url):
    return url.replace("https://", "")


def first_line(text, limit=90):
    line = next((l.strip() for l in text.splitlines() if l.strip()), "")
    return line if len(line) <= limit else line[:limit].rstrip() + "…"


def strong_posts(ch, limit=5):
    ref = datetime.fromisoformat(ch["fetched_at"])
    aged = [p for p in ch["posts"] if p["views"] is not None and not p["forwarded"]
            and (ref - datetime.fromisoformat(p["date"])).days >= 21]
    out = []
    for p in aged:
        d = datetime.fromisoformat(p["date"])
        near = [q["views"] for q in aged
                if abs((datetime.fromisoformat(q["date"]) - d).days) <= STRONG_WINDOW
                and q["id"] != p["id"]]
        if len(near) < 5:
            continue
        med = statistics.median(near)
        if med and p["views"] >= STRONG_RATIO * med:
            out.append({**p, "median_then": med, "ratio": round(p["views"] / med, 1)})
    return sorted(out, key=lambda p: -p["ratio"])[:limit]


def top_commented(ch):
    """Пост из выборки комментариев с числом комментариев ≥ 2× медианы и ≥ 10."""
    cm = load(ch["username"], "comments.json")
    if not cm:
        return None
    opened = [x for x in cm["sampled"] if x["status"] == "open" and x["comments_cnt"] is not None]
    if len(opened) < 5:
        return None
    med = statistics.median(x["comments_cnt"] for x in opened)
    best = max(opened, key=lambda x: x["comments_cnt"])
    if best["comments_cnt"] < max(10, 2 * med):
        return None
    post = next((p for p in ch["posts"] if p["id"] == best["post_id"]), None)
    if not post:
        return None
    return {"text": post["text"], "cnt": best["comments_cnt"], "median": med,
            "url": post["url"]}


def gender_guess(ch):
    """По постам о себе: «я …л» против «я …ла». Это оценка, не факт."""
    own = " ".join(p["text"] for p in ch["posts"] if not p["forwarded"])
    m = len(re.findall(r"(?<!\w)я\s+(?:\w+\s+)?\w+л(?!\w)", own, re.I))
    f = len(re.findall(r"(?<!\w)я\s+(?:\w+\s+)?\w+ла(?!\w)", own, re.I))
    if m + f < 3:
        return f"unknown (мало глаголов о себе: м {m} / ж {f}) — проверить руками"
    g = "м" if m > 2 * f else "ж" if f > 2 * m else "unknown"
    return f"{g} (оценка: «я …л» {m}, «я …ла» {f}) — проверить руками"


def name_guess(ch):
    desc = ch.get("description") or ""
    m = re.search(r"(?:меня зовут|^я)\s+([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)", desc, re.M | re.I)
    if m:
        return m.group(1)
    # Название вида «Кирилл Сачков | Development» / «Дима | Перекат в AQA»
    head = re.split(r"\s*\|\s*|\s+[—–-]\s+", ch.get("title") or "")[0].strip()
    if re.fullmatch(r"[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?", head):
        return head
    return "unknown"


def num(x):
    if isinstance(x, float) and x.is_integer():
        return int(x)
    return x


def hook(ch, m, s):
    """Одна конкретная зацепка из данных, по приоритету."""
    gold = [x for x in s["groups"]["gold"]["matches"] if x["source"] == "comment"]
    if gold:
        return (f"{len(gold)} раз(а) спрашивали про обучение в комментариях: "
                f"«{gold[0]['quote'][:80]}»")
    if s["groups"]["burned"]["matches"]:
        x = s["groups"]["burned"]["matches"][0]
        return f"писал(а) о неудаче: «{x['quote'][:90]}» ({ddmmyyyy(x['date'])})"
    sp = strong_posts(ch, 1)
    if sp:
        p = sp[0]
        return (f"пост «{first_line(p['text'], 60)}» собрал в {p['ratio']}× больше "
                f"медианы ({p['views']} против {int(p['median_then'])})")
    c = top_commented(ch)
    if c:
        return (f"пост «{first_line(c['text'], 60)}» — комментариев: {c['cnt']} "
                f"при медиане {num(c['median'])} по выборке")
    if s["groups"]["limits"]["matches"]:
        x = s["groups"]["limits"]["matches"][0]
        return f"упирается в нагрузку: «{x['quote'][:90]}»"
    return "явной зацепки автомат не нашёл — смотреть руками"


def quotes_md(matches, limit=5):
    lines = []
    for x in matches[:limit]:
        src = " (комментарий)" if x["source"] == "comment" else \
              " (описание канала)" if x["source"] == "description" else ""
        q = x["quote"].replace("\n", " ")
        lines.append(f"- «{q}» — {ddmmyyyy(x['date'])}, {short_url(x['url'])}{src}")
    return "\n".join(lines)


def dossier(u, ch, m, s, sc):
    L = [f"# @{u} — {name_guess(ch)}", "", f"Канал: «{ch['title']}» · балл {sc['score']}/100", "",
         "## Факты"]
    er = f"{round(m['er'] * 100)}%" if m["er"] is not None else "unknown"
    L.append(f"Подписчики: {m['subscribers']} · Медиана просмотров (посты 21+ дней): "
             f"{num(m['median_views'])} · ER: {er}  ")
    L.append(f"Постов в неделю: {m['posts_per_week']} · Тренд: {m['trend']}"
             + (f" (медиана {num(m['trend_median_21_110'])} за дни 21–110 против "
                f"{num(m['trend_median_111_200'])} за 111–200)"
                if m["trend_ratio"] else "") + "  ")
    cs = m["comments_share"]
    L.append(f"Комментарии: открыты в {cs if cs == 'unknown' else str(round(cs * 100)) + '%'} "
             f"постов (оценка по выборке {m['comments_sample']}), медиана "
             f"{m['comments_median_cnt']} на пост  ")
    L.append(f"Пол: {gender_guess(ch)}  ")
    L.append(f"Контакт: {sc['contact'] or 'не найден в описании'}  ")
    L.append(f"Описание канала: «{(ch.get('description') or '').replace(chr(10), ' / ')}»")
    L += ["", "## Что продаёт сейчас"]
    hp = s["groups"]["has_product"]["matches"]
    L.append(quotes_md(hp) if hp else "Продаж в постах и описании не найдено.")
    L += ["", f"## Сегмент: {s['segment']}"]
    if s["segment_basis"]:
        L.append("Основание — цитаты:")
        for g in s["segment_basis"].split("+"):
            L.append(quotes_md(s["groups"][g]["matches"], 3))
    else:
        L.append("Подтверждающих цитат для сегмента нет — не определён.")
    L += ["", "## Сильные посты"]
    sp = strong_posts(ch)
    if sp:
        for p in sp:
            L.append(f"- {ddmmyyyy(p['date'])} · {p['views']} просмотров · медиана на тот момент "
                     f"{int(p['median_then'])} (×{p['ratio']}) · «{first_line(p['text'])}» · "
                     f"{short_url(p['url'])}")
    else:
        L.append(f"Постов с просмотрами ≥{STRONG_RATIO}× медианы не найдено.")
    L += ["", "## Сигналы по группам"]
    any_g = False
    for g, title in GROUP_TITLES.items():
        x = s["groups"][g]
        if x["count"]:
            any_g = True
            L += [f"**{title}** — {x['count']} совпад.", quotes_md(x["matches"]), ""]
    if not any_g:
        L.append("Ни одна группа не сработала.")
    L += ["", "## Красные флаги"]
    if s["red_flags"]:
        for f in s["red_flags"]:
            t = FLAG_TITLES.get(f["flag"], f["flag"])
            if "quote" in f:
                L.append(f"- {t}: «{f['quote']}» — {ddmmyyyy(f['date'])}, {short_url(f['url'])}")
            else:
                L.append(f"- {t}: {f.get('details', '')}")
                for e in f.get("examples", []):
                    L.append(f"  - «{e['quote']}» — {ddmmyyyy(e['date'])}, {short_url(e['url'])}")
    else:
        L.append("Не найдено.")
    L += ["", "## Баллы", "| Блок | Балл | Основание |", "|---|---|---|"]
    for k, v in sc["blocks"].items():
        L.append(f"| {k} | {v['points']} | {v['basis']} |")
    for p in sc["penalties"]:
        L.append(f"| штраф {p['flag']} | {p['points']} | |")
    L += ["", "## Чего не хватает"]
    gaps = ["Закреплённое сообщение: веб-превью его отдельно не отдаёт — открыть канал.",
            "Прошлые продукты старше "
            f"{m['history_days']} дней: история скачана только за этот срок.",
            "Пол и имя определены автоматически — проверить по постам о себе."]
    if m["comments_sample"]:
        gaps.append(f"Комментарии прочитаны только под {m['comments_sample']} постами из "
                    f"{m['posts_total']} — спрос на обучение мог прозвучать под другими.")
    else:
        gaps.append("Комментарии не прочитаны.")
    for r in sc["needs_review"]:
        gaps.append(f"Ручная проверка: {r}.")
    if not hp:
        gaps.append("Продаж не найдено — проверить, нет ли продукта вне канала (сайт, профиль).")
    L += [f"- {g}" for g in gaps]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("usernames", nargs="+")
    ap.add_argument("--niche", default="it")
    ap.add_argument("--out")
    ap.add_argument("--size", type=int, default=30)
    ap.add_argument("--dossiers", type=int, default=10)
    args = ap.parse_args()
    out = Path(args.out) if args.out else \
        ROOT / "out" / f"batch-{now_utc().date().isoformat()}-{args.niche}"
    (out / "dossier").mkdir(parents=True, exist_ok=True)

    rows, names = [], [u.lstrip("@") for u in args.usernames]
    for u in names:
        m, s, sc, ch = load(u, "metrics.json"), load(u, "signals.json"), \
            load(u, "score.json"), load(u, "channel.json")
        if not (m and s and sc and ch) or m["verdict"] != "passed":
            continue
        rows.append((sc["score"], u, ch, m, s, sc))
    rows.sort(key=lambda r: -r[0])
    top = rows[:args.size]

    L = [f"# Shortlist — {args.niche}, {now_utc().date().strftime('%d.%m.%Y')}", "",
         f"Прошли отсев: {len(rows)} · в списке: {len(top)}", ""]
    if len(rows) < args.size:
        L += [f"> Нашлось {len(rows)} из {args.size} — не добираю мусором.", ""]
    L += ["| # | Канал | Имя | Подписчики | Медиана | ER | Сегмент | Балл | За что зацепиться |",
          "|---|---|---|---|---|---|---|---|---|"]
    for i, (score, u, ch, m, s, sc) in enumerate(top, 1):
        er = f"{round(m['er'] * 100)}%" if m["er"] is not None else "unknown"
        h = hook(ch, m, s).replace("|", "/").replace("\n", " ")
        L.append(f"| {i} | @{u} | {name_guess(ch)} | {m['subscribers']} | {num(m['median_views'])} | "
                 f"{er} | {s['segment']} | {score} | {h} |")
    (out / "shortlist.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    for score, u, ch, m, s, sc in top[:args.dossiers]:
        (out / "dossier" / f"{u}.md").write_text(dossier(u, ch, m, s, sc), encoding="utf-8")

    if REJECTED_CSV.exists():
        with REJECTED_CSV.open(encoding="utf-8", newline="") as f:
            rej = [r for r in csv.DictReader(f) if r["username"] in names]
        with (out / "rejected.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["username", "reason", "details", "stage", "date"])
            w.writeheader()
            w.writerows(rej)
    print(f"{out.relative_to(ROOT)}: shortlist {len(top)}, досье "
          f"{min(len(top), args.dossiers)}")


if __name__ == "__main__":
    main()
