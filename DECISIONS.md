# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity, duplicate-create race); `(department, emp_code)` for the filtered list and the trend; `(joined_on, department)` for headcount. `attendance_logs`: unique `(emp_code, date)` (one record per day, punch-in race, monthly summary, corrections); `(date desc, emp_code)` for the list sort and month ranges; `(status, date desc, emp_code)` for the status filter. Rejected: a covering `(date, late_minutes, emp_code)` index for the leaderboard; the date index already avoids a collection scan and every extra index slows writes.

2. **Punch-in race.** There is no read first. Both requests compute the same date and both call `insert_one`. The unique index lets one insert succeed; the other raises `DuplicateKeyError`, which I return as 409.

3. **Ties.** `$rank` gives both employees the same rank, and I keep every row with `rank <= limit`, so both are returned (ordered by `emp_code`) even if that is more than `limit` rows. Cutting one would be arbitrary.

4. **Headcount.** The pipeline starts from `employees` with `joined_on` on or before month end. Log totals are merged in per `emp_code`, so someone with no logs still counts, with zeros.

5. **100x.** Keep a daily roll-up collection updated on every write, so analytics read summaries instead of scanning a month of raw logs.
