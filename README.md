# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # or export MONGO_URI / MONGO_DB
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` are read from the environment; a local `.env` is only a fallback and never
overrides real environment variables. Indexes are created at startup. MongoDB 6.0+ is required
(`$densify`, `$setWindowFields`).

## What is implemented

Every endpoint in `openapi.yaml`. Part A findings are in `REVIEW.md`, Part C answers in `DECISIONS.md`.

## Where the contract left a choice

- **Trend range**: "exceeds 92 days" is read as more than 92 calendar days inclusive, so 1 June - 31 August is the longest allowed range.
- **Punch-out on a closed record**: 409 is checked before the 422 time checks.
- **`moving_avg_7d`**: the mean of the already rounded `attendance_rate` values in the window.
- **Concurrent corrections**: the PATCH is an optimistic update; the request that loses the race gets 409 and no history entry is lost.
- **A correction that "changes nothing"**: one where `status`, `punch_in` and `punch_out` all end up equal to the stored values.
- **Rounding**: MongoDB's `$round` is half-even, so half-up rounding inside pipelines is done with whole-number arithmetic.

## Tests

`tests/selftest.py` seeds about 96,000 records (ties, weekend records, employees with no logs, mid-month
joiners, overnight shifts, legacy documents, logs of unknown employees) and compares each analytics endpoint
with a plain-Python reference, then checks the rules, error codes, races and explain plans. It wipes the
database it is pointed at, so it only runs against a `MONGO_DB` ending in `_selftest`; usage is at the top of
the file. Developed and tested on MongoDB 8.2; not run against 7.0.
