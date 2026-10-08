"""Этап D: сигналы, красные флаги и сегмент.

python scripts/signals.py <username> [<username> ...]

Вход: data/raw/<u>/channel.json (+ comments.json, если есть).
Выход: data/raw/<u>/signals.json.

Каждое совпадение = дословная цитата (подстрока сохранённого текста),
дата и ссылка. Без ссылки совпадение не записывается.
Ищем в своих постах (пересланные не считаются) и в описании канала.
Группа gold ищется также в комментариях других людей (не владельца).
"""
import argparse
import json
import re
from datetime import datetime

from common import RAW, compile_terms, find_all, load_yaml, log, quote_around

SEGMENT_GROUPS = ["has_product", "burned", "pragmatic", "gold", "burnout",
                  "missionary", "limits", "audience_care"]
QUOTES_PER_GROUP = 8
SALE_WINDOW_DAYS = 45


def sources(ch, comments):
    """(kind, text, date, url) для всего, где ищем сигналы."""
    u = ch["username"]
    if ch.get("description"):
        yield "description", ch["description"], ch["fetched_at"], f"https://t.me/{u}"
    for p in ch["posts"]:
        if not p["forwarded"] and p["text"]:
            yield "post", p["text"], p["date"], p["url"]
    for s in (comments or {}).get("sampled", []):
        for c in s["comments"]:
            if c["text"] and not c["from_owner"]:
                yield "comment", c["text"], c["date"], f"{s['url']}?comment={c['id']}"


def clean(ms, ex_rx, text):
    """Убирает совпадения, попавшие внутрь фразы-исключения."""
    if not ex_rx:
        return ms
    spans = [(e.start(), e.end()) for e in find_all(ex_rx, text)]
    return [m for m in ms if not any(a <= m.start() < b for a, b in spans)]


def match_group(rx, items, limit=QUOTES_PER_GROUP, ex_rx=None):
    """Все совпадения группы: по одной цитате на источник."""
    found, seen = [], set()
    for kind, text, date, url in items:
        ms = clean(find_all(rx, text), ex_rx, text)
        if not ms or url in seen:
            continue
        seen.add(url)
        m = ms[0]
        q = quote_around(text, m.start(), m.end())
        assert q in text  # цитата обязана быть дословной
        found.append({"phrase": m.group(), "quote": q, "date": date, "url": url,
                      "source": kind})
    return {"count": len(found), "matches": found[:limit]}


def has_product(cfg, price_rx, items, ex_rx=None):
    strong = compile_terms(cfg["strong"])
    weak = compile_terms(cfg["weak"])
    own = compile_terms(cfg["own_product"])
    found, seen = [], set()
    for kind, text, date, url in items:
        if kind == "comment" or url in seen:
            continue
        ms = find_all(strong, text)
        if not ms and price_rx.search(text) and find_all(own, text):
            ms = clean(find_all(weak, text), ex_rx, text)
        if not ms:
            continue
        seen.add(url)
        m = ms[0]
        found.append({"phrase": m.group(), "quote": quote_around(text, m.start(), m.end()),
                      "date": date, "url": url, "source": kind})
    return {"count": len(found), "matches": found[:QUOTES_PER_GROUP]}


