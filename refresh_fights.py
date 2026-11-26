#!/usr/bin/env python3
"""Weekly UFC fight-card refresh: UFCalendar Fight API -> data/fights.json.

Stdlib only. The only secret is read from the environment and never printed:
  UFCALENDAR_API_KEY

The output file has the same shape the app decodes from the old /api/fights response:
  {"status": "success", "count": N, "fights": [...], "timestamp": "..."}

Modes:
  (default)    fetch from UFCalendar and write data/fights.json (only if the fights changed)
  --no-write   fetch from UFCalendar, parse and validate, do not touch the file
  --dry-run    no network, no secrets: parse tests/fixtures and print what would be written
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("refresh_fights")

API_BASE = "https://api.ufcalendar.com"
FIXTURE_DIR = Path(__file__).parent / "tests" / "fixtures"
FIXTURE_TODAY = date(2026, 11, 26)

DEFAULT_LOOKBACK_DAYS = 10
DEFAULT_LOOKAHEAD_DAYS = 28
DEFAULT_MAX_REQUESTS = 10
HARD_MAX_REQUESTS = 10
# Free plan is 100 requests/month. Stop calling upstream if fewer than this remain.
QUOTA_RESERVE = 15
# Free plan is 10 requests/minute; ~8.5/min leaves headroom.
MIN_SECONDS_BETWEEN_REQUESTS = 7.0

SKIPPED_EVENT_STATUSES = {"cancelled", "postponed"}
SKIPPED_BOUT_STATUSES = {"cancelled"}
DEFAULT_ROUNDS = 3  # what the old pipeline assumed when the source had no round count
OUTPUT_PATH = Path(__file__).parent / "data" / "fights.json"
MAIN_SECTIONS = {"main"}
PRELIM_SECTIONS = {"prelims", "early_prelims"}
MAIN_FALLBACK_MAX_ORDERING = 5


class BudgetExceeded(RuntimeError):
    pass


class QuotaLow(RuntimeError):
    pass


class ParseError(ValueError):
    pass


@dataclass
class Budget:
    """Counts upstream requests and refuses to exceed the per-run cap."""

    max_requests: int
    used: int = 0
    quota_remaining: Optional[int] = None

    def spend(self) -> None:
        if self.used >= self.max_requests:
            raise BudgetExceeded(f"per-run request cap of {self.max_requests} reached")
        if self.quota_remaining is not None and self.quota_remaining < QUOTA_RESERVE:
            raise QuotaLow(
                f"only {self.quota_remaining} monthly requests left (reserve {QUOTA_RESERVE})"
            )
        self.used += 1


class UfcalendarClient:
    """Live client. Every call goes through Budget.spend()."""

    def __init__(self, api_key: str, budget: Budget, raw_dir: Optional[Path] = None):
        self._api_key = api_key
        self._budget = budget
        self._raw_dir = raw_dir
        self._last_request_at = 0.0

    def get(self, path: str, params: dict[str, str]) -> Any:
        self._budget.spend()
        wait = MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - self._last_request_at)
        if self._last_request_at and wait > 0:
            time.sleep(wait)
        url = f"{API_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                remaining = resp.headers.get("X-RateLimit-Remaining")
                body = json.load(resp)
        except urllib.error.HTTPError as err:
            raise RuntimeError(f"UFCalendar {path} failed with HTTP {err.code}") from None
        finally:
            self._last_request_at = time.monotonic()
        if remaining is not None and remaining.isdigit():
            self._budget.quota_remaining = int(remaining)
        if self._raw_dir is not None:
            self._raw_dir.mkdir(parents=True, exist_ok=True)
            name = path.strip("/").replace("/", "_") + ".json"
            (self._raw_dir / name).write_text(json.dumps(body, indent=2))
        return body


class FixtureClient:
    """Offline stand-in that serves saved sample responses; counts requests like the live client."""

    def __init__(self, budget: Budget, fixture_dir: Path = FIXTURE_DIR):
        self._budget = budget
        self._dir = fixture_dir

    def get(self, path: str, params: dict[str, str]) -> Any:
        self._budget.spend()
        if path == "/v1/events":
            name = "events_list.json"
        else:
            name = f"event_{path.rsplit('/', 1)[-1]}.json"
        return json.loads((self._dir / name).read_text())


def _require(obj: dict, key: str, ctx: str) -> Any:
    if key not in obj or obj[key] is None:
        raise ParseError(f"{ctx}: missing required field '{key}'")
    return obj[key]


def _parse_ts(value: str, ctx: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as err:
        raise ParseError(f"{ctx}: bad timestamp {value!r}") from err


def local_event_date(starts_at: datetime, tz_name: Optional[str]) -> date:
    if tz_name:
        try:
            return starts_at.astimezone(ZoneInfo(tz_name)).date()
        except ZoneInfoNotFoundError:
            log.warning("unknown timezone %r, falling back to UTC date", tz_name)
    return starts_at.astimezone(timezone.utc).date()


def parse_event_list(payload: dict) -> tuple[list[dict], bool]:
    """Return (events, has_more) from a GET /v1/events response."""
    data = _require(payload, "data", "events list")
    if not isinstance(data, list):
        raise ParseError("events list: 'data' is not a list")
    has_more = bool(payload.get("meta", {}).get("pagination", {}).get("has_more"))
    events = []
    for raw in data:
        ctx = f"event {raw.get('id', '?')}"
        _require(raw, "id", ctx)
        _require(raw, "title", ctx)
        _require(raw, "starts_at", ctx)
        _require(raw, "status", ctx)
        events.append(raw)
    return events, has_more


def select_events_for_cards(
    events: list[dict], today: date, max_cards: int
) -> list[dict]:
    """Drop cancelled/postponed cards; if over budget keep the ones closest to today."""
    live = [e for e in events if e["status"] not in SKIPPED_EVENT_STATUSES]

    def distance(event: dict) -> float:
        start = _parse_ts(event["starts_at"], f"event {event['id']}")
        return abs((start.date() - today).days)

    chosen = sorted(live, key=distance)[:max_cards]
    return sorted(chosen, key=lambda e: e["starts_at"])


def section_to_main_or_prelim(section: Optional[str], is_main: bool, ordering: Optional[int]) -> str:
    if section in MAIN_SECTIONS:
        return "main"
    if section in PRELIM_SECTIONS:
        return "prelim"
    if is_main:
        return "main"
    if ordering is not None and ordering <= MAIN_FALLBACK_MAX_ORDERING:
        return "main"
    return "prelim"


def parse_event_detail(payload: dict, synced_at: datetime) -> tuple[dict, list[dict], int]:
    """Return (event_row, fight_rows, skipped_bouts) from GET /v1/events/{id}."""
    ev = _require(payload, "data", "event detail")
    ctx = f"event {ev.get('id', '?')}"
    starts_at = _parse_ts(_require(ev, "starts_at", ctx), ctx)
    venue = ev.get("venue") or {}

    def opt_ts(key: str) -> Optional[str]:
        value = ev.get(key)
        return _parse_ts(value, ctx).isoformat() if value else None

    event_row = {
        "ufcalendar_id": _require(ev, "id", ctx),
        "slug": _require(ev, "slug", ctx),
        "title": _require(ev, "title", ctx),
        "numbering": ev.get("numbering"),
        "org": ev.get("org") or "ufc",
        "starts_at": starts_at.isoformat(),
        "event_date": local_event_date(starts_at, ev.get("tz")).isoformat(),
        "main_card_at": opt_ts("main_card_at"),
        "prelims_at": opt_ts("prelims_at"),
        "early_prelims_at": opt_ts("early_prelims_at"),
        "tz": ev.get("tz"),
        "status": _require(ev, "status", ctx),
        "is_ppv": bool(ev.get("is_ppv")),
        "is_title_card": bool(ev.get("is_title_card")),
        "venue_name": venue.get("name"),
        "venue_city": venue.get("city"),
        "venue_region": venue.get("region"),
        "venue_country": venue.get("country"),
        "card_synced_at": synced_at.isoformat(),
    }

    fights: list[dict] = []
    skipped = 0
    seen: set[int] = set()
    for bout in ev.get("card") or []:
        fctx = f"{ctx} fight {bout.get('id', '?')}"
        fighter_a, fighter_b = bout.get("fighter_a"), bout.get("fighter_b")
        if not fighter_a or not fighter_b or not fighter_a.get("name") or not fighter_b.get("name"):
            skipped += 1
            continue
        fight_id = _require(bout, "id", fctx)
        if fight_id in seen:
            continue
        seen.add(fight_id)
        result = bout.get("result") or {}
        winner_id = result.get("winner_fighter_id")
        winner = None
        if winner_id is not None:
            winner = {fighter_a.get("id"): fighter_a["name"], fighter_b.get("id"): fighter_b["name"]}.get(
                winner_id
            )
        ordering = bout.get("ordering")
        section = bout.get("card_section")
        fights.append(
            {
                "ufcalendar_id": fight_id,
                "event_ufcalendar_id": event_row["ufcalendar_id"],
                "card_placement": ordering if ordering is not None else 0,
                "card_section": section,
                "main_or_prelim": section_to_main_or_prelim(
                    section, bool(bout.get("is_main")), ordering
                ),
                "fighter_1": fighter_a["name"],
                "fighter_2": fighter_b["name"],
                "weightclass": bout.get("weight_class"),
                "is_championship": bool(bout.get("is_title")),
                "scheduled_rounds": bout.get("scheduled_rounds"),
                "status": bout.get("status"),
                "winner": winner,
                "result_method": result.get("method"),
                "result_method_norm": result.get("method_normalized"),
                "result_round": result.get("round"),
                "result_time": result.get("time"),
            }
        )
    return event_row, fights, skipped


@dataclass
class RunSummary:
    requests_used: int = 0
    quota_remaining: Optional[int] = None
    events: list[dict] = field(default_factory=list)
    fights: list[dict] = field(default_factory=list)
    skipped_bouts: int = 0


def collect(
    client: Any,
    budget: Budget,
    today: date,
    lookback_days: int,
    lookahead_days: int,
    now: datetime,
) -> RunSummary:
    """Fetch the event window and each card; parse into rows. No writes.

    Raises BudgetExceeded or QuotaLow rather than returning a partial result."""
    summary = RunSummary()
    params = {
        "org": "ufc",
        "from": (today - timedelta(days=lookback_days)).isoformat(),
        "to": (today + timedelta(days=lookahead_days)).isoformat(),
        "limit": "100",
    }
    listing, has_more = parse_event_list(client.get("/v1/events", params))
    if budget.quota_remaining is not None and budget.quota_remaining < QUOTA_RESERVE:
        raise QuotaLow(f"only {budget.quota_remaining} monthly requests left (reserve {QUOTA_RESERVE})")
    if has_more:
        log.warning("event list has more than one page; window too wide, extra pages ignored")
    max_cards = max(budget.max_requests - budget.used, 0)
    chosen = select_events_for_cards(listing, today, max_cards)
    live_count = sum(1 for e in listing if e["status"] not in SKIPPED_EVENT_STATUSES)
    if live_count > len(chosen):
        log.warning("per-run cap leaves %d cards unfetched", live_count - len(chosen))
    log.info("window has %d events, fetching %d cards", len(listing), len(chosen))
    for event in chosen:
        payload = client.get(f"/v1/events/{event['id']}", {})
        event_row, fights, skipped = parse_event_detail(payload, now)
        summary.events.append(event_row)
        summary.fights.extend(fights)
        summary.skipped_bouts += skipped
    summary.requests_used = budget.used
    summary.quota_remaining = budget.quota_remaining
    return summary


def to_app_fight(event: dict, fight: dict) -> dict:
    """Map one parsed bout to the row shape the app decodes (old /api/fights row, minus created_at)."""
    placement = fight["card_placement"]
    rounds = fight["scheduled_rounds"]
    return {
        "id": fight["ufcalendar_id"],
        "date": event["event_date"],
        "promotion": event["org"].upper(),
        "event": event["title"],
        "main_or_prelim": fight["main_or_prelim"],
        "card_placement": str(placement),
        "fighter_1": fight["fighter_1"],
        "fighter_2": fight["fighter_2"],
        "weightclass": fight["weightclass"] or "",
        "rounds": rounds if rounds is not None else DEFAULT_ROUNDS,
        "is_championship": 1 if fight["is_championship"] else 0,
    }


def build_fights(summary: "RunSummary") -> list[dict]:
    """Flatten the run into app rows: cancelled bouts dropped, sorted by date, main card first."""
    events = {e["ufcalendar_id"]: e for e in summary.events}
    rows = [
        (fight, to_app_fight(events[fight["event_ufcalendar_id"]], fight))
        for fight in summary.fights
        if fight["status"] not in SKIPPED_BOUT_STATUSES
    ]
    rows.sort(
        key=lambda pair: (
            pair[1]["date"],
            pair[1]["event"],
            pair[1]["main_or_prelim"] != "main",
            pair[0]["card_placement"],
            pair[1]["id"],
        )
    )
    return [row for _, row in rows]


def build_payload(fights: list[dict], generated_at: datetime) -> dict:
    return {
        "status": "success",
        "count": len(fights),
        "fights": fights,
        "timestamp": generated_at.isoformat(timespec="seconds"),
    }


def write_fights_json(path: Path, fights: list[dict], generated_at: datetime) -> bool:
    """Write the payload. Leaves the file untouched (returns False) if the fights are unchanged."""
    if path.exists():
        try:
            if json.loads(path.read_text()).get("fights") == fights:
                return False
        except (json.JSONDecodeError, AttributeError):
            log.warning("%s was unreadable; rewriting it", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(build_payload(fights, generated_at), indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return True


def validate(summary: RunSummary) -> list[str]:
    """Sanity checks used by --dry-run and the tests."""
    problems = []
    if not summary.events:
        problems.append("no events parsed")
    event_ids = {e["ufcalendar_id"] for e in summary.events}
    for fight in summary.fights:
        label = f"fight {fight['ufcalendar_id']}"
        if fight["event_ufcalendar_id"] not in event_ids:
            problems.append(f"{label}: orphaned")
        if fight["main_or_prelim"] not in {"main", "prelim"}:
            problems.append(f"{label}: bad main_or_prelim")
        if not fight["fighter_1"] or not fight["fighter_2"]:
            problems.append(f"{label}: missing fighter name")
    return problems


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="offline: parse saved samples, no secrets, no file written")
    p.add_argument("--no-write", action="store_true", help="fetch live but do not write data/fights.json")
    p.add_argument("--output", type=Path, default=OUTPUT_PATH, help="JSON file to write (default data/fights.json)")
    p.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    p.add_argument("--lookahead-days", type=int, default=DEFAULT_LOOKAHEAD_DAYS)
    p.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    p.add_argument("--save-raw", type=Path, help="save raw upstream responses to this directory")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    started = datetime.now(timezone.utc)
    budget = Budget(max_requests=min(args.max_requests, HARD_MAX_REQUESTS))

    if args.dry_run:
        today = FIXTURE_TODAY
        now = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
        client: Any = FixtureClient(budget)
    else:
        api_key = os.environ.get("UFCALENDAR_API_KEY")
        if not api_key:
            log.error("UFCALENDAR_API_KEY is not set")
            return 2
        today = started.date()
        now = started
        client = UfcalendarClient(api_key, budget, args.save_raw)

    try:
        summary = collect(client, budget, today, args.lookback_days, args.lookahead_days, now)
    except (BudgetExceeded, QuotaLow) as stop:
        log.error("aborting without writing: %s (requests used: %d)", stop, budget.used)
        return 3
    except (RuntimeError, ParseError) as err:
        log.error("aborting without writing: %s (requests used: %d)", err, budget.used)
        return 1

    fights = build_fights(summary)
    problems = validate(summary)
    if not fights:
        problems.append("no fights to write")
    log.info(
        "parsed %d events, %d fights (%d bouts skipped for TBA fighters)",
        len(summary.events),
        len(fights),
        summary.skipped_bouts,
    )
    log.info(
        "upstream requests this run: %d (cap %d), monthly quota remaining: %s",
        summary.requests_used,
        budget.max_requests,
        summary.quota_remaining if summary.quota_remaining is not None else "unknown",
    )
    if problems:
        for problem in problems:
            log.error("validation: %s", problem)
        return 1

    if args.dry_run or args.no_write:
        log.info("%s: not writing %s", "dry-run" if args.dry_run else "no-write", args.output)
        return 0

    changed = write_fights_json(args.output, fights, started)
    log.info("%s %s (%d fights)", "wrote" if changed else "no change to", args.output, len(fights))
    return 0


if __name__ == "__main__":
    sys.exit(main())
