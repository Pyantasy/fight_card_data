# fight_card_data

Weekly job that copies UFC fight cards from the [UFCalendar Fight API](https://www.ufcalendar.com/developers) (free tier) into `data/fights.json`, committed to this repo by GitHub Actions. The Fight Card app reads that file; there is no server and no database. This replaces the old Heroku Flask + MySQL + FightingTomatoes/ESPN scraper stack.

## How it runs

- `.github/workflows/weekly-refresh.yml`: Mondays 14:00 UTC, plus manual dispatch (modes: `refresh`, `fetch-only`, `dry-run`). It runs the tests, runs `refresh_fights.py`, and commits `data/fights.json` as `github-actions[bot]` only when the fights changed. The job has `contents: write` and nothing else.
- `refresh_fights.py`: standard library only (Python 3.12).
- `data/fights.json`: the output. Same shape the app decoded from the old `/api/fights` response:

```json
{
  "status": "success",
  "count": 10,
  "fights": [
    {
      "id": 301,
      "date": "2026-11-14",
      "promotion": "UFC",
      "event": "UFC Sample Night: Sample vs. Example",
      "main_or_prelim": "main",
      "card_placement": "1",
      "fighter_1": "Alex Sample",
      "fighter_2": "Brian Example",
      "weightclass": "Welterweight",
      "rounds": 5,
      "is_championship": 0
    }
  ],
  "timestamp": "2026-11-26T12:00:00+00:00"
}
```

Fights are sorted by date, then event, then main card before prelims, then `card_placement` (1 = headliner). `id` is the UFCalendar bout id and is stable across weeks. `date` is the event's local date (`YYYY-MM-DD`). `timestamp` only changes when the fights change. The file holds the refresh window (10 days back, 28 days ahead); the old table kept growing, this one does not. Cancelled bouts and bouts with a TBA corner are left out.

## Request budget (UFCalendar free tier: 100/month)

Per run: 1 event-list call (window = 10 days back, 28 days ahead) + 1 call per event card, usually 4 to 6 cards. Typical run is 5 to 7 requests, hard cap 10 (`--max-requests` cannot go above 10). 5 Mondays in a month is at most 50 requests. If the `X-RateLimit-Remaining` header shows fewer than 15 requests left, the run aborts without writing anything. Each run logs `upstream requests this run: N`.

## Secrets (GitHub Actions)

Only `UFCALENDAR_API_KEY`, read from the environment and never printed. The commit uses the workflow's built-in `GITHUB_TOKEN`; no other secret is needed.

## Local checks

```
python3 -m unittest discover -s tests     # parsing, budget and output tests, offline
python3 refresh_fights.py --dry-run       # offline, parses tests/fixtures, writes nothing
UFCALENDAR_API_KEY=... python3 refresh_fights.py --no-write --save-raw /tmp/raw   # live fetch, writes nothing
```

`tests/fixtures` are hand-built to the published OpenAPI schema (invented fighters). Replace them with real responses saved by `--save-raw` after the first live run.
