"""Общее для этапов: пути, вежливый HTTP, кэш, rejected.csv."""
import csv
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DATA = ROOT / "data"
RAW = DATA / "raw"
REJECTED_CSV = DATA / "rejected.csv"
CHANNELS_CSV = DATA / "channels.csv"

CACHE_TTL = timedelta(days=14)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PAUSE_RANGE = (1.5, 3.0)
BLOCK_SLEEP = 600  # 10 минут на 429/403
BLOCK_LIMIT = 3    # три блокировки подряд — стоп этапа


class StopStage(RuntimeError):
    """Три 429/403 подряд: этап нужно остановить и доложить человеку."""


def now_utc():
    return datetime.now(timezone.utc)


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return now_utc() - mtime < CACHE_TTL


class PoliteClient:
    """Один поток, пауза 1.5–3 с перед каждым запросом, нормальный UA.

    На 429/403 — ждём 10 минут и повторяем один раз. Три блокировки подряд
    (по любым URL) — StopStage.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA
        self.blocked_in_row = 0

    def get(self, url: str) -> requests.Response:
        for attempt in (1, 2):
            time.sleep(random.uniform(*PAUSE_RANGE))
            r = self.session.get(url, timeout=30)
            if r.status_code not in (403, 429):
                self.blocked_in_row = 0
                return r
            self.blocked_in_row += 1
            log(f"HTTP {r.status_code} на {url} (подряд: {self.blocked_in_row})")
            if self.blocked_in_row >= BLOCK_LIMIT:
                raise StopStage(f"{BLOCK_LIMIT} блокировки подряд, последняя на {url}")
            if attempt == 1:
                log(f"пауза {BLOCK_SLEEP // 60} минут, потом один повтор")
                time.sleep(BLOCK_SLEEP)
        return r


def parse_count(s):
    """'1.2K' -> 1200, '3.4M' -> 3400000, '4 412' -> 4412. Нет числа -> None."""
    if not s:
        return None
    s = s.strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    m = re.match(r"^([\d.]+)([KkMm]?)", s)
    if not m:
        return None
    mult = {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2).lower()]
    return int(round(float(m.group(1)) * mult))


REJECTED_FIELDS = ["username", "reason", "details", "stage", "date"]


def add_rejected(username, reasons, stage):
    """Пишет (или перезаписывает) строки канала в data/rejected.csv.

    reasons — список (reason, details).
    """
    rows = []
    if REJECTED_CSV.exists():
        with REJECTED_CSV.open(encoding="utf-8", newline="") as f:
            rows = [r for r in csv.DictReader(f)
                    if not (r["username"] == username and r["stage"] == stage)]
    today = now_utc().date().isoformat()
    for reason, details in reasons:
        rows.append({"username": username, "reason": reason, "details": details,
                     "stage": stage, "date": today})
    DATA.mkdir(parents=True, exist_ok=True)
    with REJECTED_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REJECTED_FIELDS)
        w.writeheader()
        w.writerows(rows)


def clear_rejected(username, stage):
    add_rejected(username, [], stage)
