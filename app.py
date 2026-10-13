from datetime import date, datetime, timedelta
from functools import wraps
import calendar
import sqlite3

from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = "change-this-to-any-long-random-text"  # needed for login sessions

DATABASE = "periodly.db"
FLOWS = ["spotting", "light", "medium", "heavy"]
SYMPTOMS = [
    "Cramps", "Headache", "Bloating", "Fatigue", "Mood swings",
    "Back pain", "Nausea", "Acne", "Breast tenderness", "Cravings",
]


# ---------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------

def get_db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row  # lets us use row["name"]
    return db


def init_db():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            flow TEXT NOT NULL,
            symptoms TEXT NOT NULL DEFAULT ''
        )
    """)
    db.commit()
    db.close()


init_db()


# ---------------------------------------------------------------
# SMALL HELPERS
# ---------------------------------------------------------------

def parse_date(text):
    return datetime.strptime(text, "%Y-%m-%d").date()


@app.template_filter("pretty")
def pretty_date(d):
    # date(2026, 10, 3) -> "Oct 3, 2026"
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("Please sign in first.", "error")
            return redirect(url_for("signin"))
        return view(*args, **kwargs)
    return wrapper


def make_period(row):
    """Turn a database row into a simple dictionary with real dates."""
    start = parse_date(row["start_date"])
    end = parse_date(row["end_date"])
    return {
        "id": row["id"],
        "start": start,
        "end": end,
        "duration": (end - start).days + 1,
        "flow": row["flow"],
        "symptoms": row["symptoms"].split(",") if row["symptoms"] else [],
    }


def get_periods(user_id):
    """All of a user's periods, newest first."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM periods WHERE user_id = ? ORDER BY start_date DESC",
        (user_id,),
    ).fetchall()
    db.close()
    return [make_period(row) for row in rows]


def read_period_form():
    """Read and check the log/edit form. Returns (data, error_message)."""
    flow = request.form.get("flow", "")
    symptoms = [s for s in request.form.getlist("symptoms") if s in SYMPTOMS]

    try:
        start = parse_date(request.form.get("start_date", ""))
        end = parse_date(request.form.get("end_date", ""))
    except ValueError:
        return None, "Please choose a valid start and end date."

    if end < start:
        return None, "The end date can't be before the start date."
    if start > date.today():
        return None, "The start date can't be in the future."
    if (end - start).days > 14:
        return None, "That period looks too long. Please check your dates."
    if flow not in FLOWS:
        return None, "Please choose a flow."

    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "flow": flow,
        "symptoms": ",".join(symptoms),
    }, None


def form_values():
    """Keep what the user typed so the form doesn't reset after an error."""
    return {
        "start_date": request.form.get("start_date", ""),
        "end_date": request.form.get("end_date", ""),
        "flow": request.form.get("flow", "medium"),
        "symptoms": request.form.getlist("symptoms"),
    }


# ---------------------------------------------------------------
# CYCLE MATHS
# ---------------------------------------------------------------

