# REVIEW.md

Defects found in the starter `app/main.py`. "Shift" means the default 09:30 - 18:30 unless stated.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Builds the shift start with `punch_in.replace(hour=..)` on whatever clock the datetime carries (server-local / UTC), not IST. | On a UTC server, punch-in 10:05 IST is 04:35; start becomes 09:30 on that same clock, the difference is negative, `late_minutes` = 0 instead of 35. It only works when the server's own time zone happens to be IST. | `shift_window()` builds the shift start as an IST instant from the attendance date; the subtraction is between two aware datetimes. |
| 2 | `compute_late_minutes` | Grace is checked on truncated minutes (`minutes > 10`), so "strictly more than 10:00" is lost. | 09:40:01 gives 0; R2 says 10. | Compare seconds: late if `seconds > 600`, then `seconds // 60`. |
| 3 | `compute_late_minutes` | No overnight handling: the shift start is always put on the punch-in's own calendar day. | Shift 22:00 - 06:00, punch-in 01:00: starter gives 0, correct is 180 (the shift started the previous day). | Shift start is derived from the R1 attendance date, not from the punch-in's date. |
| 4 | `compute_work_hours` | `round()` on a binary float is neither half-up nor exact. | 4 h 00 min 18 s (14418 s = 4.005 h) gives 4.0; R4 says 4.01. 300 such durations between 4 h and 10 h. | `Decimal(seconds) / 3600` quantized with `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | No 30-minute minimum. | Punch-out 18:35 gives 5; R3 says 0 (sample document 1). | Return 0 below 30 minutes. |
| 6 | `compute_overtime` | Shift end is always on the record's date, wrong for overnight shifts. | Sample document 7 (22:00 - 06:00, out 06:40 next day): starter gives 1480, correct is 40. | `shift_window()` moves the end to the next day when `shift_end <= shift_start`. |
| 7 | all three helpers | Instants are never truncated to whole seconds (R1). | Punch-in 09:40:00.900 is stored and returned with the 900 ms. | `from_ms()` drops the milliseconds before anything is computed or stored. |
| 8 | `health` | Always answers 200; MongoDB is never contacted. | Stop MongoDB: `/health` still says ok. | `ping` the server; 503 on failure. Connection failures elsewhere also map to 503, not 500. |
| 9 | `create_employee` | Check-then-insert with no unique index: a race creates duplicates. | 12 parallel creates of the same `emp_code` give several 201s. | Unique index on `emp_code`; insert and turn `DuplicateKeyError` into 409. |
| 10 | `EmployeeIn` | No validation at all. | `emp_code: "x"`, `email: "nope"`, `shift_start: "25:99"`, `joined_on: "tomorrow"`, `shift_start == shift_end` are all accepted with 201. | Patterns and lengths from the contract, a real-calendar-date check, and a validator that the shift times differ. |
| 11 | `create_employee` | `created_at = datetime.now()` is naive local time (PyMongo stores it as if it were UTC, so it is 5:30 off on an IST machine), and it is returned as a string, not epoch ms. | Response has `"created_at": "2026-...T..."`. | `datetime.now(UTC)`, and `employee_out()` converts to epoch ms. |
| 12 | `list_employees` | `skip = page * page_size`: page 1 skips the first page. | With 6 employees, `page=1&page_size=20` returns nothing. | `(page - 1) * page_size`. |
| 13 | `list_employees` | `total` counts the whole collection, ignoring the filter (R10). | `?department=Sales` reports the total of all departments. | `count_documents(q)` with the same filter. |
| 14 | `list_employees` | No sort, and `page` / `page_size` are unbounded. | Order is whatever MongoDB returns; `page=-1` gives a negative skip and a 500; `page_size=1000000` is accepted. | Sort by `emp_code`; `page >= 1`, `1 <= page_size <= 100` (422 otherwise). |
| 15 | `punch_in` | Unknown employee: `emp` is `None` and `emp["shift_start"]` raises. | `emp_code: "EMP9999"` gives 500, not 404. | `get_employee_or_404()`. |
| 16 | `punch_in` | `datetime.fromtimestamp(ms / 1000)` and `datetime.now()` are naive server-local time, and the date is that clock's date, not the IST date. | On a UTC server a punch-in at 00:10 IST lands on the previous day; the stored instant is off by the server's UTC offset. | Aware UTC instants; the date comes from `attendance_date()` in IST. |
| 17 | `punch_in` | The overnight rule of R1 is missing. | Shift 22:00 - 06:00, punch-in 01:00 on the 3rd is filed under the 3rd instead of the 2nd. | `attendance_date()` moves an early-morning punch-in of an overnight shift to the previous day. |
| 18 | `punch_in` | Check-then-insert race, no unique index. | 16 parallel identical punch-ins give several 201s and duplicate records. | Unique index on (`emp_code`, `date`); insert and map `DuplicateKeyError` to 409. |
| 19 | `PunchInIn` / `punch_in` | `punched_at` accepts seconds, floats and numeric strings (lax `int`), `if body.punched_at` treats 0 as "now", and any `status` is accepted. | `punched_at: 1783312500` creates a record in 1970; `"1783312500000"` is accepted; `status: "LEAVE"` creates a leave record with a punch-in. | Strict int with the contract's min/max; `is not None`; `status` limited to PRESENT / WFH / ON_DUTY. |
| 20 | `punch_in`, `list_attendance` | Responses leak MongoDB's `_id` as `id` and return datetimes as strings instead of epoch ms. | `"id": "66..."` in the response; `punch_in` is an ISO string. | One serializer, `record_out()`, with exactly the contract's fields. |
| 21 | `list_attendance` | Loads the entire collection, sorts and pages in Python; no index; no `emp_code` tie-break. | Slow and memory-hungry at 100k records; rows of the same date come back in arbitrary order. | Filter, sort (`date` desc, `emp_code` asc), skip and limit in MongoDB, backed by indexes; `count_documents` for the total. |
| 22 | `list_attendance` | Parameters are not validated. | `page=0` returns a wrong slice; `date_from=banana`, `status=SICK`, `date_from > date_to` all return 200. | Typed and bounded parameters; 422 for a bad date, status or reversed range. |
| 23 | `list_attendance` | Seeded records without `half_day` / `history` are returned without those fields, and dates nested in `history[].changes` are not converted. | Sample document 9 lacks two required fields; sample document 8 shows date strings inside `changes`. | `record_out()` fills the defaults and `history_out()` converts nested dates. |
| 24 | module level | `MongoClient` returns naive datetimes, and no index is created at startup. | Every instant needs a guessed time zone; the grader's database has only the `_id` index. | `tz_aware=True`; `ensure_indexes()` runs in the app's lifespan and is idempotent. |

## Looked at and decided it is not a defect

- **`load_dotenv()`**: its default is `override=False`, so real environment variables already win over `.env`, as the environment contract requires.
- **`MongoClient` created at import time**: it connects lazily and is thread-safe, so one module-level client is the right shape for sync endpoints running in FastAPI's thread pool.
- **`doc.pop("_id", None)` after `insert_one`**: looks odd, but `insert_one` really does add `_id` to the dict it is given, so removing it was correct (the serializer makes it unnecessary now).
- **String comparison of dates** (`$gte` / `$lte` on `YYYY-MM-DD`): ISO dates sort the same as text and as dates, so a range filter on the stored strings is correct.
- **`IST = timezone(timedelta(hours=5, minutes=30))`**: a fixed offset is fine because India has no daylight saving time.
- **`int(seconds / 60)` in the helpers**: truncation equals floor for the non-negative values that matter there; the real problems were the ones listed above.
