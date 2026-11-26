#!/usr/bin/env python3
"""Read-only observer for Poly-ARB runtime snapshots.

Run beside the scanner. It only reads arbs_live.json and scanner_status.json,
then writes its own JSONL observations and a human-readable summary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STOP = False


def _stop_handler(_signum, _frame):
    global STOP
    STOP = True


def now_iso(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(epoch or time.time()).astimezone().isoformat(timespec="seconds")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def active_rows(payload: Any) -> dict[str, dict]:
    """Accept the scanner's slug->snapshot object and compatible list envelopes."""
    if isinstance(payload, dict) and isinstance(payload.get("arbs"), dict):
        payload = payload["arbs"]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = list(payload.values())
    else:
        return {}
    result = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("inactive_snapshot") is True:
            continue
        slug = str(row.get("slug") or "").strip()
        if not slug:
            continue
        bets = row.get("bets") if isinstance(row.get("bets"), list) else []
        pm = next((b for b in bets if isinstance(b, dict) and b.get("source") == "Polymarket"), {})
        bk = next((b for b in bets if isinstance(b, dict) and b.get("source") != "Polymarket"), {})
        # Different directions on one slug are separate signal episodes.
        identity = "|".join((slug, str(pm.get("outcome") or ""),
                              str(bk.get("source") or row.get("bk_source") or ""),
                              str(bk.get("outcome") or "")))
        result[identity] = row
    return result


def snapshot_record(row: dict) -> dict:
    """Keep analysis-relevant quote/depth fields; omit UI/debug-only payloads."""
    keys = (
        "slug", "q", "sport", "market_type", "phase", "phase_source",
        "roi", "bank", "total_bank", "stakes", "currency", "profit", "payout", "ts",
        "pm_age", "pm_best_ask", "pm_best_bid", "pm_book_snapshot",
        "bk_source", "bk_odds_age_sec", "bk_binding",
    )
    result = {key: row[key] for key in keys if key in row}
    bets = row.get("bets")
    if isinstance(bets, list):
        result["bets"] = [
            {key: bet[key] for key in (
                "source", "outcome", "o", "odds", "pm_best_ask", "pm_avg_ask",
                "pm_best_bid", "pm_book_age_sec", "pm_book_age_source", "pm_age",
                "odds_age_sec", "token", "stake", "stake_usdt", "pm_fee_enabled",
                "pm_fee_rate", "pm_fee_exponent", "pm_fee_source",
            ) if key in bet}
            for bet in bets if isinstance(bet, dict)
        ]
    return result


def signal_key(row: dict) -> str:
    rows = active_rows([row])
    return next(iter(rows), "")