def get_cycle_info(periods):
    """Work out predictions from logged periods (periods is newest first)."""
    today = date.today()
    last_start = periods[0]["start"]

    # Cycle length = days between one period start and the next.
    gaps = []
    for i in range(len(periods) - 1):
        gap = (periods[i]["start"] - periods[i + 1]["start"]).days
        if 15 <= gap <= 60:  # ignore gaps that are clearly missing logs
            gaps.append(gap)

    cycle_known = len(gaps) > 0
    cycle_length = round(sum(gaps) / len(gaps)) if cycle_known else 28
    avg_period = round(sum(p["duration"] for p in periods) / len(periods))

    # Next period
    next_start = last_start + timedelta(days=cycle_length)
    days_until = (next_start - today).days
    if days_until > 1:
        next_text = f"in {days_until} days"
    elif days_until == 1:
        next_text = "tomorrow"
    elif days_until == 0:
        next_text = "today"
    elif days_until == -1:
        next_text = "1 day late"
    else:
        next_text = f"{-days_until} days late"

    # Ovulation happens about 14 days before the next period.
    ovulation_days = []
    ov = last_start + timedelta(days=cycle_length - 14)
    while ov < today + timedelta(days=180):
        ovulation_days.append(ov)
        ov += timedelta(days=cycle_length)

    # The next ovulation that hasn't fully passed yet
    upcoming_ov = ovulation_days[0]
    for ov in ovulation_days:
        if ov + timedelta(days=1) >= today:
            upcoming_ov = ov
            break

    # Where are we today?
    cycle_day = (today - last_start).days + 1
    progress = min(100, round(cycle_day / cycle_length * 100))

    phase = None
    if any(p["start"] <= today <= p["end"] for p in periods):
        phase = "Period"
    else:
        for ov in ovulation_days:
            if today == ov:
                phase = "Ovulation day"
            elif ov - timedelta(days=5) <= today <= ov + timedelta(days=1):
                phase = "Fertile window"
    if phase is None:
        first_ov = ovulation_days[0]
        phase = "Follicular phase" if today < first_ov - timedelta(days=5) else "Luteal phase"

    return {
        "cycle_length": cycle_length,
        "cycle_known": cycle_known,
        "avg_period": avg_period,
        "cycle_day": cycle_day,
        "progress": progress,
        "phase": phase,
        "next_start": next_start,
        "next_text": next_text,
        "ovulation_days": ovulation_days,
        "ovulation": upcoming_ov,
        "fertile_start": upcoming_ov - timedelta(days=5),
        "fertile_end": upcoming_ov + timedelta(days=1),
    }


def build_calendar(first_of_month, periods, info):
    """Return a flat list of calendar cells for one month."""
    period_days = set()
    for p in periods:
        for i in range(p["duration"]):
            period_days.add(p["start"] + timedelta(days=i))

    ovulation_set = set(info["ovulation_days"])
    fertile_set = set()
    for ov in info["ovulation_days"]:
        for i in range(-5, 2):
            fertile_set.add(ov + timedelta(days=i))

    today = date.today()
    cells = []
    # firstweekday=6 means the week starts on Sunday
    weeks = calendar.Calendar(firstweekday=6)
    for d in weeks.itermonthdates(first_of_month.year, first_of_month.month):
        if d.month != first_of_month.month:
            cells.append({"empty": True, "classes": "empty", "day": ""})
            continue
        classes = []
        if d in period_days:
            classes.append("period-day")
        elif d in ovulation_set:
            classes.append("ovulation-day")
        elif d in fertile_set:
            classes.append("fertile-day")
        if d == today:
            classes.append("today")
        cells.append({"empty": False, "classes": " ".join(classes), "day": d.day})
    return cells


# ---------------------------------------------------------------
# ACCOUNT PAGES
# ---------------------------------------------------------------

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("signin"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not name or not email or not password:
            flash("Please fill in every field.", "error")
        elif len(password) < 6:
            flash("Your password must be at least 6 characters.", "error")
        elif password != confirm:
            flash("The two passwords don't match.", "error")
        else:
            db = get_db()
            try:
                cursor = db.execute(
                    "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                    (name, email, generate_password_hash(password)),
                )
                db.commit()
                session["user_id"] = cursor.lastrowid
                session["user_name"] = name
                flash("Welcome to Periodly! 🌸", "success")
                return redirect(url_for("dashboard"))
            except sqlite3.IntegrityError:
                flash("An account with that email already exists.", "error")
            finally:
                db.close()

    return render_template("register.html")


@app.route("/signin", methods=["GET", "POST"])
def signin():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        db.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            return redirect(url_for("dashboard"))
        flash("Wrong email or password.", "error")

    return render_template("signin.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You've been signed out.", "success")
    return redirect(url_for("signin"))


# ---------------------------------------------------------------
# TRACKER PAGES
# ---------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    periods = get_periods(session["user_id"])
    if not periods:
        return render_template("dashboard.html", info=None)

    info = get_cycle_info(periods)

    # Which month is the calendar showing? e.g. /dashboard?month=2026-11
    try:
        first = datetime.strptime(request.args.get("month", ""), "%Y-%m").date()
    except ValueError:
        first = date.today().replace(day=1)
    prev_month = (first - timedelta(days=1)).replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)

    return render_template(
        "dashboard.html",
        info=info,
        recent=periods[:3],
        cells=build_calendar(first, periods, info),
        month_label=first.strftime("%B %Y"),
        prev_month=prev_month.strftime("%Y-%m"),
        next_month=next_month.strftime("%Y-%m"),
    )


