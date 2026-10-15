"""End-to-end self test. It WIPES the database it is pointed at, so it refuses to run
unless MONGO_DB ends with "_selftest".

    set MONGO_DB=attendance_selftest          (PowerShell: $env:MONGO_DB = "attendance_selftest")
    uvicorn app.main:app --port 8011          (same MONGO_DB)
    python tests/selftest.py http://127.0.0.1:8011

It seeds ~100k awkward records straight into MongoDB (ties, weekend records, employees with no
logs, mid-month joiners, overnight shifts, legacy documents, orphan logs), then compares every
analytics endpoint with a plain-Python reference and exercises the rules, errors and races.
"""
import calendar
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
DB_NAME = os.getenv("MONGO_DB", "")
assert DB_NAME.endswith("_selftest"), "set MONGO_DB to a name ending in _selftest"
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[DB_NAME]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
failures = []
checks = 0


def check(name, ok, detail=""):
    global checks
    checks += 1
    if not ok:
        failures.append(name)
        print(f"FAIL {name} {detail}")


def call(method, path, body=None, **params):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")


def ist_ms(day, hh, mm, ss=0, ms=0):
    y, m, d = map(int, day.split("-"))
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=IST).timestamp()) * 1000 + ms


def hup(value, places):
    return float(Decimal(value).quantize(Decimal(places), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Seed
# --------------------------------------------------------------------------- #
def seed():
    random.seed(7)
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    depts = ["Engineering", "Sales", "Support", "Finance", "Ops", "HR"]
    employees = []
    for i in range(1, 801):
        if i % 40 == 0:
            joined = f"2026-0{random.choice([5, 6, 7])}-{random.randint(2, 27):02d}"  # mid-period joiner
        elif i % 97 == 0:
            joined = "2026-11-03"  # has not joined yet
        else:
            joined = "2025-06-02"
        overnight = i % 25 == 0
        employees.append({
            "emp_code": f"EMP{i:04d}", "name": f"Person {i}", "email": f"p{i}@example.com",
            "department": depts[i % len(depts)],
            "shift_start": "22:00" if overnight else "09:30", "shift_end": "06:00" if overnight else "18:30",
            "joined_on": joined, "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })
    employees.append({  # a department whose only member joins later: omitted from the July summary
        "emp_code": "EMP9001", "name": "Late Starter", "email": "ls@example.com", "department": "Legal",
        "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-20",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    })
    db.employees.insert_many([dict(e) for e in employees])

    logs = []
    day, end = date(2026, 3, 1), date(2026, 8, 31)
    while day <= end:
        ds = day.isoformat()
        for e in employees:
            n = int(e["emp_code"][3:])
            if n % 50 == 7 or n > 9000 or ds < e["joined_on"]:
                continue  # some employees never have logs
            if day.weekday() >= 5 and random.random() > 0.04:
                continue  # a few weekend records
            if random.random() < 0.06:
                continue  # a weekday with no record at all
            r = random.random()
            status = "PRESENT" if r < 0.70 else "WFH" if r < 0.80 else "ON_DUTY" if r < 0.86 else "LEAVE" if r < 0.93 else "ABSENT"
            doc = {"emp_code": e["emp_code"], "date": ds, "status": status}
            if status in PRESENCE:
                late = random.choice([0, 0, 0, 0, 15, 30, 45])  # few distinct values -> many ties
                start_h = 22 if e["shift_start"] == "22:00" else 9
                pin = datetime(day.year, day.month, day.day, start_h, 30, tzinfo=IST) + timedelta(minutes=late)
                doc.update(punch_in=pin, late_minutes=late)
                if random.random() < 0.95:
                    hours = Decimal(random.randint(200, 1100)) / 100
                    doc.update(punch_out=pin + timedelta(seconds=int(hours * 3600)), work_hours=float(hours),
                               overtime_minutes=random.choice([0, 0, 30, 45]), half_day=hours < Decimal("4.5"))
                else:
                    doc.update(punch_out=None, work_hours=None, overtime_minutes=0, half_day=False)
            else:
                doc.update(punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
            if random.random() < 0.05:  # legacy shape: no half_day, no history
                doc.pop("half_day")
            else:
                doc["history"] = []
            logs.append(doc)
        if day.day == 10:  # logs of an employee that does not exist
            logs.append({"emp_code": "EMP7777", "date": ds, "status": "PRESENT",
                         "punch_in": datetime(day.year, day.month, day.day, 11, tzinfo=IST), "punch_out": None,
                         "work_hours": None, "late_minutes": 90, "overtime_minutes": 0, "half_day": False, "history": []})
        day += timedelta(days=1)
    db.attendance_logs.insert_many([dict(x) for x in logs])
    print(f"seeded {len(employees)} employees, {len(logs)} logs")
    return employees, logs


# --------------------------------------------------------------------------- #
# Plain-Python reference implementations
# --------------------------------------------------------------------------- #
def weekday(ds):
    return date.fromisoformat(ds).weekday() < 5


def day_value(log, weekdays_only):
    if log["status"] not in PRESENCE or (weekdays_only and not weekday(log["date"])):
        return Decimal(0)
    return Decimal("0.5") if log.get("half_day") else Decimal(1)


def month_range(month):
    y, m = map(int, month.split("-"))
    return f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}"


def ref_monthly(emp, logs, month):
    first, last = month_range(month)
    mine = [x for x in logs if x["emp_code"] == emp["emp_code"] and first <= x["date"] <= last]
    start = max(first, emp["joined_on"])
    working = 0
    d = date.fromisoformat(start)
    while d.isoformat() <= last:
        working += d.weekday() < 5
        d += timedelta(days=1)
    present = sum(day_value(x, True) for x in mine)
    return {
        "emp_code": emp["emp_code"], "month": month, "working_days": working, "present_days": float(present),
        "leave_days": sum(x["status"] == "LEAVE" for x in mine),
        "late_count": sum(x.get("late_minutes", 0) > 0 for x in mine),
        "total_late_minutes": sum(x.get("late_minutes", 0) for x in mine),
        "total_overtime_minutes": sum(x.get("overtime_minutes", 0) for x in mine),
        "attendance_pct": hup(present * 100 / working, "0.01") if working else None,
    }


def ref_summary(employees, logs, month, department=None):
    first, last = month_range(month)
    by_code = {e["emp_code"]: e for e in employees if e["joined_on"] <= last}
    out = {}
    for e in by_code.values():
        if department is None or e["department"] == department:
            row = out.setdefault(e["department"], {"department": e["department"], "headcount": 0, "present": Decimal(0),
                                                   "cents": 0, "n": 0, "late_count": 0, "total_late_minutes": 0,
                                                   "leave_count": 0, "on_duty_count": 0})
            row["headcount"] += 1
    for x in logs:
        e = by_code.get(x["emp_code"])
        if e is None or not (first <= x["date"] <= last) or e["department"] not in out:
            continue
        row = out[e["department"]]
        row["present"] += day_value(x, True)
        if x["status"] in PRESENCE and x.get("work_hours") is not None:
            row["cents"] += int(round(x["work_hours"] * 100))
            row["n"] += 1
        if x.get("late_minutes", 0) > 0:
            row["late_count"] += 1
            row["total_late_minutes"] += x["late_minutes"]
        row["leave_count"] += x["status"] == "LEAVE"
        row["on_duty_count"] += x["status"] == "ON_DUTY"
    items = []
    for name in sorted(out):
        r = out[name]
        items.append({"department": name, "headcount": r["headcount"], "present_days": float(r["present"]),
                      "avg_work_hours": hup(Decimal(r["cents"]) / 100 / r["n"], "0.01") if r["n"] else None,
                      "late_count": r["late_count"], "total_late_minutes": r["total_late_minutes"],
                      "leave_count": r["leave_count"], "on_duty_count": r["on_duty_count"]})
    return items


def ref_leaderboard(employees, logs, month, limit, department=None):
    first, last = month_range(month)
    by_code = {e["emp_code"]: e for e in employees}
    totals, counts = defaultdict(int), defaultdict(int)
    for x in logs:
        if first <= x["date"] <= last and x.get("late_minutes", 0) > 0 and x["emp_code"] in by_code:
            if department is None or by_code[x["emp_code"]]["department"] == department:
                totals[x["emp_code"]] += x["late_minutes"]
                counts[x["emp_code"]] += 1
    ordered = sorted(totals, key=lambda c: (-totals[c], c))
    items = []
    for code in ordered:
        rank = 1 + sum(totals[o] > totals[code] for o in ordered)
        if rank <= limit:
            e = by_code[code]
            items.append({"rank": rank, "emp_code": code, "name": e["name"], "department": e["department"],
                          "total_late_minutes": totals[code], "late_count": counts[code]})
    return items


def ref_trend(employees, logs, department, d_from, d_to):
    members = [e for e in employees if e["department"] == department]
    codes = {e["emp_code"] for e in members}
    per_day = defaultdict(list)
    for x in logs:
        if x["emp_code"] in codes and d_from <= x["date"] <= d_to:
            per_day[x["date"]].append(x)
    rows, d = [], date.fromisoformat(d_from)
    while d.isoformat() <= d_to:
        ds = d.isoformat()
        head = sum(e["joined_on"] <= ds for e in members)
        present = sum(day_value(x, False) for x in per_day[ds])
        working = d.weekday() < 5
        rate = hup(present / head, "0.0001") if working and head else None
        rows.append({"date": ds, "is_working_day": working, "headcount": head, "present_count": float(present),
                     "late_count": sum(x.get("late_minutes", 0) > 0 for x in per_day[ds]), "attendance_rate": rate})
        d += timedelta(days=1)
    for i, row in enumerate(rows):
        window = [r["attendance_rate"] for r in rows[max(0, i - 6): i + 1] if r["attendance_rate"] is not None]
        row["moving_avg_7d"] = hup(sum(Decimal(str(v)) for v in window) / len(window), "0.0001") if window else None
    return rows


def plan_text(body):
    return json.dumps(body["explain"])


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_analytics(employees, logs):
    by_code = {e["emp_code"]: e for e in employees}
    for code in ["EMP0001", "EMP0007", "EMP0025", "EMP0040", "EMP0080", "EMP0097", "EMP0120", "EMP9001"]:
        for month in ["2026-05", "2026-06", "2026-07", "2026-02"]:
            st, got = call("GET", f"/analytics/employees/{code}/monthly", month=month)
            want = ref_monthly(by_code[code], logs, month)
            check(f"monthly {code} {month}", st == 200 and got == want, f"\n got {got}\nwant {want}")
    check("monthly unknown employee 404", call("GET", "/analytics/employees/EMP5555/monthly", month="2026-07")[0] == 404)
    check("monthly bad month 422", call("GET", "/analytics/employees/EMP0001/monthly", month="2026-13")[0] == 422)
    check("monthly missing month 422", call("GET", "/analytics/employees/EMP0001/monthly")[0] == 422)

    for month in ["2026-05", "2026-06", "2026-07", "2026-08", "2025-01"]:
        for dept in [None, "Sales", "Legal", "Nope"]:
            params = {"month": month, **({"department": dept} if dept else {})}
            st, got = call("GET", "/analytics/departments/summary", **params)
            want = ref_summary(employees, logs, month, dept)
            check(f"summary {month} {dept}", st == 200 and got == {"month": month, "items": want},
                  f"\n got {got}\nwant {want}")

    for month in ["2026-05", "2026-07"]:
        for limit in [1, 3, 10, 50]:
            for dept in [None, "Support"]:
                params = {"month": month, "limit": limit, **({"department": dept} if dept else {})}
                st, got = call("GET", "/analytics/leaderboard/late", **params)
                want = ref_leaderboard(employees, logs, month, limit, dept)
                check(f"leaderboard {month} {limit} {dept}", st == 200 and got["items"] == want,
                      f"\n got {got['items'][:6]}\nwant {want[:6]}")
    st, got = call("GET", "/analytics/leaderboard/late", month="2026-07", limit=10)
    ranks = [r["rank"] for r in got["items"]]
    print(f"   leaderboard limit=10 returned {len(ranks)} rows, ranks {ranks[:14]}")
    check("leaderboard limit 0 -> 422", call("GET", "/analytics/leaderboard/late", month="2026-07", limit=0)[0] == 422)
    check("leaderboard limit 51 -> 422", call("GET", "/analytics/leaderboard/late", month="2026-07", limit=51)[0] == 422)

    for dept, d_from, d_to in [("Sales", "2026-06-01", "2026-08-31"), ("Engineering", "2026-07-04", "2026-07-04"),
                               ("Legal", "2026-07-01", "2026-07-31"), ("Ops", "2026-02-20", "2026-03-10"),
                               ("HR", "2027-01-01", "2027-01-10"), ("Support", "2026-04-27", "2026-05-12")]:
        st, got = call("GET", f"/analytics/departments/{dept}/trend", **{"from": d_from, "to": d_to})
        want = ref_trend(employees, logs, dept, d_from, d_to)
        ok = st == 200 and got["items"] == want
        detail = ""
        if st == 200 and not ok:
            diff = [(g, w) for g, w in zip(got["items"], want) if g != w][:2]
            detail = f"rows {len(got['items'])}/{len(want)} first diffs {diff}"
        check(f"trend {dept} {d_from}..{d_to}", ok, detail)
    check("trend unknown dept 404", call("GET", "/analytics/departments/Nope/trend", **{"from": "2026-07-01", "to": "2026-07-02"})[0] == 404)
    check("trend to<from 422", call("GET", "/analytics/departments/Sales/trend", **{"from": "2026-07-02", "to": "2026-07-01"})[0] == 422)
    check("trend 93 days 422", call("GET", "/analytics/departments/Sales/trend", **{"from": "2026-06-01", "to": "2026-09-01"})[0] == 422)
    check("trend 92 days 200", call("GET", "/analytics/departments/Sales/trend", **{"from": "2026-06-01", "to": "2026-08-31"})[0] == 200)
    check("trend bad date 422", call("GET", "/analytics/departments/Sales/trend", **{"from": "2026-02-30", "to": "2026-03-01"})[0] == 422)


def test_list_and_explain(logs):
    st, got = call("GET", "/attendance", page_size=100)
    want = sorted(logs, key=lambda x: (-int(x["date"].replace("-", "")), x["emp_code"]))
    check("list total", st == 200 and got["total"] == len(logs))
    check("list order", [(i["date"], i["emp_code"]) for i in got["items"]] == [(w["date"], w["emp_code"]) for w in want[:100]])
    check("list fields", set(got["items"][0]) == {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours",
                                                  "late_minutes", "overtime_minutes", "half_day", "history"})
    sel = [x for x in want if x["emp_code"] == "EMP0003" and x["status"] == "LEAVE" and "2026-05-01" <= x["date"] <= "2026-07-31"]
    st, got = call("GET", "/attendance", emp_code="EMP0003", status="LEAVE", date_from="2026-05-01", date_to="2026-07-31", page=1, page_size=5)
    check("list filtered", got["total"] == len(sel) and [i["date"] for i in got["items"]] == [x["date"] for x in sel[:5]])
    st, got = call("GET", "/attendance", status="ABSENT", page=3, page_size=7)
    sel = [x for x in want if x["status"] == "ABSENT"]
    check("list status page 3", got["total"] == len(sel) and [(i["date"], i["emp_code"]) for i in got["items"]] == [(x["date"], x["emp_code"]) for x in sel[14:21]])
    legacy = next(x for x in want if "half_day" not in x)
    st, got = call("GET", "/attendance", emp_code=legacy["emp_code"], date_from=legacy["date"], date_to=legacy["date"])
    check("legacy record defaults", got["items"][0]["half_day"] is False and got["items"][0]["history"] == [])
    for bad in [{"date_from": "2026-07-10", "date_to": "2026-07-01"}, {"date_from": "07-10-2026"}, {"status": "SICK"},
                {"page": 0}, {"page_size": 101}, {"page_size": 0}, {"page": "x"}]:
        check(f"list 422 {bad}", call("GET", "/attendance", **bad)[0] == 422)
    check("list huge page", call("GET", "/attendance", page=10**15)[1]["items"] == [])

    cases = [
        ("attendance_list", {}), ("attendance_list", {"status": "LEAVE", "page": 5}),
        ("attendance_list", {"emp_code": "EMP0003"}), ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31"}),
        ("attendance_list", {"emp_code": "EMP0003", "status": "PRESENT", "date_from": "2026-07-01"}),
        ("employee_monthly", {"emp_code": "EMP0003", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}), ("department_summary", {"month": "2026-07", "department": "Sales"}),
        ("late_leaderboard", {"month": "2026-07"}), ("late_leaderboard", {"month": "2026-07", "department": "Sales", "limit": 5}),
        ("department_trend", {"department": "Sales", "from": "2026-06-01", "to": "2026-08-31"}),
    ]
    for name, params in cases:
        t = time.perf_counter()
        st, got = call("GET", f"/admin/explain/{name}", **params)
        ms = (time.perf_counter() - t) * 1000
        text = plan_text(got) if st == 200 else ""
        check(f"explain {name} {params}", st == 200 and "IXSCAN" in text and "COLLSCAN" not in text and got["endpoint"] == name,
              f"status {st} ixscan={'IXSCAN' in text} collscan={'COLLSCAN' in text}")
        print(f"   explain {name:19s} {got.get('collection', ''):16s} {ms:6.0f} ms  {params}")
    check("explain unknown 422", call("GET", "/admin/explain/nope")[0] == 422)
    check("explain missing month 422", call("GET", "/admin/explain/late_leaderboard")[0] == 422)
    check("explain missing emp_code 422", call("GET", "/admin/explain/employee_monthly", month="2026-07")[0] == 422)
    check("explain trend missing to 422", call("GET", "/admin/explain/department_trend", department="Sales", **{"from": "2026-07-01"})[0] == 422)


def test_timing():
    for path, params in [("/attendance", {"page": 300, "page_size": 100}), ("/attendance", {"status": "PRESENT", "page": 200}),
                         ("/analytics/departments/summary", {"month": "2026-07"}),
                         ("/analytics/leaderboard/late", {"month": "2026-07", "limit": 50}),
                         ("/analytics/departments/Sales/trend", {"from": "2026-06-01", "to": "2026-08-31"}),
                         ("/analytics/employees/EMP0003/monthly", {"month": "2026-07"})]:
        t = time.perf_counter()
        st, _ = call("GET", path, **params)
        ms = (time.perf_counter() - t) * 1000
        print(f"   {ms:6.0f} ms  {path} {params}")
        check(f"fast {path}", st == 200 and ms < 2000, f"{ms:.0f} ms")


def new_emp(code, **over):
    body = {"emp_code": code, "name": "Test", "email": "t@example.com", "department": "QA", "joined_on": "2026-01-01", **over}
    return call("POST", "/employees", body)


def test_employees():
    st, got = new_emp("EMP800001")
    check("create employee", st == 201 and got["shift_start"] == "09:30" and isinstance(got["created_at"], int)
          and "_id" not in got and "id" not in got, str(got))
    check("duplicate employee 409", new_emp("EMP800001")[0] == 409)
    for label, over in [("bad code", {"emp_code": "E1"}), ("code too long", {"emp_code": "EMP1234567"}), ("empty name", {"name": ""}),
                        ("bad email", {"email": "nope"}), ("bad shift", {"shift_start": "24:00"}), ("shift 9:30", {"shift_start": "9:30"}),
                        ("same shift", {"shift_start": "10:00", "shift_end": "10:00"}), ("bad date", {"joined_on": "2026-02-30"}),
                        ("date format", {"joined_on": "01-02-2026"}), ("long dept", {"department": "x" * 51})]:
        check(f"employee 422 {label}", new_emp(over.get("emp_code", "EMP800002"), **over)[0] == 422)
    check("employee missing field 422", call("POST", "/employees", {"emp_code": "EMP800003"})[0] == 422)
    with ThreadPoolExecutor(12) as pool:
        codes = sorted(st for st, _ in pool.map(lambda _: new_emp("EMP800009"), range(12)))
    check("employee create race", codes == [201] + [409] * 11, str(codes))

    st, got = call("GET", "/employees", department="Sales", page=2, page_size=10)
    want = sorted(e["emp_code"] for e in db.employees.find({"department": "Sales"}))
    check("employees page 2", got["total"] == len(want) and [i["emp_code"] for i in got["items"]] == want[10:20])
    check("employees page 0 -> 422", call("GET", "/employees", page=0)[0] == 422)
    check("employees size 101 -> 422", call("GET", "/employees", page_size=101)[0] == 422)
    check("employees unknown dept", call("GET", "/employees", department="Nope")[1]["total"] == 0)


def punch(kind, code, at=None, **extra):
    body = {"emp_code": code, **extra}
    if at is not None:
        body["punched_at"] = at
    return call("POST", f"/attendance/punch-{kind}", body)


def test_punching():
    new_emp("EMP810001")
    new_emp("EMP810002", shift_start="22:00", shift_end="06:00")
    # R2 examples for a 09:30 shift
    for day, (h, m, s), want in [("2026-09-01", (9, 40, 0), 0), ("2026-09-02", (9, 40, 1), 10), ("2026-09-03", (10, 15, 59), 45),
                                 ("2026-09-04", (9, 0, 0), 0), ("2026-09-07", (9, 41, 0), 11)]:
        st, got = punch("in", "EMP810001", ist_ms(day, h, m, s))
        check(f"R2 {h}:{m}:{s}", st == 201 and got["late_minutes"] == want and got["date"] == day, str(got))
    st, got = punch("in", "EMP810001", ist_ms("2026-09-08", 9, 40, 0, 900))  # truncated to 09:40:00
    check("R1 truncation", got["late_minutes"] == 0 and got["punch_in"] == ist_ms("2026-09-08", 9, 40, 0)
          and got["punch_out"] is None and got["work_hours"] is None and got["history"] == [] and "id" not in got, str(got))
    st, got = punch("in", "EMP810001", ist_ms("2026-09-09", 0, 10))  # 00:10 IST is the previous day in UTC
    check("R1 IST date", got["date"] == "2026-09-09", str(got))
    check("second punch-in 409", punch("in", "EMP810001", ist_ms("2026-09-09", 12, 0))[0] == 409)
    check("unknown employee 404", punch("in", "EMP000000", ist_ms("2026-09-09", 12, 0))[0] == 404)
    for label, at in [("seconds", 1783312500), ("float", 1783312500000.5), ("string", "1783312500000"), ("too big", 4102444800001), ("bool", True)]:
        check(f"punched_at {label} 422", punch("in", "EMP810001", at)[0] == 422)
    check("status LEAVE 422", punch("in", "EMP810001", ist_ms("2026-09-10", 9, 0), status="LEAVE")[0] == 422)
    st, got = punch("in", "EMP810001", ist_ms("2026-09-10", 9, 0), status="WFH", late_minutes=99)
    check("status WFH, derived field ignored", st == 201 and got["status"] == "WFH" and got["late_minutes"] == 0)
    st, got = punch("in", "EMP810001")
    check("punch-in now", st == 201 and abs(got["punch_in"] - time.time() * 1000) < 5000, str(got))

    # overnight shift 22:00 - 06:00
    st, got = punch("in", "EMP810002", ist_ms("2026-09-01", 21, 55))
    check("overnight early", got["date"] == "2026-09-01" and got["late_minutes"] == 0, str(got))
    st, got = punch("out", "EMP810002", ist_ms("2026-09-02", 6, 40))
    check("overnight punch-out", st == 200 and got["date"] == "2026-09-01" and got["overtime_minutes"] == 40 and got["work_hours"] == 8.75, str(got))
    st, got = punch("in", "EMP810002", ist_ms("2026-09-03", 1, 0))  # belongs to the shift of the 2nd
    check("overnight after midnight", got["date"] == "2026-09-02" and got["late_minutes"] == 180, str(got))
    st, got = punch("in", "EMP810002", ist_ms("2026-09-04", 5, 59, 59))
    check("overnight 05:59:59", got["date"] == "2026-09-03" and got["late_minutes"] == 479, str(got))
    st, got = punch("in", "EMP810002", ist_ms("2026-09-05", 6, 0))
    check("overnight 06:00 is its own day", got["date"] == "2026-09-05" and got["late_minutes"] == 0, str(got))

    # R3-R5 on day shift records opened above
    def out_for(day, h, m, s=0):
        return punch("out", "EMP810001", ist_ms(day, h, m, s))
    st, got = out_for("2026-09-01", 18, 59, 59)   # 9:40 -> 18:59:59, overtime 29 -> 0
    check("R3 29 min", st == 200 and got["overtime_minutes"] == 0 and got["work_hours"] == 9.33 and got["half_day"] is False, str(got))
    st, got = out_for("2026-09-02", 19, 0, 0)     # overtime 30
    check("R3 30 min", got["overtime_minutes"] == 30, str(got))
    st, got = out_for("2026-09-03", 14, 45, 41)   # 10:15:59 -> 14:45:41 = 16182 s = 4.495 h -> 4.50 (half-up)
    check("R4 half-up, R5 4.50 is not half", got["work_hours"] == 4.5 and got["half_day"] is False, str(got))
    st, got = out_for("2026-09-04", 13, 29, 23)   # 9:00 -> 13:29:23 = 16163 s = 4.4897 -> 4.49
    check("R5 4.49 is half", got["work_hours"] == 4.49 and got["half_day"] is True and got["late_minutes"] == 0, str(got))
    check("punch-out twice 409", punch("out", "EMP810001", ist_ms("2026-09-04", 14, 0))[0] == 409)
    check("punch-out equal to punch-in 422", out_for("2026-09-07", 9, 41)[0] == 422)
    st, got = out_for("2026-09-07", 13, 41, 18)   # 14418 s = 4.005 h; float round() gives 4.0
    check("R4 half-up 4.005 -> 4.01", st == 200 and got["work_hours"] == 4.01 and got["half_day"] is True, str(got))
    check("punch-out > 24h 422", punch("out", "EMP810001", ist_ms("2026-09-11", 9, 0, 1))[0] == 422)
    check("punch-out exactly 24h 200", punch("out", "EMP810001", ist_ms("2026-09-11", 9, 0, 0))[0] == 200)
    check("punch-out no punch-in 404", punch("out", "EMP810001", ist_ms("2026-08-01", 9, 0))[0] == 404)
    check("punch-out unknown employee 404", punch("out", "EMP000000", ist_ms("2026-09-08", 9, 0))[0] == 404)
    check("punch-out bad ts 422", punch("out", "EMP810001", "x")[0] == 422)

    # races
    new_emp("EMP810003")
    at = ist_ms("2026-09-15", 9, 30)
    with ThreadPoolExecutor(16) as pool:
        codes = sorted(st for st, _ in pool.map(lambda _: punch("in", "EMP810003", at), range(16)))
    check("punch-in race", codes == [201] + [409] * 15, str(codes))
    with ThreadPoolExecutor(16) as pool:
        codes = sorted(st for st, _ in pool.map(lambda _: punch("out", "EMP810003", ist_ms("2026-09-15", 18, 30)), range(16)))
    check("punch-out race", codes == [200] + [409] * 15, str(codes))


def patch(code, day, **body):
    body = {"reason": "selftest fix", "regularized_by": "qa", **body}
    return call("PATCH", f"/attendance/{code}/{day}", body)


def test_regularize():
    new_emp("EMP820001")
    day = "2026-09-21"
    punch("in", "EMP820001", ist_ms(day, 10, 5))
    punch("out", "EMP820001", ist_ms(day, 18, 30))
    st, got = patch("EMP820001", day, punch_in=ist_ms(day, 9, 28))
    h = got["history"][0] if st == 200 else {}
    check("patch punch_in", st == 200 and got["late_minutes"] == 0 and got["work_hours"] == 9.03 and len(got["history"]) == 1
          and h["changes"] == {"punch_in": {"from": ist_ms(day, 10, 5), "to": ist_ms(day, 9, 28)},
                               "late_minutes": {"from": 35, "to": 0}, "work_hours": {"from": 8.42, "to": 9.03}}
          and h["by"] == "qa" and h["reason"] == "selftest fix" and isinstance(h["at"], int), str(got))
    stored = db.attendance_logs.find_one({"emp_code": "EMP820001", "date": day})
    check("history stored as dates", isinstance(stored["history"][0]["at"], datetime)
          and isinstance(stored["history"][0]["changes"]["punch_in"]["to"], datetime))
    check("patch nothing changed 422", patch("EMP820001", day, punch_in=ist_ms(day, 9, 28))[0] == 422)
    check("patch only reason 422", patch("EMP820001", day)[0] == 422)
    check("patch same status 422", patch("EMP820001", day, status="PRESENT")[0] == 422)
    check("patch punch_in other date 422", patch("EMP820001", day, punch_in=ist_ms("2026-09-22", 9, 0))[0] == 422)
    check("patch punch_out before in 422", patch("EMP820001", day, punch_out=ist_ms(day, 9, 0))[0] == 422)
    check("patch punch_in after stored out 422", patch("EMP820001", day, punch_in=ist_ms(day, 19, 0))[0] == 422)
    check("patch LEAVE with times 422", patch("EMP820001", day, status="LEAVE", punch_out=ist_ms(day, 19, 0))[0] == 422)
    check("patch short reason 422", patch("EMP820001", day, status="WFH", reason="hm")[0] == 422)
    check("patch missing by 422", call("PATCH", f"/attendance/EMP820001/{day}", {"reason": "valid reason", "status": "WFH"})[0] == 422)
    check("patch bad date 422", patch("EMP820001", "2026-9-21", status="WFH")[0] == 422)
    check("patch unknown employee 404", patch("EMP000000", day, status="WFH")[0] == 404)
    check("patch no record 404", patch("EMP820001", "2026-09-22", status="WFH")[0] == 404)
    st, got = patch("EMP820001", day, status="WFH", punch_out=ist_ms(day, 19, 10), work_hours=1)
    check("patch status + out", st == 200 and got["status"] == "WFH" and got["overtime_minutes"] == 40 and len(got["history"]) == 2
          and set(got["history"][1]["changes"]) == {"status", "punch_out", "work_hours", "overtime_minutes"}, str(got))
    st, got = patch("EMP820001", day, status="LEAVE")
    check("patch to LEAVE clears", st == 200 and got["punch_in"] is None and got["punch_out"] is None and got["work_hours"] is None
          and got["overtime_minutes"] == 0 and len(got["history"]) == 3
          and got["history"][2]["changes"]["punch_out"] == {"from": ist_ms(day, 19, 10), "to": None}, str(got))
    check("patch presence without punch_in 422", patch("EMP820001", day, status="PRESENT")[0] == 422)
    check("patch punch_in on LEAVE 422", patch("EMP820001", day, punch_in=ist_ms(day, 9, 0))[0] == 422)
    st, got = patch("EMP820001", day, status="ON_DUTY", punch_in=ist_ms(day, 9, 45), punch_out=ist_ms(day, 13, 0))
    check("patch back to presence", st == 200 and got["late_minutes"] == 15 and got["work_hours"] == 3.25 and got["half_day"] is True, str(got))

    # legacy record (no history field) and concurrent edits
    db.attendance_logs.insert_one({"emp_code": "EMP820001", "date": "2026-09-23", "status": "PRESENT",
                                   "punch_in": datetime.fromtimestamp(ist_ms("2026-09-23", 9, 30) / 1000, timezone.utc),
                                   "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0})
    with ThreadPoolExecutor(12) as pool:
        results = list(pool.map(lambda i: patch("EMP820001", "2026-09-23", punch_in=ist_ms("2026-09-23", 9, 31 + i)), range(12)))
    codes = sorted(st for st, _ in results)
    stored = db.attendance_logs.find_one({"emp_code": "EMP820001", "date": "2026-09-23"})
    check("patch race keeps every history entry", set(codes) <= {200, 409} and codes.count(200) >= 1
          and len(stored["history"]) == codes.count(200), f"{codes} history={len(stored['history'])}")
    print(f"   patch race: {codes.count(200)} x 200, {codes.count(409)} x 409, history entries {len(stored['history'])}")


if __name__ == "__main__":
    check("health", call("GET", "/health") == (200, {"status": "ok"}))
    employees, logs = seed()
    test_analytics(employees, logs)
    test_list_and_explain(logs)
    test_timing()
    test_employees()
    test_punching()
    test_regularize()
    print(f"\n{checks - len(failures)}/{checks} checks passed")
    sys.exit(1 if failures else 0)
