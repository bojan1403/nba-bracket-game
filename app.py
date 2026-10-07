import hmac, json, math, os, smtplib, urllib.request
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from functools import wraps
import pyodbc
from flask import Flask, request, session, redirect, render_template_string, flash, g
from itsdangerous import URLSafeTimedSerializer, BadSignature
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
PROD = "WEBSITE_SITE_NAME" in os.environ            # Azure App Service sets this automatically
app.secret_key = os.environ.get("SECRET_KEY", "dev-change-me")
if PROD and app.secret_key == "dev-change-me":
    raise RuntimeError("Set SECRET_KEY in App Settings")
app.config.update(SESSION_COOKIE_SECURE=PROD, SESSION_COOKIE_SAMESITE="Lax")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")   # public https URL, used in email links
if PROD and (not BASE_URL or not os.environ.get("SMTP_HOST")):
    raise RuntimeError("Set BASE_URL and SMTP_* in App Settings (needed for confirmation and reset emails)")
ser_verify = URLSafeTimedSerializer(app.secret_key, salt="verify-email")
ser_reset = URLSafeTimedSerializer(app.secret_key, salt="reset-password")

EAST = ["Atlanta Hawks", "Boston Celtics", "Brooklyn Nets", "Charlotte Hornets", "Chicago Bulls",
        "Cleveland Cavaliers", "Detroit Pistons", "Indiana Pacers", "Miami Heat", "Milwaukee Bucks",
        "New York Knicks", "Orlando Magic", "Philadelphia 76ers", "Toronto Raptors", "Washington Wizards"]
WEST = ["Dallas Mavericks", "Denver Nuggets", "Golden State Warriors", "Houston Rockets", "LA Clippers",
        "Los Angeles Lakers", "Memphis Grizzlies", "Minnesota Timberwolves", "New Orleans Pelicans",
        "Oklahoma City Thunder", "Phoenix Suns", "Portland Trail Blazers", "Sacramento Kings",
        "San Antonio Spurs", "Utah Jazz"]

# ---------- database (Azure SQL via the AZURE_SQL_CONN app setting) ----------
SCHEMA = """
IF OBJECT_ID('dbo.users','U') IS NULL CREATE TABLE dbo.users(
  email NVARCHAR(254) PRIMARY KEY, name NVARCHAR(40) NOT NULL, password_hash NVARCHAR(255) NOT NULL,
  verified BIT NOT NULL DEFAULT 0, last_mail DATETIME2 NULL,
  failed_logins INT NOT NULL DEFAULT 0, locked_until DATETIME2 NULL, created DATETIME2 DEFAULT SYSUTCDATETIME());
IF OBJECT_ID('dbo.predictions','U') IS NULL CREATE TABLE dbo.predictions(
  email NVARCHAR(254) PRIMARY KEY, east NVARCHAR(MAX), west NVARCHAR(MAX), updated DATETIME2 DEFAULT SYSUTCDATETIME());
IF OBJECT_ID('dbo.standings','U') IS NULL CREATE TABLE dbo.standings(
  conf CHAR(1) NOT NULL, pos INT NOT NULL, team NVARCHAR(100) NOT NULL, wins INT, losses INT,
  updated DATETIME2 DEFAULT SYSUTCDATETIME(), PRIMARY KEY(conf, pos));
"""
_schema_ready = False

def db():
    global _schema_ready
    if "db" not in g:
        g.db = pyodbc.connect(os.environ["AZURE_SQL_CONN"])
        if not _schema_ready:                       # create the tables once per process
            g.db.execute(SCHEMA)
            g.db.commit()
            _schema_ready = True
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()

# ---------- season start, lock and late penalty ----------
LOCK_DAYS = 5                                       # picks lock 5 days after the season starts

def season_start():
    v = os.environ.get("SEASON_START")              # UTC, e.g. 2026-10-20T23:00:00Z ; unset = no lock, no penalty
    if not v:
        return None
    t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

def lock_time():
    s = season_start()
    return s + timedelta(days=LOCK_DAYS) if s else None

def is_locked():
    t = lock_time()
    return bool(t and datetime.now(timezone.utc) >= t)

def days_late(saved):
    """Started days between the season start and a save: 0 if before it, 1 within the first 24 h, ... max LOCK_DAYS."""
    s = season_start()
    if not s or saved is None:
        return 0
    if saved.tzinfo is None:                        # SQL returns naive UTC datetimes
        saved = saved.replace(tzinfo=timezone.utc)
    if saved <= s:
        return 0
    return min(LOCK_DAYS, math.ceil((saved - s) / timedelta(days=1)))

