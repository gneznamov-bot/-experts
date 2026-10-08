"""Оркестратор пачки (раздел 10 CLAUDE.md).

python scripts/run_batch.py --niche it --size 30
    кандидаты из data/candidates.csv (этап A: снежный ком + папки)
python scripts/run_batch.py --niche it --channels a b c   (или --channels-file list.txt)
    свой список каналов вместо этапа A
Флаги: --addlist <url> ... (папки для A1), --target 120, --no-comments,
       --selftest-channel senatorov_head, --include-seen (пересчёт уже виденных)

Порядок: проверка парсера → A → B → C → комментарии → C → D → E → F → seen.json.
"""
import argparse
import subprocess
import sys
from collections import Counter

import discover
import fetch_channel
import fetch_comments
import metrics
import report
import score
import signals
from common import (ROOT, PoliteClient, StopStage, exclusions, load_seen, log,
                    now_utc, save_seen)


def selftest(channel):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "selftest.py"), channel],
                       capture_output=True, text=True)
    tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-300:]
    if r.returncode != 0:
        log("ПРОВЕРКА ПАРСЕРА НЕ ПРОЙДЕНА (раздел 3), пачку не запускаю:\n" + r.stdout[-1500:]
            + r.stderr[-500:])
        sys.exit(3)
    log(f"проверка парсера на @{channel}: {tail}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche", default="it")
    ap.add_argument("--size", type=int, default=30)
    ap.add_argument("--target", type=int, default=120)
    ap.add_argument("--channels", nargs="*")
    ap.add_argument("--channels-file")
    ap.add_argument("--addlist", nargs="*", default=[])
    ap.add_argument("--no-comments", action="store_true")
    ap.add_argument("--selftest-channel", default="senatorov_head")
    ap.add_argument("--include-seen", action="store_true",
                    help="не вычитать seen.json/CRM (повторный прогон тех же каналов)")
    args = ap.parse_args()

    selftest(args.selftest_channel)
    excl = set() if args.include_seen else exclusions()

    # --- A ---
    source_of = {}
    if args.channels or args.channels_file:
        names = list(args.channels or [])
        if args.channels_file:
            names += [l.strip() for l in open(args.channels_file, encoding="utf-8") if l.strip()]
        names = [n.strip().rstrip("/").split("/")[-1].lstrip("@").lower() for n in names]
        cands = [n for n in dict.fromkeys(names) if n not in excl]
        known = {r["username"].lower(): r["source"] for r in discover.load_candidates()}
        source_of = {n: known.get(n, "manual") for n in cands}
        skipped = len(names) - len(cands)
        if skipped:
            log(f"исключено как уже виденные/CRM: {skipped}")
    else:
        if args.addlist:
            discover.addlist(args.addlist)
        discover.snowball()
        rows = [r for r in discover.load_candidates() if r["username"].lower() not in excl]
        rows = rows[:args.target]
        cands = [r["username"].lower() for r in rows]
        source_of = {r["username"].lower(): r["source"] for r in rows}
    log(f"кандидатов: {len(cands)}")
    if not cands:
        log("кандидатов нет: нужны новые папки, список каналов или другие источники")
        return

    client = PoliteClient()
    verdicts, reasons_all = {}, Counter()
    try:
        # --- B ---
        for u in cands:
            ch = fetch_channel.fetch_channel(u, client)
            if ch.get("status") != "ok":
                verdicts[u] = "unavailable"
                reasons_all["unavailable"] += 1
        # --- C ---
        alive = [u for u in cands if u not in verdicts]
        passed = []
        for u in alive:
            m, reasons = metrics.process(u, args.niche)
            if reasons:
                verdicts[u] = "rejected: " + m["reject_reasons"]
                reasons_all.update(r for r, _ in reasons)
            else:
                passed.append(u)
        # --- комментарии (только прошедшим) и пересчёт C ---
        if not args.no_comments:
            for u in passed:
                fetch_comments.fetch_comments(u, client)
                metrics.process(u, args.niche)
    except StopStage as e:
        log(f"ПАЧКА ОСТАНОВЛЕНА: {e}. Готовое сохранено, повторный запуск продолжит из кэша.")
        sys.exit(2)

    # --- D, E ---
    segs = Counter()
    for u in passed:
        s = signals.process(u)
        sc = score.process(u)
        verdicts[u] = f"passed: {sc['score']}"
        segs[s["segment"]] += 1

    # --- F ---
    out = ROOT / "out" / f"batch-{now_utc().date().isoformat()}-{args.niche}"
    sys.argv = ["report.py", "--niche", args.niche, "--out", str(out),
                "--size", str(args.size), *cands]
    report.main()

    # --- seen.json ---
    seen = load_seen()
    today = now_utc().date().isoformat()
    for u, v in verdicts.items():
        seen[u] = {"date": today, "verdict": v, "niche": args.niche,
                   "source": source_of.get(u, "")}
    save_seen(seen)

    # --- отчёт ---
    by_src = Counter(source_of.get(u, "") for u in cands)
    by_src_ok = Counter(source_of.get(u, "") for u in passed)
    print("\n=== ИТОГ ПАЧКИ ===")
    print(f"кандидатов: {len(cands)} · отсеяно: {len(cands) - len(passed)} · осталось: {len(passed)}")
    print("причины отсева:", ", ".join(f"{k} {v}" for k, v in reasons_all.most_common()) or "нет")
    print("источники (всего → прошло):",
          ", ".join(f"{k} {v}→{by_src_ok[k]}" for k, v in by_src.most_common()))
    print("сегменты:", ", ".join(f"{k} {v}" for k, v in segs.most_common()) or "нет")
    if len(passed) < args.size:
        print(f"нашлось {len(passed)} из {args.size}, источники исчерпаны — "
              f"нужны новые папки, список каналов или ниша")
    print(f"результат: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