def snapshot_fingerprint(row: dict) -> str:
    canonical = snapshot_record(row)
    canonical.pop("ts", None)  # calculation timestamp alone is not a quote change
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def recent_drop_for(slug: str, status: dict, episode_start: float, now: float) -> dict | None:
    drops = status.get("recent_signal_drops")
    if not isinstance(drops, list):
        return None
    candidates = []
    for row in drops:
        if not isinstance(row, dict) or str(row.get("slug") or "") != slug:
            continue
        try:
            dropped_at = float(row.get("dropped_at"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(dropped_at) and episode_start <= dropped_at <= now + 2 and now - dropped_at <= 300:
            candidates.append((dropped_at, row))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def summarize_jsonl(path: Path, active_count: int = 0, skipped_stale: int = 0) -> str:
    starts = []
    ends = []
    snapshots = 0
    health = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = event.get("event")
                if kind == "fork_start":
                    starts.append(event)
                elif kind == "fork_end":
                    ends.append(event)
                elif kind == "fork_snapshot":
                    snapshots += 1
                elif kind == "collector_health":
                    health.append(event)
    except OSError:
        pass
    phases = {}
    for event in starts:
        phase = str(event.get("phase") or "unknown").lower()
        phases[phase] = phases.get(phase, 0) + 1
    reasons = {}
    durations = []
    for event in ends:
        reason = str(event.get("reason") or "unknown")
        reasons[reason] = reasons.get(reason, 0) + 1
        if isinstance(event.get("duration_sec"), (int, float)):
            durations.append(float(event["duration_sec"]))
    mean = sum(durations) / len(durations) if durations else None
    lines = [
        f"Poly-ARB: сводка наблюдения — {now_iso()}",
        f"Найдено эпизодов за эту сессию: {len(starts)}",
        f"Снимков изменений вилок записано: {snapshots}",
        f"Сейчас активных по последнему наблюдению: {active_count}",
        "Фаза при первом обнаружении: " + (", ".join(f"{k}: {v}" for k, v in sorted(phases.items())) or "пока нет данных"),
        f"Завершённых эпизодов с известной длительностью: {len(durations)}",
        "Средняя длительность завершённых эпизодов: " + (f"{mean:.1f} сек" if mean is not None else "пока нет данных"),
        "Причины снятия: " + (", ".join(f"{k}: {v}" for k, v in sorted(reasons.items())) or "пока нет данных"),
        f"Опросов пропущено из-за устаревшего статуса сканера: {skipped_stale}",
        "Примечание: длительность оценена по локальным наблюдениям, это не точное время жизни и не подтверждение исполнения ставки.",
    ]
    if health:
        lines.append("Последний статус наблюдателя: " + str(health[-1].get("message") or "ok"))
    return "\n".join(lines) + "\n"


def format_event(event: dict) -> str:
    kind = event.get("event")
    at = event.get("observed_at") or now_iso()
    if kind == "fork_start":
        return (f"\n[{at}] НАЙДЕНА ВИЛКА ({str(event.get('phase') or 'unknown').upper()})\n"
                f"Матч: {event.get('q') or event.get('slug')}\n"
                f"Рынок: {event.get('slug')} | тип: {event.get('market_type') or '—'}\n"
                "Начало длительности: первое наблюдение сборщика; сигнал мог появиться раньше.\n")
    if kind == "fork_snapshot":
        row = event.get("snapshot") or {}
        bets = row.get("bets") if isinstance(row.get("bets"), list) else []
        pm = next((b for b in bets if b.get("source") == "Polymarket"), {})
        bk = next((b for b in bets if b.get("source") != "Polymarket"), {})
        roi = row.get("roi")
        roi_text = f"{float(roi) * 100:+.2f}%" if isinstance(roi, (int, float)) else "—"
        return (f"[{at}] СНИМОК / ИЗМЕНЕНИЕ КОТИРОВОК\n"
                f"{row.get('slug') or event.get('identity')} | ROI {roi_text} | фаза {row.get('phase') or 'unknown'}\n"
                f"Polymarket: исход {pm.get('outcome') or '—'}, кэф {pm.get('o', '—')}, "
                f"лучший ask {pm.get('pm_best_ask', row.get('pm_best_ask', '—'))}, "
                f"средний ask {pm.get('pm_avg_ask', '—')}, возраст стакана {pm.get('pm_book_age_sec', row.get('pm_age', '—'))} сек.\n"
                f"БК {bk.get('source') or row.get('bk_source') or '—'}: исход {bk.get('outcome') or '—'}, "
                f"кэф {bk.get('o', bk.get('odds', '—'))}, возраст котировки {bk.get('odds_age_sec', row.get('bk_odds_age_sec', '—'))} сек.\n")
    if kind == "fork_end":
        details = event.get("reason_details") or {}
        return (f"[{at}] СИГНАЛ СНЯТ\nМатч: {event.get('slug')} | причина: {event.get('reason') or 'unknown'}\n"
                f"Оценка длительности: {event.get('duration_sec', '—')} сек. "
                f"Снимков: {event.get('snapshots', 0)}.\n"
                f"ROI до снятия: {details.get('previous_roi_pct', '—')}%; "
                f"лучший ROI при снятии: {details.get('current_best_roi_pct', '—')}%.\n")
    if kind == "collector_health":
        return f"[{at}] СТАТУС НАБЛЮДАТЕЛЯ: {event.get('message', '')}\n"
    if kind == "collector_start":
        return f"[{at}] СБОР НАЧАТ | интервал {event.get('interval_sec')} сек. | только чтение файлов сканера.\n"
    if kind == "collector_stop":
        return f"[{at}] СБОР ОСТАНОВЛЕН | активных сигналов на остановке: {event.get('active_at_stop', 0)}.\n"
    return f"[{at}] {json.dumps(event, ensure_ascii=False, default=str)}\n"


class Observer:
    def __init__(self, log_path: Path, summary_path: Path, *, jsonl_path: Path | None = None,
                 max_status_age: float = 10.0):
        self.log_path = log_path
        self.summary_path = summary_path
        self.jsonl_path = jsonl_path or log_path.with_suffix(".jsonl")
        self.max_status_age = max_status_age
        self.episodes: dict[str, dict] = {}
        self.missing_counts: dict[str, int] = {}
        self.last_fingerprint: dict[str, str] = {}
        self.skipped_stale = 0
        self.last_health_message = None

    def emit(self, event: dict):
        event = {"observed_at": now_iso(), **event}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.jsonl_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
        with self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(format_event(event) + "\n")

    def poll(self, live_path: Path, status_path: Path, now: float | None = None) -> bool:
        now = time.time() if now is None else float(now)
        status = read_json(status_path)
        if not isinstance(status, dict):
            self._health("scanner_status.json недоступен или повреждён")
            return False
        try:
            status_ts = float(status.get("ts"))
        except (TypeError, ValueError):
            status_ts = 0.0
        state = str(status.get("state") or "unknown")
        if not math.isfinite(status_ts) or now - status_ts > self.max_status_age or state != "running":
            self.skipped_stale += 1
            self._health(f"сканер не подтверждён как свежий: state={state}, возраст статуса={max(0.0, now-status_ts):.1f} сек")
            self.write_summary()
            return False

        live = read_json(live_path)
        if not isinstance(live, (dict, list)):
            self._health("arbs_live.json недоступен или повреждён; эпизоды не закрываются")
            self.write_summary()
            return False
        rows = active_rows(live)
        current = set(rows)
        for key, row in rows.items():
            episode = self.episodes.get(key)
            if episode is None:
                episode = {
                    "slug": str(row.get("slug") or ""),
                    "first_observed_at": now,
                    "last_observed_at": now,
                    "snapshot_count": 0,
                    "phase": str(row.get("phase") or "unknown"),
                }
                self.episodes[key] = episode
                self.emit({"event": "fork_start", "identity": key, "slug": episode["slug"],
                           "phase": episode["phase"], "market_type": row.get("market_type"),
                           "q": row.get("q"), "start_basis": "first_local_observation"})
            episode["last_observed_at"] = now
            self.missing_counts.pop(key, None)
            fingerprint = snapshot_fingerprint(row)
            if self.last_fingerprint.get(key) != fingerprint:
                self.emit({"event": "fork_snapshot", "identity": key, "snapshot": snapshot_record(row)})
                episode["snapshot_count"] += 1
                self.last_fingerprint[key] = fingerprint
        for key in list(self.episodes):
            if key in current:
                continue
            self.missing_counts[key] = self.missing_counts.get(key, 0) + 1
            if self.missing_counts[key] < 2:
                continue
            episode = self.episodes.pop(key)
            self.missing_counts.pop(key, None)
            self.last_fingerprint.pop(key, None)
            drop = recent_drop_for(episode["slug"], status, episode["first_observed_at"], now)
            reason = str(drop.get("reason") or "unknown") if drop else "unknown"
            dropped_at = drop.get("dropped_at") if drop else None
            end_at = float(dropped_at) if isinstance(dropped_at, (int, float)) and episode["first_observed_at"] <= dropped_at <= now + 2 else now
            self.emit({"event": "fork_end", "identity": key, "slug": episode["slug"],
                       "phase": episode["phase"], "reason": reason,
                       "reason_details": drop, "duration_sec": round(max(0.0, end_at - episode["first_observed_at"]), 2),
                       "duration_basis": "observed_interval_estimate",
                       "snapshots": episode["snapshot_count"]})
        self.write_summary()
        return True

    def _health(self, message: str):
        if message != self.last_health_message:
            self.emit({"event": "collector_health", "message": message})
            self.last_health_message = message

    def write_summary(self):
        self.summary_path.parent.mkdir(parents=True, exist_ok=True)
        text = summarize_jsonl(self.jsonl_path, active_count=len(self.episodes), skipped_stale=self.skipped_stale)
        self.summary_path.write_text(text, encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only сбор статистики активных вилок и снятых сигналов")
    parser.add_argument("--interval", type=float, default=1.0, help="частота чтения snapshot, секунды (по умолчанию 1)")
    parser.add_argument("--max-status-age", type=float, default=10.0, help="максимальный возраст scanner_status для учёта")
    parser.add_argument("--live", type=Path, default=ROOT / "arbs_live.json")
    parser.add_argument("--status", type=Path, default=ROOT / "scanner_status.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "fork_observer_logs")
    args = parser.parse_args(argv)
    if args.interval < 0.2 or args.max_status_age < args.interval:
        parser.error("--interval должен быть >= 0.2, а --max-status-age не меньше интервала")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    session = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    observer = Observer(args.output_dir / f"observations_{session}.txt",
                        args.output_dir / f"summary_{session}.txt",
                        max_status_age=args.max_status_age)
    observer.emit({"event": "collector_start", "interval_sec": args.interval,
                   "live_file": str(args.live), "status_file": str(args.status),
                   "duration_note": "episodes begin at first local observation"})
    print("[observer] Только чтение arbs_live.json/scanner_status.json; журнал:", observer.log_path)
    print("[observer] Остановить: Ctrl+C. Сканер запускать отдельно, если он ещё не запущен.")
    signal.signal(signal.SIGINT, _stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop_handler)
    try:
        while not STOP:
            observer.poll(args.live, args.status)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    observer.emit({"event": "collector_stop", "active_at_stop": len(observer.episodes),
                   "note": "активные эпизоды не закрываются искусственно"})
    observer.write_summary()
    print("[observer] Сводка сохранена:", observer.summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
