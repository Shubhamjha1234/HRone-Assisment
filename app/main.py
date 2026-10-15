"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Layout of this file: config, time and rule helpers (R1-R5), request models, serializers,
query/pipeline builders (shared by the endpoints and /admin/explain), then the endpoints.
"""
import calendar
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

load_dotenv()  # override=False, so variables already set in the environment are kept

log = logging.getLogger("attendance")

# tz_aware=True: BSON dates come back as aware UTC datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_BELOW_HOURS = Decimal("4.50")
MAX_RECORD_SECONDS = 24 * 3600
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
MAX_TREND_DAYS = 92
MAX_SKIP = 2**31 - 1


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when the same index already exists."""
    # employees
    db.employees.create_index("emp_code", unique=True)  # identity + duplicate-create race
    db.employees.create_index([("department", 1), ("emp_code", 1)])  # list by department, trend
    db.employees.create_index([("joined_on", 1), ("department", 1)])  # headcount (R9)
    # attendance_logs
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)  # one record per day
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])  # list sort, month ranges
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])  # list by status


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        # Stay up so /health can report 503 instead of the process dying.
        log.exception("could not create indexes at startup")
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(ConnectionFailure)
def database_unavailable(_, __):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def unprocessable(loc: list, msg: str) -> HTTPException:
    """A 422 for rules pydantic cannot express, in the same shape FastAPI uses."""
    return HTTPException(422, detail=[{"type": "value_error", "loc": loc, "msg": msg}])


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return calendar.timegm(dt.utctimetuple()) * 1000 + dt.microsecond // 1000


def now_utc() -> datetime:
    return from_ms(int(time.time() * 1000))


def whole_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def half_up(value: Decimal, places: str) -> float:
    """Round half-up (R4, R8). Python's round() is half-even and works on binary floats."""
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def month_bounds(month: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31')."""
    year, mon = int(month[:4]), int(month[5:])
    if year < 1:
        raise unprocessable(["query", "month"], "year must be 0001 or later")
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def count_weekdays(first: str, last: str) -> int:
    """Mon-Fri days from first to last inclusive (R7). A calendar count, not a data loop."""
    day, end, total = date.fromisoformat(first), date.fromisoformat(last), 0
    while day <= end:
        total += day.weekday() < 5
        day += timedelta(days=1)
    return total


# --------------------------------------------------------------------------- #
# Business rules R1-R5
# --------------------------------------------------------------------------- #
def shift_minutes(emp: dict) -> tuple[int, int]:
    """Shift start and end as minutes since midnight IST."""
    sh, sm = map(int, emp.get("shift_start", "09:30").split(":"))
    eh, em = map(int, emp.get("shift_end", "18:30").split(":"))
    return sh * 60 + sm, eh * 60 + em


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: the IST calendar date of the punch-in; an overnight shift's early-morning
    punch-in (before shift_end) belongs to the shift that started the previous day."""
    start, end = shift_minutes(emp)
    local = punch_in.astimezone(IST)
    day = local.date()
    if end <= start and local.hour * 60 + local.minute < end:
        day -= timedelta(days=1)
    return day.isoformat()


def shift_window(emp: dict, day: str) -> tuple[datetime, datetime]:
    """The shift's start and end instants for an attendance date."""
    start, end = shift_minutes(emp)
    midnight = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=IST)
    if end <= start:  # overnight: the shift ends on the next calendar day
        end += 24 * 60
    return midnight + timedelta(minutes=start), midnight + timedelta(minutes=end)