# ---------- scoring ----------
ZONE = 10                                           # positions 1-10 = playoff / play-in zone
PTS_ZONE, PTS_EXACT_ZONE = 3, 6                     # top 10: right zone / exact spot
PTS_OUT, PTS_EXACT_OUT = 1, 3                       # spots 11-15: right zone / exact spot

def score_conference(pred, actual):
    """pred / actual: lists of team names, index 0 = position 1."""
    pts = 0
    for i, team in enumerate(pred, start=1):
        if team not in actual:                      # standings not loaded yet
            continue
        j = actual.index(team) + 1                  # where the team really is
        if j == i:                                  # exact spot
            pts += PTS_EXACT_ZONE if i <= ZONE else PTS_EXACT_OUT
        elif i <= ZONE and j <= ZONE:               # top-10 team, right zone, wrong spot
            pts += PTS_ZONE
        elif i > ZONE and j > ZONE:                 # 11-15 team, right zone, wrong spot
            pts += PTS_OUT
    return pts

def compute_score(pred_east, pred_west, actual_east, actual_west):
    return score_conference(pred_east, actual_east) + score_conference(pred_west, actual_west)

def leaderboard(actual):
    rows = db().execute("SELECT u.name, p.east, p.west, p.updated FROM users u JOIN predictions p ON p.email = u.email").fetchall()
    board = []
    for r in rows:
        late = days_late(r.updated)                 # updated = submission time (picks can't be edited)
        raw = compute_score(json.loads(r.east), json.loads(r.west), actual["E"], actual["W"])
        board.append((r.name, raw - late, late))
    return sorted(board, key=lambda x: (-x[1], x[0].lower()))

# ---------- standings provider (ESPN's public JSON feed, no API key; swap this one function to change provider) ----------
ESPN_URL = "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"

def fetch_standings():
    """Returns {"E": [(seed, team, wins, losses), ...], "W": [...]} sorted by conference seed."""
    req = urllib.request.Request(ESPN_URL, headers={"User-Agent": "nba-predictions/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    out = {"E": [], "W": []}
    for conf in data.get("children", []):
        c = (conf.get("abbreviation") or conf.get("name") or "")[:1].upper()      # "East" / "West"
        if c not in out:
            continue
        for e in conf.get("standings", {}).get("entries", []):
            st = {x.get("name"): x.get("value") for x in e.get("stats", [])}
            seed = int(st.get("playoffSeed") or 0)
            out[c].append((seed if seed > 0 else 99, -(st.get("winPercent") or 0.0),
                           e["team"]["displayName"], int(st.get("wins") or 0), int(st.get("losses") or 0)))
    meta = {}                                       # which season the feed is showing (seasonType 2 = regular season)
    for conf in data.get("children", []):
        st = conf.get("standings", {})
        if "seasonType" in st:
            meta = {"season": st.get("seasonDisplayName") or st.get("season"), "season_type": st.get("seasonType")}
            break
    # sort by seed (win % as tie-break), then keep (seed-position, team, wins, losses)
    res = {c: [(i, t[2], t[3], t[4]) for i, t in enumerate(sorted(v), start=1)] for c, v in out.items()}
    res["meta"] = meta
    return res

@app.post("/internal/refresh-standings")           # called once a day by a scheduled job
def refresh_standings():
    token = os.environ.get("REFRESH_TOKEN", "")
    sent = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not token or not hmac.compare_digest(sent.encode(), token.encode()):
        return "Forbidden", 403
    start = season_start()
    if start and datetime.now(timezone.utc) < start and not request.args.get("force"):
        # Before tip-off the feed shows LAST season's final table; loading it would score players against the wrong season.
        return {"ok": True, "skipped": "season has not started yet (use ?force=1 to load anyway, for testing)"}
    data = fetch_standings()
    meta = data["meta"]
    if meta.get("season_type") not in (None, 2) and not request.args.get("force"):
        # The feed also serves PRESEASON tables (seasonType 1); scoring players against those would be wrong.
        return {"ok": True, "skipped": "feed is not showing regular-season standings", "feed": meta}
    if len(data["E"]) != 15 or len(data["W"]) != 15:
        return f"Unexpected data from provider, nothing updated (got {len(data['E'])} East and {len(data['W'])} West teams, expected 15 each)", 502
    for c, names in (("E", EAST), ("W", WEST)):     # scoring matches teams by name, so names must agree
        diff = {t[1] for t in data[c]} ^ set(names)
        if diff:
            return f"Provider team names differ from the dropdown lists, nothing updated: {sorted(diff)}", 502
    if sum(t[2] + t[3] for c in ("E", "W") for t in data[c]) == 0:
        return {"ok": True, "skipped": "no games played yet, standings left unchanged", "feed": meta}
    d = db()
    d.execute("DELETE FROM standings")
    for c in ("E", "W"):
        for pos, team, w, l in data[c]:
            d.execute("INSERT INTO standings(conf, pos, team, wins, losses) VALUES(?,?,?,?,?)", (c, pos, team, w, l))
    d.commit()
    return {"ok": True, "loaded": meta}

# ---------- email: confirmation and password reset ----------
def send_mail(to, subject, body):
    host = os.environ.get("SMTP_HOST")
    if not host:                                    # local dev: print the email instead of sending it
        app.logger.warning("SMTP not configured. Email to %s | %s\n%s", to, subject, body)
        return True
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, to
    msg["From"] = os.environ.get("MAIL_FROM", os.environ.get("SMTP_USER", "noreply@example.com"))
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587)), timeout=20) as srv:
            srv.starttls()
            srv.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            srv.send_message(msg)
        return True
    except Exception:
        app.logger.exception("Sending email to %s failed", to)
        return False

