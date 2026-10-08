"""Этап E: скоринг 0–100 по разделу 8 CLAUDE.md.

python scripts/score.py <username> [<username> ...]

Вход: metrics.json + signals.json (+ channel.json). Выход: data/raw/<u>/score.json.
Каждый блок хранит балл и основание; где автомат не уверен — пометка
«нужна ручная проверка», где цифра не замер, а оценка — «оценка».
"""
import argparse
import json
import re

from common import RAW, compile_terms, find_all, load_yaml, log

MENTOR_TERMS = compile_terms(["менторств", "наставничеств", "консультаци",
                              "беру учеников", "занимаюсь с", "=ментор"])
ONEOFF_TERMS = compile_terms(["разбор резюме", "разбор", "мок-собес", "мок собес",
                              "mock"])

CONTACT_HINT = compile_terms(["пиш", "личк", "=dm", "=лс", "вопрос", "связ",
                              "контакт", "сотруднич", "реклам", "обучен", "менторств"])
MENTION_RX = re.compile(r"(?:@|t\.me/)([A-Za-z][A-Za-z0-9_]{3,31})")
FORM_RX = re.compile(r"(forms\.gle|docs\.google|tally\.so|forms\.yandex|typeform|"
                     r"taplink|linktr\.ee)", re.I)


def block_audience(m):
    er = m["er"]
    if er is None:
        return 0, "ER unknown"
    pts = 20 if er >= 0.25 else 15 if er >= 0.15 else 10 if er >= 0.10 else 5
    return pts, f"ER={er}"


def block_monetization(s):
    texts = [x["quote"] for x in s["groups"]["has_product"]["matches"]]
    if any(find_all(MENTOR_TERMS, t) for t in texts):
        return 25, "менторство/консультации: " + s["groups"]["has_product"]["matches"][0]["url"]
    if any(find_all(ONEOFF_TERMS, t) for t in texts):
        return 15, "разовые платные разборы"
    if texts:
        return 15, "что-то продаёт, но не менторство (оценка) — нужна ручная проверка"
    return 0, "продаж не найдено"


def audience_of(texts, kw):
    entry = compile_terms(kw["audience"]["entry"])
    pro = compile_terms(kw["audience"]["pro"])
    e = sum(1 for t in texts if find_all(entry, t))
    p = sum(1 for t in texts if find_all(pro, t))
    if e == p == 0:
        return None, e, p
    if p >= 1.5 * e:
        return "pro", e, p
    if e >= 1.5 * p:
        return "entry", e, p
    return "mixed", e, p


def block_free_top(ch, s, kw):
    """Свободный верх: продаёт вход или ничего, а пишет для действующих."""
    own = [p["text"] for p in ch["posts"] if not p["forwarded"] and p["text"]]
    content, ce, cp = audience_of(own, kw)
    prod_texts = [x["quote"] for x in s["groups"]["has_product"]["matches"]]
    product, pe, pp = audience_of(prod_texts, kw) if prod_texts else (None, 0, 0)
    basis = (f"контент: вход {ce} / рост {cp} постов; "
             f"продукт: {'нет' if not prod_texts else f'вход {pe} / рост {pp}'}")
    if product == "pro":
        return 5, "продаёт для действующих; " + basis, False
    if content == "pro" and (not prod_texts or product == "entry"):
        return 20, "пишет для действующих, верх не занят; " + basis, False
    return 10, "нужна ручная проверка; " + basis, True


SEGMENT_PTS = {"обжёгшийся одиночка": 15, "прагматик": 15, "сидящий на золоте": 10,
               "беглец из найма": 8, "миссионер": 5, "не определён": 0}


def block_liveness(m):
    t = m["trend"]
    pts = {"растёт": 10, "стоит": 7, "падает": 3}.get(t, 5)
    basis = f"тренд {t}" + (" (unknown → 5, оценка)" if t not in ("растёт", "стоит", "падает") else "")
    if m["posts_per_week"] is not None and m["posts_per_week"] < 1:
        pts -= 3
        basis += f"; −3: {m['posts_per_week']} поста/нед"
    return max(pts, 0), basis


def block_contact(ch):
    desc = ch.get("description") or ""
    own = ch["username"].lower()
    personal, bots = [], []
    for line in desc.splitlines():
        for u in MENTION_RX.findall(line):
            if u.lower() == own:
                continue
            if u.lower().endswith("bot"):
                bots.append(u)
            elif find_all(CONTACT_HINT, line):
                personal.append(u)
    if personal:
        return 10, f"@{personal[0]}", "@" + personal[0]
    if bots or FORM_RX.search(desc):
        return 5, "только бот или форма", ("@" + bots[0]) if bots else "форма"
    return 0, "контакта в описании нет", None


def process(username):
    base = RAW / username
    m = json.loads((base / "metrics.json").read_text(encoding="utf-8"))
    s = json.loads((base / "signals.json").read_text(encoding="utf-8"))
    ch = json.loads((base / "channel.json").read_text(encoding="utf-8"))
    kw = load_yaml("keywords.yaml")

    blocks = {}
    blocks["audience"] = block_audience(m)
    blocks["monetization"] = block_monetization(s)
    ft = block_free_top(ch, s, kw)
    blocks["free_top"] = ft[:2]
    blocks["segment"] = (SEGMENT_PTS[s["segment"]], s["segment"])
    blocks["liveness"] = block_liveness(m)
    c = block_contact(ch)
    blocks["contact"] = c[:2]

    penalties = []
    for f in s["red_flags"]:
        penalties.append((f["flag"], -20 if f["flag"] == "infobiz" else -10))
    # infobiz — один штраф −20 на канал, а не за каждый пост
    seen, uniq = set(), []
    for name, pts in penalties:
        if name not in seen:
            seen.add(name)
            uniq.append((name, pts))

    total = sum(b[0] for b in blocks.values()) + sum(p for _, p in uniq)
    total = max(0, min(100, total))
    review = []
    if ft[2]:
        review.append("свободный верх")
    if m.get("needs_review"):
        review.append(m["needs_review"])
    data = {
        "username": username,
        "score": total,
        "blocks": {k: {"points": v[0], "basis": v[1]} for k, v in blocks.items()},
        "penalties": [{"flag": n, "points": p} for n, p in uniq],
        "contact": c[2],
        "needs_review": review,
    }
    (base / "score.json").write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("usernames", nargs="+")
    args = ap.parse_args()
    for u in args.usernames:
        u = u.lstrip("@")
        if not (RAW / u / "signals.json").exists():
            log(f"@{u}: нет signals.json")
            continue
        d = process(u)
        parts = " ".join(f"{k}={v['points']}" for k, v in d["blocks"].items())
        pen = " ".join(f"{p['flag']}{p['points']}" for p in d["penalties"])
        print(f"@{u}: {d['score']:3} | {parts} {pen}")


if __name__ == "__main__":
    main()
