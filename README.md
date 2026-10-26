# CRM → SQLite → LLM → Automated Report Pipeline

A small, dependency-free pipeline that turns raw CRM records into a daily
report and an HTML dashboard — unattended, on a schedule, without producing
duplicates when it re-runs.

Built as a working answer to a real brief: *"data extraction, cleaning, Claude
processing, and scheduled report generation. No manual report generation."*

```
GoHighLevel v2 API  (cursor pagination, hard page cap)
      │  contacts / opportunities
      ▼
clean.py   → normalise, dedupe (email → phone), repair missing fields,
             drop corrupt timestamps and orphan opportunities
      ▼
store.py   → SQLite upsert, ON CONFLICT(id) DO UPDATE  (idempotent by design)
      ▼
analyze.py → Claude API when a key is present, deterministic rules otherwise
      ▼
report.py  → out/report_YYYY-MM-DD.md  +  out/dashboard.html
      ▼
run_log    → per-run counters, so you can audit the job without opening a log file
```

## Quick start

No third-party dependencies. Python 3.10+.

```bash
python pipeline.py              # run once
python pipeline.py --loop 3600  # run every hour
python pipeline.py --reset      # wipe the database and start over
python tests/test_smoke.py      # 4 assertions, ~2s
```

On Windows you can double-click `run.bat`.

With no credentials configured it generates a synthetic CRM fixture with
deliberately dirty data (duplicate contacts, missing fields, corrupt
timestamps, orphan opportunities, implausible deal sizes) so the whole
pipeline is demonstrable and testable offline.

## Data source resolution

| Condition | Source |
|---|---|
| `GHL_TOKEN` + `GHL_LOCATION_ID` set | GoHighLevel v2 API |
| `data/export.json` present | local export |
| neither | synthetic fixture |

## Three things worth pointing at

### 1. Idempotency is enforced by the database, not by care

```
run 1 → contacts 166 seen → 140 new, 26 duplicates removed
run 2 → contacts 166 seen →   0 new, 26 duplicates removed
```

The second run inserts nothing. That comes from the primary key plus
`ON CONFLICT DO UPDATE` — not from application-level checks — so retries,
network hiccups and manual re-runs cannot create duplicate rows.

### 2. Data hygiene is a first-class stage

| Check | Handling |
|---|---|
| Duplicate email / phone (case, whitespace) | normalise, then dedupe on email, falling back to phone |
| Missing email / phone / stage | counted, defaulted, pipeline continues |
| Corrupt timestamps (1970, 2099, empty) | treated as missing, excluded from "last 7 days" |
| Implausible deal value (> $100k) | **excluded from the pipeline total**, listed separately |
| Orphan opportunities (contact missing) | skipped and flagged |

The last row is why the numbers in the report are usable:

```
naive sum          $1,674,800
hygiene applied    $  174,800      (29 anomalies flagged)
```

A dashboard built on the naive sum would have been wrong by an order of
magnitude.

### 3. The LLM is optional and narrowly scoped

The model tags opportunities (`high_value`, `stalled`, `follow_up_needed`) and
writes a short narrative observation. It does **not** sum money, deduplicate,
or count rows — SQL does that.

If `ANTHROPIC_API_KEY` is absent, `analyze()` falls back to deterministic rules
and the pipeline still produces a complete report. A reporting job should not
stop because a model endpoint is unavailable.

## Configuration

Copy `.env.example` to `.env` and fill in what you have. Both credential pairs
are optional.

## Tests

```
$ python tests/test_smoke.py
test_end_to_end                ok  end-to-end: 140 contacts, 57 opportunities
test_idempotent                ok  idempotent: first=140 new, second=0 new
test_dedupe_and_hygiene        ok  hygiene: 166 seen, 26 duplicates removed, 140 stored
test_anomalous_amounts_excluded ok  anomalies excluded: naive $1,674,800 -> clean $174,800

4/4 passed
```

`test_idempotent` is the one that matters: it runs the pipeline twice against
the same input and asserts that the second run inserts zero rows.

## Known limitations

Honest list — these are next-phase items, not oversights:

- **No incremental watermark.** Every run pulls everything. At scale, filter on
  `updatedAt` and store a cursor.
- **No retry / backoff.** Production needs exponential backoff, a dead-letter
  record, and an alert on repeated failure.
- **The dashboard is static HTML**, not a time-series chart.
- **The synthetic fixture's dirt is invented.** Real CRM data is messier:
  merged contacts, custom fields, timezone drift, deleted records.
- **`fetch_from_ghl()` targets the v2 endpoint shape** (`startAfterId` cursor,
  `Version` header). Confirm field and endpoint names against the current API
  docs before pointing it at a live account.

## Layout

```
pipeline.py          entry point + all stages (single module, ~600 lines)
tests/test_smoke.py  4 assertions, no pytest needed
data/                drop export.json here for offline runs
out/                 generated report + dashboard
run.bat              Windows launcher
```

## License

MIT — see `LICENSE`.