def compute_late_minutes(emp: dict, day: str, punch_in: datetime) -> int:
    """R2: late only when strictly more than the grace period after shift_start;
    then whole minutes since shift_start (not since the end of the grace period)."""
    shift_start, _ = shift_window(emp, day)
    seconds = int((whole_seconds(punch_in) - shift_start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_closing_fields(emp: dict, day: str, punch_in: datetime, punch_out: Optional[datetime]) -> dict:
    """R3-R5: the fields that depend on punch_out."""
    if punch_out is None:
        return {"work_hours": None, "overtime_minutes": 0, "half_day": False}
    punch_in, punch_out = whole_seconds(punch_in), whole_seconds(punch_out)
    worked = int((punch_out - punch_in).total_seconds())
    hours = (Decimal(worked) / 3600).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    _, shift_end = shift_window(emp, day)
    overtime = int((punch_out - shift_end).total_seconds()) // 60
    return {
        "work_hours": float(hours),
        "overtime_minutes": overtime if overtime >= MIN_OVERTIME_MINUTES else 0,
        "half_day": hours < HALF_DAY_BELOW_HOURS,
    }


def valid_duration(punch_in: datetime, punch_out: datetime) -> bool:
    """punch_out must be after punch_in and at most 24 hours after it."""
    seconds = (whole_seconds(punch_out) - whole_seconds(punch_in)).total_seconds()
    return 0 < seconds <= MAX_RECORD_SECONDS


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
def check_date(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("must be a date in YYYY-MM-DD format")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError("is not a real calendar date")
    return value


DateStr = Annotated[str, AfterValidator(check_date)]
# strict: no floats, strings or booleans; the lower bound rejects values that look like seconds
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
ShiftTime = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class ExplainTarget(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serializers (MongoDB document -> API shape; _id never leaves this file)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def history_out(entry: dict) -> dict:
    # punch_in / punch_out inside changes are BSON dates in MongoDB, epoch ms in the API
    changes = {
        field: {k: to_ms(v) if isinstance(v, datetime) else v for k, v in change.items()}
        for field, change in (entry.get("changes") or {}).items()
    }
    return {"at": to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    # Seeded records may omit the derived fields and history; missing means 0 / false / [].
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day")),
        "history": [history_out(h) for h in doc.get("history") or []],
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


def page_skip(page: int, page_size: int) -> int:
    return min((page - 1) * page_size, MAX_SKIP)


# --------------------------------------------------------------------------- #
# Query and pipeline builders (used by the endpoints and by /admin/explain)
# --------------------------------------------------------------------------- #
ATTENDANCE_SORT = {"date": -1, "emp_code": 1}

# Expressions over one attendance_logs document. Analytics read the stored derived values.
IS_PRESENT = {"$in": ["$status", PRESENCE]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
DAY_VALUE = {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}  # R5: a half day is 0.5
PRESENT_ANY_DAY = {"$cond": [IS_PRESENT, DAY_VALUE, 0]}
PRESENT_WEEKDAY = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, DAY_VALUE, 0]}  # R7
IS_LATE = {"$gt": ["$late_minutes", 0]}
LATE_ONE = {"$cond": [IS_LATE, 1, 0]}
LATE_MINUTES = {"$cond": [IS_LATE, "$late_minutes", 0]}
HAS_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def count_if(condition: dict) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def round_half_up(numerator, denominator) -> dict:
    """numerator / denominator rounded half-up to a whole number, inside MongoDB.
    $round is half-even, so this is floor((2n + d) / 2d) on whole numbers, which is exact."""
    return {"$floor": {"$divide": [
        {"$add": [{"$multiply": [2, numerator]}, denominator]},
        {"$multiply": [2, denominator]},
    ]}}


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise unprocessable(["query", "date_from"], "date_from must not be after date_to")
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status.value
    return q


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": PRESENT_WEEKDAY},
            "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": {"$sum": LATE_ONE},
            "total_late_minutes": {"$sum": LATE_MINUTES},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    """Starts from employees so that people with no logs are still counted (R9). The month's
    logs are reduced to one totals document per employee in a single index range scan and
    merged in with $unionWith; a $lookup per employee gave the same result but was ~10x slower."""
    first, last = month_bounds(month)
    headcount: dict = {"joined_on": {"$lte": last}}
    if department is not None:
        headcount["department"] = department
    totals = ["present_days", "hours_x100", "hours_records", "late_count", "total_late_minutes",
              "leave_count", "on_duty_count"]
    month_totals = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": "$emp_code",
            "present_days": {"$sum": PRESENT_WEEKDAY},
            # work_hours has 2 decimals, so summing whole hundredths keeps the average exact
            "hours_x100": {"$sum": {"$cond": [HAS_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
            "hours_records": count_if(HAS_HOURS),
            "late_count": {"$sum": LATE_ONE},
            "total_late_minutes": {"$sum": LATE_MINUTES},
            "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
            "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
        }},
        {"$set": {"emp_code": "$_id"}},
    ]
    return [
        {"$match": headcount},
        {"$project": {"_id": 0, "emp_code": 1, "department": 1}},
        {"$unionWith": {"coll": "attendance_logs", "pipeline": month_totals}},
        # Per emp_code there is now an employee document, a totals document, or both.
        {"$group": {
            "_id": "$emp_code",
            "department": {"$max": "$department"},  # only the employee document has one
            **{f: {"$sum": f"${f}"} for f in totals},
        }},
        # Totals without an employee in the headcount (unknown, not joined yet, other department).
        {"$match": {"department": {"$ne": None}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, **{f: {"$sum": f"${f}"} for f in totals}}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            # an average over records, not an average of per-employee averages
            "avg_work_hours": {"$cond": [
                {"$gt": ["$hours_records", 0]},
                {"$divide": [round_half_up("$hours_x100", "$hours_records"), 100]},
                None,
            ]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # drops logs whose employee does not exist
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department
    pipeline += [
        # $rank is standard competition ranking: ties share a rank and the next one is skipped
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies to the rank, so ties at the cutoff all stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return pipeline


def check_trend_range(date_from: str, date_to: str) -> None:
    days = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1
    if days < 1:
        raise unprocessable(["query", "to"], "to must not be before from")
    if days > MAX_TREND_DAYS:
        raise unprocessable(["query", "to"], f"the range must not exceed {MAX_TREND_DAYS} days")


def department_trend_pipeline(department: str, date_from: str, date_to: str) -> list:
    """One row per calendar day. Every employee of the department contributes "events":
    one per log in the range, one on the day they start counting towards headcount, and
    an empty one on each end of the range so that $densify always spans from..to."""
    empty = {"present": 0, "late": 0, "joined": 0}
    last_7_rows = {"documents": [-6, 0]}
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
                {"$project": {
                    "_id": 0,
                    "day": "$date",
                    "present": PRESENT_ANY_DAY,
                    "late": LATE_ONE,
                    "joined": {"$literal": 0},
                }},
            ],
            "as": "logs",
        }},
        {"$project": {"events": {"$concatArrays": [
            "$logs",
            [{"day": date_from, **empty}, {"day": date_to, **empty}],
            {"$cond": [
                {"$lte": ["$joined_on", date_to]},
                [{"day": {"$max": ["$joined_on", date_from]}, "present": 0, "late": 0, "joined": 1}],
                [],
            ]},
        ]}}},
        {"$unwind": "$events"},
        {"$group": {
            "_id": "$events.day",
            "present": {"$sum": "$events.present"},
            "late": {"$sum": "$events.late"},
            "joined": {"$sum": "$events.joined"},
        }},
        {"$project": {
            "_id": 0,
            "day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
            "present": 1,
            "late": 1,
            "joined": 1,
        }},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": "full"}}},
        # headcount on a day = everyone who joined on or before it = running total of "joined"
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"headcount": {"$sum": "$joined", "window": {"documents": ["unbounded", "current"]}}},
        }},
        {"$set": {
            "present": {"$ifNull": ["$present", 0]},
            "late": {"$ifNull": ["$late", 0]},
            "working": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
        }},
        # the rate is kept as a whole number of ten-thousandths so the rounding stays exact
        {"$set": {"rate_x10000": {"$cond": [
            {"$and": ["$working", {"$gt": ["$headcount", 0]}]},
            round_half_up({"$multiply": ["$present", 10000]}, "$headcount"),
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {
                "window_sum": {"$sum": "$rate_x10000", "window": last_7_rows},  # $sum skips nulls
                "window_count": {"$sum": {"$cond": [{"$eq": ["$rate_x10000", None]}, 0, 1]}, "window": last_7_rows},
            },
        }},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "is_working_day": "$working",
            "headcount": 1,
            "present_count": "$present",
            "late_count": "$late",
            "attendance_rate": {"$divide": ["$rate_x10000", 10000]},  # null stays null
            "moving_avg_7d": {"$cond": [
                {"$gt": ["$window_count", 0]},
                {"$divide": [round_half_up("$window_sum", "$window_count"), 10000]},
                None,
            ]},
        }},
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# System and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(doc)  # the unique index decides the winner of a race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", 1).skip(page_skip(page, page_size)).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(emp, day, ts),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # No check-then-insert: the unique (emp_code, date) index lets exactly one insert win.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    # Most recent record that started at or before this instant. date order is punch_in order,
    # so the (emp_code, date) index serves this, and it works across midnight.
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("date", -1)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if not valid_duration(rec["punch_in"], ts):
        raise unprocessable(["body", "punched_at"], "must be after punch_in and within 24 hours of it")
    fields = compute_closing_fields(emp, rec["date"], rec["punch_in"], ts)
    # The filter repeats what was read, so of two simultaneous punch-outs only one matches.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_in": rec["punch_in"], "punch_out": None},
        {"$set": {"punch_out": ts, **fields}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort(ATTENDANCE_SORT).skip(page_skip(page, page_size)).limit(page_size)
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeIn):
    day = date  # the path parameter name is fixed by the contract
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if rec is None:
        raise HTTPException(404, "no attendance record for this date")
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day")),
    }
    status = body.status.value if body.status is not None else old["status"]
    times_sent = body.punch_in is not None or body.punch_out is not None

    if status in PRESENCE:
        pin = from_ms(body.punch_in) if body.punch_in is not None else old["punch_in"]
        pout = from_ms(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if pin is None:
            raise unprocessable(["body", "punch_in"], f"status {status} requires a punch_in")
        if body.punch_in is not None and attendance_date(pin, emp) != day:
            raise unprocessable(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if times_sent and pout is not None and not valid_duration(pin, pout):
            raise unprocessable(["body", "punch_out"], "punch_out must be after punch_in and within 24 hours of it")
        new = {
            "status": status,
            "punch_in": pin,
            "punch_out": pout,
            "late_minutes": compute_late_minutes(emp, day, pin),
            **compute_closing_fields(emp, day, pin, pout),
        }
    else:
        if times_sent:
            raise unprocessable(["body", "status"], f"status {status} cannot have punch times")
        new = {
            "status": status,
            "punch_in": None,
            "punch_out": None,
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }

    if all(old[k] == new[k] for k in ("status", "punch_in", "punch_out")):
        raise unprocessable(["body"], "the request changes nothing")

    entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {k: {"from": old[k], "to": new[k]} for k in old if old[k] != new[k]},
    }
    # Optimistic lock: every correction appends to history, so the update only applies if the
    # history is as long as when we read it (and no punch-out slipped in). The loser gets 409.
    history_len = len(rec.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "punch_out": rec.get("punch_out"),
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]},
        },
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "the record was changed by another request; retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = get_employee_or_404(emp_code)
    first, last = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    totals = rows[0] if rows else {}
    # R7: a mid-month joiner's working days are counted from joined_on
    working_days = count_weekdays(max(first, emp.get("joined_on") or first), last)
    present_days = totals.get("present_days", 0)
    pct = None
    if working_days:
        pct = half_up(Decimal(str(present_days)) * 100 / working_days, "0.01")
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": totals.get("leave_days", 0),
        "late_count": totals.get("late_count", 0),
        "total_late_minutes": int(totals.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(totals.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    items = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: Annotated[DateStr, Query(alias="from")],
    date_to: Annotated[DateStr, Query(alias="to")],
):
    check_trend_range(date_from, date_to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    items = list(db.employees.aggregate(department_trend_pipeline(department, date_from, date_to)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    trend_from: Optional[DateStr] = Query(None, alias="from"),
    trend_to: Optional[DateStr] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    def require(**params):
        for name, value in params.items():
            if value is None:
                raise unprocessable(["query", name], f"required for {endpoint.value}")

    # Each branch builds the command with the same builder the real endpoint uses.
    if endpoint is ExplainTarget.attendance_list:
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": ATTENDANCE_SORT,
            "skip": page_skip(page, page_size),
            "limit": page_size,
        }
    else:
        if endpoint is ExplainTarget.employee_monthly:
            require(emp_code=emp_code, month=month)
            collection, pipeline = "attendance_logs", employee_monthly_pipeline(emp_code, month)
        elif endpoint is ExplainTarget.department_summary:
            require(month=month)
            collection, pipeline = "employees", department_summary_pipeline(month, department)
        elif endpoint is ExplainTarget.late_leaderboard:
            require(month=month)
            collection, pipeline = "attendance_logs", late_leaderboard_pipeline(month, limit, department)
        else:
            require(department=department, **{"from": trend_from, "to": trend_to})
            check_trend_range(trend_from, trend_to)
            collection, pipeline = "employees", department_trend_pipeline(department, trend_from, trend_to)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    plan = db.command("explain", command, verbosity="executionStats")
    # The plan holds BSON-only types (timestamps, 64-bit ints); relaxed Extended JSON makes it plain JSON.
    plan = json.loads(json_util.dumps(plan, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint.value, "collection": collection, "explain": plan}
