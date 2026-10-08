"""Общее для этапов: пути, вежливый HTTP, кэш, rejected.csv."""
import csv
import json
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


# --- словари и поиск фраз (общие для этапов C и D) ---

import yaml  # noqa: E402


def load_yaml(name):
    return yaml.safe_load((CONFIG / name).read_text(encoding="utf-8"))


def norm(s):
    """Для поиска: нижний регистр, ё -> е. Длина строки не меняется,
    поэтому позиции совпадений годятся для вырезания цитаты из оригинала."""
    # Посимвольно: у редких символов lower() меняет длину (İ -> i̇).
    return "".join(c if len(c.lower()) != 1 else c.lower()
                   for c in (s or "")).replace("ё", "е")


def compile_terms(terms):
    """'фраза' — с начала слова, окончание любое; '=фраза' — целое слово."""
    parts = []
    for t in terms:
        whole = t.startswith("=")
        t = norm(t[1:] if whole else t)
        body = r"\s+".join(re.escape(w) for w in t.split())
        parts.append(r"(?<!\w)" + body + (r"(?!\w)" if whole else ""))
    return re.compile("|".join(parts)) if parts else None


def find_all(rx, text):
    return list(rx.finditer(norm(text))) if rx else []


SENT_END_RX = re.compile(r"[.!?…]+(?=\s|$)|\n")


def quote_around(text, start, end, limit=280):
    """Дословный фрагмент: предложение (или строка) вокруг совпадения.
    Возвращает подстроку оригинального текста, без правок."""
    # Граница предложения — .!?… перед пробелом/концом или перевод строки.
    # Точка внутри слова (.NET, t.me, 2.5) границей не считается.
    bounds = [m.end() for m in SENT_END_RX.finditer(text)]
    left = max([b for b in bounds if b <= start], default=0)
    right = min([b for b in bounds if b >= end], default=len(text))
    if right - left > limit:
        left = max(left, start - limit // 2)
        right = min(right, end + limit // 2)
    return text[left:right].strip()


# --- исключения: seen.json + выгрузка CRM ---

SEEN_JSON = CONFIG / "seen.json"
CRM_CSV = CONFIG / "crm_export.csv"
USERNAME_RX = re.compile(r"(?:t\.me/|telegram\.me/|@)([A-Za-z][A-Za-z0-9_]{3,31})")


def load_seen():
    if not SEEN_JSON.exists():
        return {}
    return json.loads(SEEN_JSON.read_text(encoding="utf-8") or "{}")


def save_seen(seen):
    SEEN_JSON.write_text(json.dumps(seen, ensure_ascii=False, indent=2, sort_keys=True),
                         encoding="utf-8")


def load_crm():
    """Все @каналы из выгрузки CRM: из любой колонки, где есть @ник или t.me/ник."""
    out = set()
    if not CRM_CSV.exists():
        return out
    with CRM_CSV.open(encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            for cell in row:
                out |= {u.lower() for u in USERNAME_RX.findall(cell)}
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", cell.strip()) and \
                        cell.strip().lower() != "channel":
                    out.add(cell.strip().lower())
    return out


def exclusions():
    return {u.lower() for u in load_seen()} | load_crm()