@app.route("/log", methods=["GET", "POST"])
@login_required
def log_period():
    if request.method == "POST":
        data, error = read_period_form()
        if error:
            flash(error, "error")
            return render_template(
                "log_period.html", editing=False, values=form_values(),
                flows=FLOWS, symptoms=SYMPTOMS, today=date.today().isoformat(),
            )

        db = get_db()
        db.execute(
            "INSERT INTO periods (user_id, start_date, end_date, flow, symptoms) "
            "VALUES (?, ?, ?, ?, ?)",
            (session["user_id"], data["start_date"], data["end_date"],
             data["flow"], data["symptoms"]),
        )
        db.commit()
        db.close()
        flash("Period logged.", "success")
        return redirect(url_for("dashboard"))

    values = {"start_date": "", "end_date": "", "flow": "medium", "symptoms": []}
    return render_template(
        "log_period.html", editing=False, values=values,
        flows=FLOWS, symptoms=SYMPTOMS, today=date.today().isoformat(),
    )


@app.route("/edit/<int:period_id>", methods=["GET", "POST"])
@login_required
def edit_period(period_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM periods WHERE id = ? AND user_id = ?",
        (period_id, session["user_id"]),
    ).fetchone()

    if row is None:
        db.close()
        flash("That period wasn't found.", "error")
        return redirect(url_for("history"))

    if request.method == "POST":
        data, error = read_period_form()
        if error:
            db.close()
            flash(error, "error")
            return render_template(
                "log_period.html", editing=True, values=form_values(),
                flows=FLOWS, symptoms=SYMPTOMS, today=date.today().isoformat(),
            )

        db.execute(
            "UPDATE periods SET start_date = ?, end_date = ?, flow = ?, symptoms = ? "
            "WHERE id = ? AND user_id = ?",
            (data["start_date"], data["end_date"], data["flow"],
             data["symptoms"], period_id, session["user_id"]),
        )
        db.commit()
        db.close()
        flash("Period updated.", "success")
        return redirect(url_for("history"))

    db.close()
    period = make_period(row)
    values = {
        "start_date": period["start"].isoformat(),
        "end_date": period["end"].isoformat(),
        "flow": period["flow"],
        "symptoms": period["symptoms"],
    }
    return render_template(
        "log_period.html", editing=True, values=values,
        flows=FLOWS, symptoms=SYMPTOMS, today=date.today().isoformat(),
    )


@app.route("/delete/<int:period_id>", methods=["POST"])
@login_required
def delete_period(period_id):
    db = get_db()
    db.execute(
        "DELETE FROM periods WHERE id = ? AND user_id = ?",
        (period_id, session["user_id"]),
    )
    db.commit()
    db.close()
    flash("Period deleted.", "success")
    return redirect(url_for("history"))


@app.route("/history")
@login_required
def history():
    periods = get_periods(session["user_id"])

    # How many days since the previous period started?
    for i, p in enumerate(periods):
        if i + 1 < len(periods):
            p["since_previous"] = (p["start"] - periods[i + 1]["start"]).days
        else:
            p["since_previous"] = None

    info = get_cycle_info(periods) if periods else None
    return render_template("history.html", periods=periods, info=info)


if __name__ == "__main__":
    app.run(debug=True)