def red_flags(ch, kw, items):
    rf = kw["red_flags"]
    ref = datetime.fromisoformat(ch["fetched_at"])
    own = [(k, t, d, u) for k, t, d, u in items if k == "post"]
    flags = []

    infobiz_rx = compile_terms(rf["infobiz"])
    infobiz_re = [re.compile(r, re.I) for r in rf["infobiz_regex"]]
    for k, t, d, u in own:
        ms = find_all(infobiz_rx, t)
        if ms:
            m = ms[0]
            flags.append({"flag": "infobiz", "quote": quote_around(t, m.start(), m.end()),
                          "date": d, "url": u})
            continue
        for r in infobiz_re:
            m = r.search(t)
            if m:
                flags.append({"flag": "infobiz", "quote": quote_around(t, m.start(), m.end()),
                              "date": d, "url": u})
                break

    course = compile_terms(rf["course_word"])
    sale = compile_terms(rf["sale_words"])
    price_rx = re.compile(kw["price_pattern"], re.I)
    selling = []
    for k, t, d, u in own:
        if (ref - datetime.fromisoformat(d)).days > SALE_WINDOW_DAYS:
            continue
        mc = find_all(course, t)
        if mc and (find_all(sale, t) or price_rx.search(t)):
            selling.append({"quote": quote_around(t, mc[0].start(), mc[0].end()),
                            "date": d, "url": u})
    if len(selling) >= 2:
        flags.append({"flag": "active_course_sale",
                      "details": f"{len(selling)} постов о продаже курса за {SALE_WINDOW_DAYS} дн.",
                      "examples": selling[:3]})

    we, me = compile_terms(rf["we"]), compile_terms(rf["i"])
    n_we = sum(len(find_all(we, t)) for _, t, _, _ in own)
    n_i = sum(len(find_all(me, t)) for _, t, _, _ in own)
    if n_we > n_i:
        flags.append({"flag": "we_not_i", "details": f"«мы/наш» {n_we} раз против «я/мой» {n_i}"})

    ads_rx = compile_terms(rf["ads"])
    last90 = [p for p in ch["posts"]
              if (ref - datetime.fromisoformat(p["date"])).days <= 90]
    n_ads = sum(1 for p in last90 if find_all(ads_rx, p["text"]))
    if last90 and n_ads > len(last90) - n_ads:
        flags.append({"flag": "ads_majority",
                      "details": f"рекламных {n_ads} из {len(last90)} постов за 90 дн."})
    return flags, {"we": n_we, "i": n_i, "ads_90d": n_ads, "posts_90d": len(last90)}


def segment(g):
    c = {k: g[k]["count"] for k in g}
    if c["burned"]:
        return "обжёгшийся одиночка", "burned"
    if c["gold"] and not c["has_product"]:
        return "сидящий на золоте", "gold"
    if c["has_product"] and c["pragmatic"]:
        return "прагматик", "has_product+pragmatic"
    if c["burnout"] and c["burnout"] > max(c["missionary"], c["pragmatic"]):
        return "беглец из найма", "burnout"
    if c["missionary"] and c["missionary"] > max(c["burnout"], c["pragmatic"]):
        return "миссионер", "missionary"
    return "не определён", None


def process(username):
    username = username.strip().lstrip("@")
    base = RAW / username
    ch = json.loads((base / "channel.json").read_text(encoding="utf-8"))
    if ch.get("status") != "ok":
        return None
    cpath = base / "comments.json"
    comments = json.loads(cpath.read_text(encoding="utf-8")) if cpath.exists() else None
    kw = load_yaml("keywords.yaml")
    # Посты с рекламной меткой — чужой текст: из сигналов исключаем
    # (на @xor_journal «разбор резюме» нашёлся в рекламе сервиса).
    ads_rx = compile_terms(kw["red_flags"]["ads"])
    items = [x for x in sources(ch, comments)
             if not (x[0] == "post" and find_all(ads_rx, x[1]))]
    no_comments = [x for x in items if x[0] != "comment"]
    price_rx = re.compile(kw["price_pattern"], re.I)

    groups = {}
    for name in SEGMENT_GROUPS:
        cfg = kw["signals"][name]
        ex_rx = compile_terms(kw.get("exclude", {}).get(name, []))
        if name == "has_product":
            groups[name] = has_product(cfg, price_rx, no_comments, ex_rx)
        else:
            groups[name] = match_group(compile_terms(cfg),
                                       items if name == "gold" else no_comments,
                                       ex_rx=ex_rx)
    flags, counters = red_flags(ch, kw, items)
    seg, basis = segment(groups)
    data = {
        "username": username,
        "segment": seg,
        "segment_basis": basis,
        "groups": groups,
        "red_flags": flags,
        "counters": counters,
        "comments_used": comments is not None,
    }
    (base / "signals.json").write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("usernames", nargs="+")
    args = ap.parse_args()
    for u in args.usernames:
        d = process(u)
        if not d:
            log(f"@{u}: пропуск")
            continue
        counts = ", ".join(f"{k}={v['count']}" for k, v in d["groups"].items() if v["count"])
        flags = ", ".join(f["flag"] for f in d["red_flags"]) or "нет"
        print(f"@{u}: {d['segment']} | {counts} | флаги: {flags}")


if __name__ == "__main__":
    main()