def abs_link(path):
    return (BASE_URL or request.url_root.rstrip("/")) + path

def may_send(email):
    """At most one email per address per 60 s, so the forms can't be used to spam someone's inbox."""
    cur = db().execute("UPDATE users SET last_mail = SYSUTCDATETIME() WHERE email=? "
                       "AND (last_mail IS NULL OR last_mail < DATEADD(second, -60, SYSUTCDATETIME()))", (email,))
    db().commit()
    return cur.rowcount == 1

def send_verification(email):
    link = abs_link("/verify/" + ser_verify.dumps(email))
    return send_mail(email, "Confirm your email",
                     f"Welcome! Confirm your email to activate your account (link valid for 24 hours):\n\n{link}\n")

def send_reset(email, password_hash):
    # The token carries the tail of the current password hash, so it stops working once the password changes.
    link = abs_link("/reset/" + ser_reset.dumps([email, password_hash[-16:]]))
    return send_mail(email, "Reset your password",
                     f"Use this link to choose a new password (valid for 1 hour, works once):\n\n{link}\n\n"
                     "If you didn't ask for this, you can ignore this email.\n")

# ---------- pages ----------
LAYOUT = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>NBA Predictions</title>
<style>
body{font:16px/1.5 system-ui,sans-serif;max-width:760px;margin:0 auto;padding:16px;color:#1a1a1a}
fieldset{margin:0 0 16px;padding:12px 16px;border:1px solid #ccc;border-radius:8px}
.row{display:flex;gap:8px;align-items:center;margin:6px 0}.row b{width:2.2em;text-align:right}
select,input{padding:8px;font:inherit;flex:1;min-width:0}
button{padding:10px 18px;font:inherit;font-weight:600;cursor:pointer}
.msg{background:#fff3cd;padding:8px 12px;border-radius:6px;margin:8px 0}
.two{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
table{width:100%;border-collapse:collapse}td,th{padding:4px 6px;border-bottom:1px solid #eee;text-align:left}
nav{display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.muted{color:#666;font-size:.9rem}
</style></head><body>
<nav><a href="/"><b>NBA Predictions</b></a>{% if name %}<span><a href="/standings">Standings</a> · <a href="/predict">My picks</a> · {{ name }} · <a href="/logout">Log out</a></span>{% endif %}</nav>
{% for m in get_flashed_messages() %}<div class="msg">{{ m }}</div>{% endfor %}
{{ body|safe }}</body></html>"""

HOME = """<h1>Predict the final NBA standings</h1>
<p>Rank all 30 teams, 1 to 15 in the East and 1 to 15 in the West, and see how you stack up against everyone else.</p>
<div class="two">
<form method="post" action="/register"><fieldset><legend>Create account</legend>
<div class="row"><input name="name" placeholder="Display name" maxlength="40" required></div>
<div class="row"><input type="email" name="email" placeholder="Email" required autocomplete="email"></div>
<div class="row"><input type="password" name="password" placeholder="Password (8+ characters)" minlength="8" required autocomplete="new-password"></div>
<button>Register</button></fieldset></form>
<form method="post" action="/login"><fieldset><legend>Log in</legend>
<div class="row"><input type="email" name="email" placeholder="Email" required autocomplete="email"></div>
<div class="row"><input type="password" name="password" placeholder="Password" required autocomplete="current-password"></div>
<button>Log in</button></fieldset></form></div>
<details><summary>Forgot your password?</summary><form method="post" action="/forgot">
<div class="row"><input type="email" name="email" placeholder="Your email" required><button>Send reset link</button></div></form></details>
<details><summary>Didn't get the confirmation email?</summary><form method="post" action="/resend">
<div class="row"><input type="email" name="email" placeholder="Your email" required><button>Send again</button></div></form></details>"""

RESET = """<h1>Choose a new password</h1>
<form method="post"><fieldset><legend>New password</legend>
<div class="row"><input type="password" name="password" placeholder="New password (8+ characters)" minlength="8" required autocomplete="new-password"></div>
<button>Set password</button></fieldset></form>"""

PREDICT = """<h1>My predicted standings</h1>
{% if submitted %}<p><b>Your picks were submitted on {{ submitted_at.strftime('%Y-%m-%d %H:%M') }} UTC and are final.</b></p>{% endif %}
{% if start %}<p class="muted">Season starts {{ start.strftime('%Y-%m-%d %H:%M') }} UTC; new picks close {{ lock.strftime('%Y-%m-%d %H:%M') }} UTC.
Picks submitted after the season starts lose 1 point per day late (max 5).
{% if not submitted %}{% if locked %}<b>Picks are closed.</b>{% elif late_now %}<b>Submitting now is {{ late_now }} day(s) late: -{{ late_now }} point(s).</b>{% endif %}{% endif %}</p>{% endif %}
{% if not submitted and not locked %}<p class="muted">Once you submit, your picks can't be changed.</p>{% endif %}
<form method="post" onsubmit="return confirm('Submit your picks? They can\'t be changed afterwards.')">
{% for title, p, teams in confs %}
<fieldset><legend>{{ title }} Conference</legend>
{% for i in range(1, 16) %}<div class="row"><b>{{ i }}.</b>
<select name="{{ p }}{{ i }}" required {{ 'disabled' if locked or submitted }}><option value="">Choose a team…</option>
{% for t in teams %}<option {{ 'selected' if saved[p][i-1] == t }}>{{ t }}</option>{% endfor %}</select></div>{% endfor %}
</fieldset>{% endfor %}
{% if not locked and not submitted %}<button>Submit predictions</button>{% endif %}</form>
<script>
// Disable a team in other dropdowns once it's picked, so each team is used once per conference.
document.querySelectorAll('fieldset').forEach(fs=>{
  const sels=[...fs.querySelectorAll('select')];
  const sync=()=>{const used=sels.map(s=>s.value);
    sels.forEach(s=>[...s.options].forEach(o=>o.disabled=!!o.value&&used.includes(o.value)&&s.value!==o.value))};
  sels.forEach(s=>s.onchange=sync); sync();
});
</script>"""

STANDINGS = """<h1>Standings</h1>
{% if updated %}<p class="muted">NBA standings last updated {{ updated.strftime('%Y-%m-%d %H:%M') }} UTC</p>
{% else %}<p class="muted">NBA standings haven't been loaded yet.</p>{% endif %}
<div class="two">
{% for title, key in [('Eastern', 'E'), ('Western', 'W')] %}
<div><h2>{{ title }} Conference</h2><table>
{% for r in tables[key] %}<tr><td>{{ r.pos }}</td><td>{{ r.team }}</td><td>{{ r.wins }}-{{ r.losses }}</td></tr>
{% else %}<tr><td class="muted">No data yet</td></tr>{% endfor %}</table></div>{% endfor %}
</div>
<h2>Leaderboard</h2>
<p class="muted">Per team: top 10 (playoff/play-in zone): exact spot = 6 points, right zone but wrong spot = 3. Spots 11-15: exact spot = 3 points, right zone but wrong spot = 1. Picks submitted after the season starts lose 1 point per day late (max 5). Picks can't be changed once submitted.</p>
<table><tr><th>#</th><th>Player</th><th>Score</th><th>Late penalty</th></tr>
{% for n, s, late in board %}<tr><td>{{ loop.index }}</td><td>{{ n }}</td><td>{{ s }}</td><td class="muted">{% if late %}-{{ late }}{% endif %}</td></tr>{% endfor %}</table>"""

def page(tpl, **ctx):
    body = render_template_string(tpl, **ctx)
    return render_template_string(LAYOUT, body=body, name=session.get("name"))

def login_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not session.get("email"):
            flash("Please log in first.")
            return redirect("/")
        return f(*a, **k)
    return wrapper

def has_prediction(email):
    return db().execute("SELECT 1 FROM predictions WHERE email=?", (email,)).fetchone() is not None

def landing():
    """Where a logged-in user belongs: the form until they've filled it, then the standings."""
    return redirect("/standings" if has_prediction(session["email"]) or is_locked() else "/predict")

def start_session(email, name):
    session.clear()                                 # fresh session on every login
    session["email"], session["name"] = email, name

@app.get("/")
def home():
    return landing() if session.get("email") else page(HOME)

@app.post("/register")
def register():
    email = request.form["email"].strip().lower()
    name = request.form["name"].strip()
    pw = request.form["password"]
    if "@" not in email or len(email) > 254 or not 1 <= len(name) <= 40 or not 8 <= len(pw) <= 200:
        flash("Enter a display name, a valid email, and a password of 8 to 200 characters.")
        return redirect("/")
    try:
        db().execute("INSERT INTO users(email, name, password_hash, verified) VALUES(?,?,?,0)",
                     (email, name, generate_password_hash(pw)))
        db().commit()
    except pyodbc.IntegrityError:                   # email is the primary key
        flash("That email is already registered. Please log in or reset your password.")
        return redirect("/")
    may_send(email)                                 # starts the 60 s throttle for this address
    if send_verification(email):
        flash(f"Almost there! We sent a confirmation link to {email}. Click it to activate your account.")
    else:
        flash("Account created, but we couldn't send the confirmation email. Use \"Didn't get the confirmation email?\" below.")
    return redirect("/")

LOGIN_MAX_FAILS = 5                                 # consecutive failed logins before the account locks
LOCKOUT_MINUTES = 15

@app.post("/login")
def login():
    email = request.form["email"].strip().lower()
    row = db().execute(
        "SELECT name, password_hash, verified, failed_logins, "
        "CASE WHEN locked_until > SYSUTCDATETIME() THEN DATEDIFF(minute, SYSUTCDATETIME(), locked_until) + 1 ELSE 0 END AS lock_min "
        "FROM users WHERE email=?", (email,)).fetchone()
    if row and row.lock_min:                        # locked: don't even test the password
        flash(f"Too many failed attempts. Try again in {row.lock_min} minute(s), or reset your password.")
        return redirect("/")
    if not row or not check_password_hash(row.password_hash, request.form["password"]):
        if row:
            # In T-SQL every SET expression sees the pre-update values, so this is one atomic step.
            # Reaching the limit locks the account and zeroes the counter (a fresh set of tries after the lock).
            db().execute("UPDATE users SET "
                         "failed_logins = CASE WHEN failed_logins + 1 >= ? THEN 0 ELSE failed_logins + 1 END, "
                         "locked_until = CASE WHEN failed_logins + 1 >= ? THEN DATEADD(minute, ?, SYSUTCDATETIME()) ELSE locked_until END "
                         "WHERE email=?", (LOGIN_MAX_FAILS, LOGIN_MAX_FAILS, LOCKOUT_MINUTES, email))
            db().commit()
        if row and row.failed_logins + 1 >= LOGIN_MAX_FAILS:
            flash(f"Too many failed attempts. This account is locked for {LOCKOUT_MINUTES} minutes. You can also reset your password.")
        else:
            flash("Wrong email or password.")
        return redirect("/")
    if row.failed_logins:                           # correct password: the streak of failures is over
        db().execute("UPDATE users SET failed_logins=0, locked_until=NULL WHERE email=?", (email,))
        db().commit()
    if not row.verified:
        flash("Please confirm your email first (check your inbox), or request a new link below.")
        return redirect("/")
    start_session(email, row.name)
    return landing()

@app.get("/verify/<token>")
def verify(token):
    try:
        email = ser_verify.loads(token, max_age=86400)
    except BadSignature:                            # also covers expired tokens
        flash("That confirmation link is invalid or expired. Request a new one below.")
        return redirect("/")
    row = db().execute("SELECT name FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        flash("That account no longer exists.")
        return redirect("/")
    db().execute("UPDATE users SET verified=1 WHERE email=?", (email,))
    db().commit()
    start_session(email, row.name)
    return landing()                                # new user: no picks yet -> the form

@app.post("/resend")
def resend():
    email = request.form["email"].strip().lower()
    row = db().execute("SELECT verified FROM users WHERE email=?", (email,)).fetchone()
    if row and not row.verified and may_send(email):
        send_verification(email)
    flash("If that address is registered and not yet confirmed, we've sent a new link.")   # same answer either way
    return redirect("/")

@app.post("/forgot")
def forgot():
    email = request.form["email"].strip().lower()
    row = db().execute("SELECT password_hash FROM users WHERE email=?", (email,)).fetchone()
    if row and may_send(email):
        send_reset(email, row.password_hash)
    flash("If that address is registered, we've sent a password reset link.")               # same answer either way
    return redirect("/")

@app.route("/reset/<token>", methods=["GET", "POST"])
def reset(token):
    bad = "That reset link is invalid or expired. Request a new one."
    try:
        email, tail = ser_reset.loads(token, max_age=3600)
    except (BadSignature, ValueError):
        flash(bad)
        return redirect("/")
    row = db().execute("SELECT password_hash FROM users WHERE email=?", (email,)).fetchone()
    if not row or row.password_hash[-16:] != tail:  # password already changed: the link is single-use
        flash(bad)
        return redirect("/")
    if request.method == "POST":
        pw = request.form["password"]
        if not 8 <= len(pw) <= 200:
            flash("Password must be 8 to 200 characters.")
            return redirect(request.path)
        db().execute("UPDATE users SET password_hash=?, verified=1, failed_logins=0, locked_until=NULL WHERE email=?", (generate_password_hash(pw), email))
        db().commit()                               # resetting via the inbox also proves the email address
        flash("Password updated. You can log in now.")
        return redirect("/")
    return page(RESET)

@app.route("/predict", methods=["GET", "POST"])
@login_required
def predict():
    email = session["email"]
    row = db().execute("SELECT east, west, updated FROM predictions WHERE email=?", (email,)).fetchone()
    if request.method == "POST":
        if row:
            flash("You've already submitted your picks. They can't be changed.")
            return redirect("/predict")
        if is_locked():
            flash("Predictions are closed.")
            return redirect("/predict")
        east = [request.form.get(f"e{i}") for i in range(1, 16)]
        west = [request.form.get(f"w{i}") for i in range(1, 16)]
        if sorted(east) != sorted(EAST) or sorted(west) != sorted(WEST):
            flash("Each of the 15 teams must be picked exactly once in each conference.")
            return redirect("/predict")
        try:                                        # INSERT only; the primary key (email) guarantees one submission
            db().execute("INSERT INTO predictions(email, east, west) VALUES(?,?,?)",
                         (email, json.dumps(east), json.dumps(west)))
            db().commit()
        except pyodbc.IntegrityError:               # double click or second tab: the first submit wins
            flash("You've already submitted your picks. They can't be changed.")
            return redirect("/predict")
        late = days_late(datetime.now(timezone.utc))
        flash("Submitted! Your picks are final." + (f" Submitted {late} day(s) late: -{late} point(s)." if late else ""))
        return redirect("/standings")
    saved = {"e": json.loads(row.east), "w": json.loads(row.west)} if row else {"e": [""] * 15, "w": [""] * 15}
    return page(PREDICT, saved=saved, confs=[("Eastern", "e", EAST), ("Western", "w", WEST)],
                start=season_start(), lock=lock_time(), locked=is_locked(), late_now=days_late(datetime.now(timezone.utc)),
                submitted=row is not None, submitted_at=row.updated if row else None)

@app.get("/standings")
@login_required
def standings():
    if not has_prediction(session["email"]):
        if not is_locked():
            flash("Fill in your predictions first.")
            return redirect("/predict")
        flash("Predictions are closed and you didn't submit any, so you're not on the leaderboard.")
    rows = db().execute("SELECT conf, pos, team, wins, losses, updated FROM standings ORDER BY conf, pos").fetchall()
    tables = {"E": [r for r in rows if r.conf == "E"], "W": [r for r in rows if r.conf == "W"]}
    actual = {k: [r.team for r in v] for k, v in tables.items()}
    return page(STANDINGS, tables=tables, updated=rows[0].updated if rows else None, board=leaderboard(actual))

@app.get("/logout")
def logout():
    session.clear()
    return redirect("/")

if __name__ == "__main__":
    app.run(debug=True)