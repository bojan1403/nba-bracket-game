import hmac, json, math, os, re, smtplib, unicodedata, urllib.request
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
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

# ---------- texts shown to players (from NBA_Predictions_Serbian_texts.docx; footer, privacy note and award names are kept in English) ----------
T = {
    "forms.day": "dan / dana / dana",
    "forms.point": "poen / poena / poena",
    "forms.minute": "minut / minuta / minuta",
    "nav.brand": "NBA Kostur",
    "nav.standings": "Tabela",
    "nav.mypicks": "Moj kostur",
    "nav.rules": "Pravila",
    "nav.picks": "Kosturi",
    "rules.title": "Pravila",
    "picks.title": "Kosturi",
    "picks.legend": "Poređenje sa trenutnom NBA tabelom:",
    "picks.exact": "tačno mesto",
    "picks.zone": "prava zona",
    "picks.none": "Još niko nije poslao kostur.",
    "msg.picks.submitfirst": "Tuđe kosture možeš da vidiš tek kad pošalješ svoj.",
    "nav.admin": "Admin",
    "nav.logout": "Odjavi se",
    "footer": "Independent fan project, not affiliated with, endorsed by, or sponsored by the National Basketball Association or any of its teams. Team and player names are used only to identify them. Standings come from public sources and may contain errors.",
    "conf.east": "Istok",
    "conf.west": "Zapad",
    "home.title": "Pogodi konačnu NBA tabelu",
    "home.intro": "Poređaj svih 30 timova, od 1. do 15. mesta u Istočnoj i od 1. do 15. mesta u Zapadnoj konferenciji, i vidi kako stojiš u odnosu na ostale.",
    "home.reg.legend": "Napravi nalog",
    "home.reg.name": "Ime za prikaz",
    "home.email": "E-mail",
    "home.reg.password": "Lozinka (najmanje 8 karaktera)",
    "home.reg.button": "Registruj se",
    "home.privacy": "We store your display name, email address, a hashed password and your predictions, only to run this game, and we don’t share them.",
    "home.privacy.contact": "To have your data deleted, write to {contact}.",
    "home.login.legend": "Prijavi se",
    "home.login.password": "Lozinka",
    "home.login.button": "Prijavi se",
    "home.forgot.summary": "Zaboravljena lozinka?",
    "home.forgot.email": "Tvoj e-mail",
    "home.forgot.button": "Pošalji link za novu lozinku",
    "home.resend.summary": "Nije stigao e-mail za potvrdu?",
    "home.resend.button": "Pošalji ponovo",
    "msg.login_required": "Prvo se prijavi.",
    "msg.register.invalid": "Unesi ime za prikaz, ispravnu e-mail adresu i lozinku od 8 do 200 karaktera.",
    "msg.register.exists": "Ta e-mail adresa je već registrovana. Prijavi se ili resetuj lozinku.",
    "msg.register.sent": "Još malo! Poslali smo link za potvrdu na {email}. Klikni na njega da aktiviraš nalog.",
    "msg.register.mailfail": "Nalog je napravljen, ali nismo uspeli da pošaljemo e-mail za potvrdu. Probaj opciju „Nije stigao e-mail za potvrdu?“ ispod.",
    "msg.login.wrong": "Pogrešan e-mail ili lozinka.",
    "msg.login.unverified": "Prvo potvrdi e-mail (proveri mail) ili zatraži novi link ispod.",
    "msg.login.locknow": "Previše neuspešnih pokušaja. Nalog je zaključan na {minutes}. Možeš i da resetuješ lozinku.",
    "msg.login.locked": "Previše neuspešnih pokušaja. Probaj ponovo za {minutes} ili resetuj lozinku.",
    "msg.verify.invalid": "Link za potvrdu nije ispravan ili je istekao. Zatraži novi ispod.",
    "msg.verify.noaccount": "Taj nalog više ne postoji.",
    "msg.resend.done": "Ako je ta adresa registrovana i još nije potvrđena, poslali smo novi link.",
    "msg.forgot.done": "Ako je ta adresa registrovana, poslali smo link za resetovanje lozinke.",
    "reset.title": "Izaberi novu lozinku",
    "reset.legend": "Nova lozinka",
    "reset.placeholder": "Nova lozinka (najmanje 8 karaktera)",
    "reset.button": "Sačuvaj lozinku",
    "msg.reset.invalid": "Link za resetovanje nije ispravan ili je istekao. Zatraži novi.",
    "msg.reset.length": "Lozinka mora imati od 8 do 200 karaktera.",
    "msg.reset.done": "Lozinka je promenjena. Sada možeš da se prijaviš.",
    "email.verify.subject": "Potvrdi svoj e-mail",
    "email.verify.body": "Dobrodošli u igru! Potvrdi svoj e-mail da aktiviraš nalog (link važi 24 sata):\n\n{link}",
    "email.reset.subject": "Resetovanje lozinke",
    "email.reset.body": "Iskoristi ovaj link da izabereš novu lozinku (važi 1 sat i može se upotrebiti samo jednom):\n\n{link}\n\nAko resetovanje lozinke nije tvoj zahtev, slobodno ignoriši ovaj e-mail.",
    "predict.title": "Moj kostur",
    "predict.submitted": "Tvoj kostur je poslat {date} i konačan je.",
    "predict.season": "Sezona počinje {start}; nove kosture primamo do {lock}.",
    "predict.penalty": "Kosturi poslati nakon početka sezone gube 1 poen za svaki dan kašnjenja (najviše 5).",
    "predict.closed": "Prijem kostura je završen.",
    "predict.latenow": "Ako pošalješ sada, kasniš {days} i gubiš {points}.",
    "predict.finalhint": "Kad jednom pošalješ, kostur se ne može menjati.",
    "predict.select": "Izaberi tim…",
    "predict.awards.legend": "Individualne nagrade (po 2 poena)",
    "predict.awards.hint": "Upiši puno ime. Potrudi se da upišeš tačno ime.",
    "predict.button": "Pošalji kostur",
    "predict.confirm": "Da li želiš da pošalješ kostur? Posle slanja nema izmena.",
    "msg.predict.already": "Kostur je već poslat i ne može se menjati.",
    "msg.predict.closed": "Prijem kostura je završen.",
    "msg.predict.teams": "U svakoj konferenciji moraš izabrati svih 15 timova, svaki tačno jednom.",
    "msg.predict.awards": "Popuni sve nagrade (imena do 100 karaktera).",
    "msg.predict.ok": "Poslato! Tvoj kostur je konačan.",
    "msg.predict.ok.late": "Poslato sa kašnjenjem od {days}: gubiš {points}.",
    "rules.intro": "Bodovanje, po timu:",
    "rules.top10": "Mesta 1–10 (plej-of / plej-in zona): tačno mesto = 6 poena, prava zona ali pogrešno mesto = 3 poena.",
    "rules.low": "Mesta 11–15: tačno mesto = 3 poena, prava zona ali pogrešno mesto = 1 poen.",
    "rules.awards": "Svaka tačno pogođena individualna nagrada = 2 poena (ukupno 7 nagrada).",
    "rules.late": "Kostur poslat nakon početka sezone gubi 1 poen za svaki dan kašnjenja (najviše 5).",
    "rules.final": "Kad se kostur pošalje, ne može se menjati.",
    "rules.example": "Primer: LA Clippers staviš na 5. mesto Zapada. Ako završe na 5. mestu dobijaš 6 poena, na 7. mestu 3 poena, a na 12. mestu 0 poena.",
    "st.title": "Tabela",
    "st.updated": "NBA tabela poslednji put ažurirana: {date}",
    "st.notloaded": "NBA tabela još nije učitana.",
    "st.nodata": "Još nema podataka",
    "st.leaderboard": "Rang lista",
    "st.col.player": "Takmičar",
    "st.col.score": "Poeni",
    "st.col.awards": "Nagrade",
    "st.col.late": "Kazna za kašnjenje",
    "st.awards.title": "Individualne nagrade",
    "st.awards.tba": "biće objavljeno",
    "st.awards.empty": "Dobitnici će se pojaviti ovde čim budu proglašeni.",
    "msg.st.fillfirst": "Prvo popuni svoj kostur.",
    "msg.st.nopicks": "Prijem kostura je završen, a sa tvog naloga nije stigao nijedan, pa nisi na rang listi.",
    "award.mvp": "Most Valuable Player (MVP)",
    "award.roy": "Rookie of the Year",
    "award.dpoy": "Defensive Player of the Year",
    "award.smoy": "Sixth Man of the Year",
    "award.mip": "Most Improved Player",
    "award.coy": "Coach of the Year",
    "award.ppg": "Scoring leader (points per game)",
    "admin.title": "Dobitnici nagrada",
    "admin.intro": "Unesi zvanične dobitnike kad budu proglašeni. Ostavi polje prazno ako još nije odlučeno. U slučaju izjednačenja, razdvoji imena tačkom i zarezom (Ime A; Ime B). Bodovi se ažuriraju odmah.",
    "admin.legend": "Zvanični dobitnici",
    "admin.button": "Sačuvaj dobitnike",
    "msg.admin.ok": "Dobitnici su sačuvani.",
}

def plural(n, kind):
    """'1 dan', '2 dana', '5 dana': the Serbian noun form after a number. kind = day | point | minute."""
    forms = [f.strip() for f in T["forms." + kind].split("/")]
    n = int(n)
    last, last2 = n % 10, n % 100
    i = 0 if (last == 1 and last2 != 11) else 1 if (2 <= last <= 4 and not 12 <= last2 <= 14) else 2
    return f"{n} {forms[min(i, len(forms) - 1)]}"

def t(key, **kw):
    """Text by id; {placeholders} are filled from the keyword arguments."""
    s = T[key]
    for k, v in kw.items():
        s = s.replace("{" + k + "}", str(v))
    return s

def fmt_dt(d):
    if d.tzinfo is None:                            # SQL returns naive UTC datetimes
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(LOCAL_TZ).strftime("%d.%m.%Y. %H:%M")

def rules_text():
    keys = ("rules.intro", "rules.top10", "rules.low", "rules.awards", "rules.late", "rules.final", "rules.example")
    return " ".join(T[k] for k in keys if T.get(k))

app.jinja_env.globals.update(t=t, plural=plural, fmt_dt=fmt_dt, rules_text=rules_text)

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
IF OBJECT_ID('dbo.award_picks','U') IS NULL CREATE TABLE dbo.award_picks(
  email NVARCHAR(254) NOT NULL, award VARCHAR(10) NOT NULL, pick NVARCHAR(100) NOT NULL, PRIMARY KEY(email, award));
IF OBJECT_ID('dbo.award_results','U') IS NULL CREATE TABLE dbo.award_results(
  award VARCHAR(10) PRIMARY KEY, winner NVARCHAR(200) NOT NULL, updated DATETIME2 DEFAULT SYSUTCDATETIME());
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
LOCAL_TZ = ZoneInfo("Europe/Belgrade")                  # all times shown to users; handles summer/winter time
LOCK_DAYS = 5                                       # picks lock 5 days after the season starts

def season_start():
    v = os.environ.get("SEASON_START")              # e.g. 2026-10-21T01:00:00 (Belgrade time) or 2026-10-20T23:00:00Z; unset = no lock, no penalty
    if not v:
        return None
    t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return (t if t.tzinfo else t.replace(tzinfo=LOCAL_TZ)).astimezone(timezone.utc)   # no zone given = Belgrade time; kept in UTC so 5 days = exactly 120 h

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

# ---------- individual awards ----------
AWARDS = [("mvp", "Most Valuable Player (MVP)"), ("roy", "Rookie of the Year"), ("dpoy", "Defensive Player of the Year"),
          ("smoy", "Sixth Man of the Year"), ("mip", "Most Improved Player"), ("coy", "Coach of the Year"),
          ("ppg", "Scoring leader (points per game)")]
PTS_AWARD = 2

CYR = dict(zip("абвгдђежзијклљмнњопрстћуфхцчџш",
               ["a", "b", "v", "g", "d", "dj", "e", "z", "z", "i", "j", "k", "l", "lj", "m", "n", "nj", "o", "p", "r", "s", "t", "c", "u", "f", "h", "c", "c", "dz", "s"]))
EXTRA = {"đ": "dj", "ł": "l", "ø": "o", "æ": "ae", "ß": "ss"}

def norm_name(text):
    """'  Nikola  Jokić ', 'nikola jokic' and 'Никола Јокић' compare equal: lower-case, Cyrillic to Latin,
    no accents, punctuation as spaces."""
    low = (text or "").lower()
    low = "".join(CYR.get(ch) or EXTRA.get(ch) or ch for ch in low)
    low = unicodedata.normalize("NFKD", low)
    low = "".join(ch for ch in low if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", low).split())

def award_points(picks, results):
    """picks / results: {award_key: text}. A result may list several winners separated by ';' (ties)."""
    pts = 0
    for key, _ in AWARDS:
        pick = norm_name(picks.get(key, ""))
        winners = {norm_name(w) for w in (results.get(key) or "").split(";")} - {""}
        if pick and pick in winners:
            pts += PTS_AWARD
    return pts

def award_results():
    return {r.award: r.winner for r in db().execute("SELECT award, winner FROM award_results").fetchall()}

def team_marks(pred, actual):
    """Per predicted team: 'exact', 'zone' (right zone, wrong spot) or '' (miss / standings not loaded). Same rules as score_conference."""
    out = []
    for i, team in enumerate(pred, start=1):
        j = actual.index(team) + 1 if team in actual else 0
        out.append("" if not j else "exact" if j == i else "zone" if (i <= ZONE) == (j <= ZONE) else "")
    return out

def entries(actual):
    """Everyone who submitted, best first: score parts, their picks and how each pick is doing."""
    d = db()
    rows = d.execute("SELECT u.email, u.name, p.east, p.west, p.updated FROM users u JOIN predictions p ON p.email = u.email").fetchall()
    picks = {}
    for r in d.execute("SELECT email, award, pick FROM award_picks").fetchall():
        picks.setdefault(r.email, {})[r.award] = r.pick
    results = award_results()
    out = []
    for r in rows:
        east, west = json.loads(r.east), json.loads(r.west)
        late = days_late(r.updated)                 # updated = submission time (picks can't be edited)
        raw = compute_score(east, west, actual["E"], actual["W"])
        mine = picks.get(r.email, {})
        aw = award_points(mine, results)
        hit = {k: norm_name(mine.get(k, "")) in ({norm_name(w) for w in (results.get(k) or "").split(";")} - {""}) for k, _ in AWARDS}
        out.append(dict(email=r.email, name=r.name, total=raw + aw - late, late=late, aw=aw,
                        east=list(zip(east, team_marks(east, actual["E"]))), west=list(zip(west, team_marks(west, actual["W"]))),
                        awards=[(k, mine.get(k, ""), hit[k]) for k, _ in AWARDS]))
    return sorted(out, key=lambda e: (-e["total"], e["name"].lower()))

def leaderboard(actual):
    return [(e["name"], e["total"], e["late"], e["aw"]) for e in entries(actual)]

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
    return send_mail(email, t("email.verify.subject"), t("email.verify.body", link=link) + "\n")

def send_reset(email, password_hash):
    # The token carries the tail of the current password hash, so it stops working once the password changes.
    link = abs_link("/reset/" + ser_reset.dumps([email, password_hash[-16:]]))
    return send_mail(email, t("email.reset.subject"), t("email.reset.body", link=link) + "\n")

# ---------- admin (enter the official award winners) ----------
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "").strip()   # shown in the privacy note; optional
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}

def is_admin():
    return bool(session.get("email")) and session["email"] in ADMIN_EMAILS

# ---------- logo (a basketball whose seams are bones: kostur = skeleton) ----------
LOGO_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#14171c"/><circle cx="256" cy="256" r="172" fill="none" stroke="#ff7a33" stroke-width="9"/><path d="M256 92V420" fill="none" stroke="#ffffff" stroke-width="18" stroke-linecap="butt"/><circle cx="272.1" cy="90.0" r="17" fill="#ffffff"/><circle cx="239.8" cy="90.0" r="17" fill="#ffffff"/><circle cx="239.8" cy="422.0" r="17" fill="#ffffff"/><circle cx="272.1" cy="422.0" r="17" fill="#ffffff"/><path d="M92 256H420" fill="none" stroke="#ffffff" stroke-width="18" stroke-linecap="butt"/><circle cx="90.0" cy="239.8" r="17" fill="#ffffff"/><circle cx="90.0" cy="272.1" r="17" fill="#ffffff"/><circle cx="422.0" cy="272.1" r="17" fill="#ffffff"/><circle cx="422.0" cy="239.8" r="17" fill="#ffffff"/><path d="M166.7 113.1Q252.6 256 166.7 398.9" fill="none" stroke="#ffffff" stroke-width="18" stroke-linecap="butt"/><circle cx="179.5" cy="103.0" r="17" fill="#ffffff"/><circle cx="151.8" cy="119.7" r="17" fill="#ffffff"/><circle cx="151.8" cy="392.3" r="17" fill="#ffffff"/><circle cx="179.5" cy="409.0" r="17" fill="#ffffff"/><path d="M345.3 113.1Q259.4 256 345.3 398.9" fill="none" stroke="#ffffff" stroke-width="18" stroke-linecap="butt"/><circle cx="360.2" cy="119.7" r="17" fill="#ffffff"/><circle cx="332.5" cy="103.0" r="17" fill="#ffffff"/><circle cx="332.5" cy="409.0" r="17" fill="#ffffff"/><circle cx="360.2" cy="392.3" r="17" fill="#ffffff"/></svg>'''

# ---------- pages ----------
LAYOUT = """<!doctype html><html lang="sr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light dark">
<title>{{ t('nav.brand') }}</title><link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
:root{--bg:#f5f6f8;--card:#fff;--ink:#14171c;--muted:#5d6572;--line:#e2e5ea;--accent:#c9460a;--on-accent:#fff;
--exact:#d3f0dc;--zone:#fcebc0;--flash:#fff1d6;--r:12px}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a20;--ink:#eceef1;--muted:#9aa3af;--line:#272c35;--accent:#ff7a33;--on-accent:#1a0d05;
--exact:#17402a;--zone:#4a3a10;--flash:#3a2d12}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-text-size-adjust:100%}
a{color:var(--accent)}
.wrap{max-width:760px;margin:0 auto;padding:0 16px 40px}
header{background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.bar{max-width:760px;margin:0 auto;padding:10px 16px 0;display:flex;align-items:center;justify-content:space-between;gap:12px}
.brand{display:flex;align-items:center;gap:8px;font-weight:800;font-size:1.2rem;letter-spacing:-.02em;color:var(--ink);text-decoration:none}
.brand i{font-style:normal;color:var(--accent)}
.who{font-size:.85rem;color:var(--muted);display:flex;gap:10px;align-items:center;white-space:nowrap}
.who a{color:var(--muted)}
.tabs{max-width:760px;margin:0 auto;padding:0 8px;display:flex;overflow-x:auto;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tabs a{padding:10px 10px;color:var(--muted);text-decoration:none;font-weight:600;font-size:.95rem;white-space:nowrap;border-bottom:3px solid transparent}
.tabs a:hover{color:var(--ink)}
.tabs a.on{color:var(--ink);border-bottom-color:var(--accent)}
:focus-visible{outline:3px solid var(--accent);outline-offset:2px;border-radius:4px}
h1{font-size:1.75rem;line-height:1.2;letter-spacing:-.02em;margin:24px 0 8px}
h2{font-size:1.1rem;margin:20px 0 8px}
p{margin:0 0 12px}
.muted{color:var(--muted);font-size:.9rem}
fieldset,.card,details{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;margin:0 0 16px;min-width:0}
legend{font-weight:700;padding:0 6px;font-size:1rem}
.row{display:flex;gap:10px;align-items:center;margin:8px 0}
.row b{flex:0 0 2rem;height:2rem;line-height:2rem;text-align:center;border-radius:50%;background:var(--bg);font-size:.85rem;font-variant-numeric:tabular-nums}
select,input{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px 12px;flex:1;min-width:0;min-height:44px}
select:disabled,input:disabled{opacity:.75}
button{font:inherit;font-weight:700;min-height:44px;padding:10px 20px;border:0;border-radius:8px;background:var(--accent);color:var(--on-accent);cursor:pointer}
button:hover{filter:brightness(1.08)}
form > button:last-child{width:100%}
.msg{background:var(--flash);border:1px solid var(--line);border-left:4px solid var(--accent);padding:10px 14px;border-radius:8px;margin:14px 0 0}
.two{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
td,th{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left}
tr:last-child td{border-bottom:0}
th{font-size:.8rem;color:var(--muted);font-weight:600}
tr.cut td{border-bottom:2px dashed var(--accent)}
.rank{width:2.2rem;color:var(--muted)}
.board td.score{font-size:1.25rem;font-weight:800;text-align:right}
.board th:nth-child(n+3),.board td:nth-child(n+4){text-align:right}
summary{cursor:pointer;font-weight:700;min-height:28px}
details[open]>summary{margin-bottom:8px}
details.mine{border-color:var(--accent)}
ol.picks{margin:8px 0;padding-left:2.2rem}ol.picks li{padding:2px 6px;border-radius:6px;margin:2px 0}
.exact{background:var(--exact)}.zone{background:var(--zone)}
span.exact,span.zone{padding:2px 8px;border-radius:6px}
footer{margin-top:32px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:.8rem}
@media (max-width:480px){h1{font-size:1.5rem}fieldset,.card,details{padding:12px}}
@media (prefers-reduced-motion:no-preference){button{transition:filter .15s}}
</style></head><body>
<header><div class="bar"><a class="brand" href="/"><img src="/favicon.svg" alt="" width="30" height="30">NBA <i>Kostur</i></a>
{% if name %}<span class="who">{{ name }} <a href="/logout">{{ t('nav.logout') }}</a></span>{% endif %}</div>
<nav class="tabs">{% if name %}
<a href="/standings"{% if path == '/standings' %} class="on"{% endif %}>{{ t('nav.standings') }}</a>
<a href="/predict"{% if path == '/predict' %} class="on"{% endif %}>{{ t('nav.mypicks') }}</a>
<a href="/picks"{% if path == '/picks' %} class="on"{% endif %}>{{ t('nav.picks') }}</a>
<a href="/rules"{% if path == '/rules' %} class="on"{% endif %}>{{ t('nav.rules') }}</a>
{% if admin %}<a href="/admin"{% if path == '/admin' %} class="on"{% endif %}>{{ t('nav.admin') }}</a>{% endif %}
{% else %}<a href="/rules"{% if path == '/rules' %} class="on"{% endif %}>{{ t('nav.rules') }}</a>{% endif %}</nav></header>
<div class="wrap">
{% for m in get_flashed_messages() %}<div class="msg">{{ m }}</div>{% endfor %}
{{ body|safe }}
<footer>{{ t('footer') }}</footer>
</div></body></html>"""

HOME = """<h1>{{ t('home.title') }}</h1>
<p>{{ t('home.intro') }}</p>
<div class="two">
<form method="post" action="/register"><fieldset><legend>{{ t('home.reg.legend') }}</legend>
<div class="row"><input name="name" placeholder="{{ t('home.reg.name') }}" maxlength="40" required></div>
<div class="row"><input type="email" name="email" placeholder="{{ t('home.email') }}" required autocomplete="email"></div>
<div class="row"><input type="password" name="password" placeholder="{{ t('home.reg.password') }}" minlength="8" required autocomplete="new-password"></div>
<button>{{ t('home.reg.button') }}</button>
<p class="muted">{{ t('home.privacy') }}{% if contact %} {% set pc = t('home.privacy.contact').split('{contact}') %}{{ pc[0] }}<a href="mailto:{{ contact }}">{{ contact }}</a>{{ pc[1] }}{% endif %}</p></fieldset></form>
<form method="post" action="/login"><fieldset><legend>{{ t('home.login.legend') }}</legend>
<div class="row"><input type="email" name="email" placeholder="{{ t('home.email') }}" required autocomplete="email"></div>
<div class="row"><input type="password" name="password" placeholder="{{ t('home.login.password') }}" required autocomplete="current-password"></div>
<button>{{ t('home.login.button') }}</button></fieldset></form></div>
<details><summary>{{ t('home.forgot.summary') }}</summary><form method="post" action="/forgot">
<div class="row"><input type="email" name="email" placeholder="{{ t('home.forgot.email') }}" required><button>{{ t('home.forgot.button') }}</button></div></form></details>
<details><summary>{{ t('home.resend.summary') }}</summary><form method="post" action="/resend">
<div class="row"><input type="email" name="email" placeholder="{{ t('home.forgot.email') }}" required><button>{{ t('home.resend.button') }}</button></div></form></details>"""

RESET = """<h1>{{ t('reset.title') }}</h1>
<form method="post"><fieldset><legend>{{ t('reset.legend') }}</legend>
<div class="row"><input type="password" name="password" placeholder="{{ t('reset.placeholder') }}" minlength="8" required autocomplete="new-password"></div>
<button>{{ t('reset.button') }}</button></fieldset></form>"""

ADMIN = """<h1>{{ t('admin.title') }}</h1>
<p class="muted">{{ t('admin.intro') }}</p>
<form method="post"><fieldset><legend>{{ t('admin.legend') }}</legend>
{% for key, _ in awards %}<div class="row"><label for="{{ key }}" style="flex:0 0 14em">{{ t('award.' + key) }}</label>
<input id="{{ key }}" name="{{ key }}" maxlength="200" value="{{ results.get(key, '') }}"></div>{% endfor %}
<button>{{ t('admin.button') }}</button></fieldset></form>"""

PREDICT = """<h1>{{ t('predict.title') }}</h1>
{% if submitted %}<p><b>{{ t('predict.submitted', date=fmt_dt(submitted_at)) }}</b></p>{% endif %}
{% if start %}<p class="muted">{{ t('predict.season', start=fmt_dt(start), lock=fmt_dt(lock)) }}
{{ t('predict.penalty') }}
{% if not submitted %}{% if locked %}<b>{{ t('predict.closed') }}</b>{% elif late_now %}<b>{{ t('predict.latenow', days=plural(late_now, 'day'), points=plural(late_now, 'point')) }}</b>{% endif %}{% endif %}</p>{% endif %}
{% if not submitted and not locked %}<p class="muted">{{ t('predict.finalhint') }}</p>{% endif %}
<form method="post" onsubmit='return confirm({{ t("predict.confirm")|tojson }})'>
{% for title, p, teams in confs %}
<fieldset><legend>{{ title }}</legend>
{% for i in range(1, 16) %}<div class="row"><b>{{ i }}.</b>
<select name="{{ p }}{{ i }}" required {{ 'disabled' if locked or submitted }}><option value="">{{ t('predict.select') }}</option>
{% for t_ in teams %}<option {{ 'selected' if saved[p][i-1] == t_ }}>{{ t_ }}</option>{% endfor %}</select></div>{% endfor %}
</fieldset>{% endfor %}
<fieldset><legend>{{ t('predict.awards.legend') }}</legend>
<p class="muted">{{ t('predict.awards.hint') }}</p>
{% for key, _ in awards %}<div class="row"><label for="a_{{ key }}" style="flex:0 0 14em">{{ t('award.' + key) }}</label>
<input id="a_{{ key }}" name="a_{{ key }}" maxlength="100" value="{{ saved_awards.get(key, '') }}" required {{ 'disabled' if locked or submitted }}></div>{% endfor %}
</fieldset>
{% if not locked and not submitted %}<button>{{ t('predict.button') }}</button>{% endif %}</form>
<script>
// Disable a team in other dropdowns once it's picked, so each team is used once per conference.
document.querySelectorAll('fieldset').forEach(fs=>{
  const sels=[...fs.querySelectorAll('select')];
  const sync=()=>{const used=sels.map(s=>s.value);
    sels.forEach(s=>[...s.options].forEach(o=>o.disabled=!!o.value&&used.includes(o.value)&&s.value!==o.value))};
  sels.forEach(s=>s.onchange=sync); sync();
});
</script>"""

STANDINGS = """<h1>{{ t('st.title') }}</h1>
{% if updated %}<p class="muted">{{ t('st.updated', date=fmt_dt(updated)) }}</p>
{% else %}<p class="muted">{{ t('st.notloaded') }}</p>{% endif %}
<div class="two">
{% for title, key in [(t('conf.east'), 'E'), (t('conf.west'), 'W')] %}
<div class="card"><h2 style="margin-top:0">{{ title }}</h2><table>
{% for r in tables[key] %}<tr{% if r.pos == 10 %} class="cut"{% endif %}><td class="rank">{{ r.pos }}</td><td>{{ r.team }}</td><td style="text-align:right">{{ r.wins }}-{{ r.losses }}</td></tr>
{% else %}<tr><td class="muted">{{ t('st.nodata') }}</td></tr>{% endfor %}</table></div>{% endfor %}
</div>
<h2>{{ t('st.leaderboard') }}</h2>
<div class="card"><table class="board"><tr><th>#</th><th>{{ t('st.col.player') }}</th><th>{{ t('st.col.score') }}</th><th>{{ t('st.col.awards') }}</th><th>{{ t('st.col.late') }}</th></tr>
{% for n, s, late, aw in board %}<tr><td class="rank">{{ loop.index }}</td><td>{{ n }}</td><td class="score">{{ s }}</td><td class="muted">{% if aw %}+{{ aw }}{% endif %}</td><td class="muted">{% if late %}-{{ late }}{% endif %}</td></tr>{% endfor %}</table></div>
<h2>{{ t('st.awards.title') }}</h2>
{% if results %}<div class="card"><table>{% for key, _ in awards %}<tr><td>{{ t('award.' + key) }}</td><td>{{ results.get(key) or t('st.awards.tba') }}</td></tr>{% endfor %}</table></div>
{% else %}<p class="muted">{{ t('st.awards.empty') }}</p>{% endif %}"""

RULES = """<h1>{{ t('rules.title') }}</h1>
<ul>{% for k in ['rules.top10', 'rules.low', 'rules.awards', 'rules.late', 'rules.final'] %}<li>{{ t(k) }}</li>{% endfor %}</ul>
<p>{{ t('rules.example') }}</p>"""

PICKS = """<h1>{{ t('picks.title') }}</h1>
<p class="muted">{{ t('picks.legend') }} <span class="exact">&nbsp;{{ t('picks.exact') }}&nbsp;</span> <span class="zone">&nbsp;{{ t('picks.zone') }}&nbsp;</span></p>
{% for e in entries %}<details{% if e.email == me %} open class="mine"{% endif %}>
<summary>{{ loop.index }}. {{ e.name }} — {{ plural(e.total, 'point') }}</summary>
<div class="two">{% for title, rows in [(t('conf.east'), e.east), (t('conf.west'), e.west)] %}
<div><h2>{{ title }}</h2><ol class="picks">{% for team, mark in rows %}<li class="{{ mark }}">{{ team }}</li>{% endfor %}</ol></div>{% endfor %}</div>
<table>{% for key, pick, hit in e.awards %}<tr><td>{{ t('award.' + key) }}</td><td{% if hit %} class="exact"{% endif %}>{{ pick }}</td></tr>{% endfor %}</table>
</details>
{% else %}<p class="muted">{{ t('picks.none') }}</p>{% endfor %}"""

def page(tpl, **ctx):
    body = render_template_string(tpl, contact=CONTACT_EMAIL, **ctx)
    return render_template_string(LAYOUT, body=body, name=session.get("name"), admin=is_admin(), path=request.path)

def login_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not session.get("email"):
            flash(t("msg.login_required"))
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
        flash(t("msg.register.invalid"))
        return redirect("/")
    try:
        db().execute("INSERT INTO users(email, name, password_hash, verified) VALUES(?,?,?,0)",
                     (email, name, generate_password_hash(pw)))
        db().commit()
    except pyodbc.IntegrityError:                   # email is the primary key
        flash(t("msg.register.exists"))
        return redirect("/")
    may_send(email)                                 # starts the 60 s throttle for this address
    if send_verification(email):
        flash(t("msg.register.sent", email=email))
    else:
        flash(t("msg.register.mailfail"))
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
        flash(t("msg.login.locked", minutes=plural(row.lock_min, "minute")))
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
            flash(t("msg.login.locknow", minutes=plural(LOCKOUT_MINUTES, "minute")))
        else:
            flash(t("msg.login.wrong"))
        return redirect("/")
    if row.failed_logins:                           # correct password: the streak of failures is over
        db().execute("UPDATE users SET failed_logins=0, locked_until=NULL WHERE email=?", (email,))
        db().commit()
    if not row.verified:
        flash(t("msg.login.unverified"))
        return redirect("/")
    start_session(email, row.name)
    return landing()

@app.get("/verify/<token>")
def verify(token):
    try:
        email = ser_verify.loads(token, max_age=86400)
    except BadSignature:                            # also covers expired tokens
        flash(t("msg.verify.invalid"))
        return redirect("/")
    row = db().execute("SELECT name FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        flash(t("msg.verify.noaccount"))
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
    flash(t("msg.resend.done"))   # same answer either way
    return redirect("/")

@app.post("/forgot")
def forgot():
    email = request.form["email"].strip().lower()
    row = db().execute("SELECT password_hash FROM users WHERE email=?", (email,)).fetchone()
    if row and may_send(email):
        send_reset(email, row.password_hash)
    flash(t("msg.forgot.done"))               # same answer either way
    return redirect("/")

@app.route("/reset/<token>", methods=["GET", "POST"])
def reset(token):
    bad = t("msg.reset.invalid")
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
            flash(t("msg.reset.length"))
            return redirect(request.path)
        db().execute("UPDATE users SET password_hash=?, verified=1, failed_logins=0, locked_until=NULL WHERE email=?", (generate_password_hash(pw), email))
        db().commit()                               # resetting via the inbox also proves the email address
        flash(t("msg.reset.done"))
        return redirect("/")
    return page(RESET)

@app.route("/predict", methods=["GET", "POST"])
@login_required
def predict():
    email = session["email"]
    row = db().execute("SELECT east, west, updated FROM predictions WHERE email=?", (email,)).fetchone()
    if request.method == "POST":
        if row:
            flash(t("msg.predict.already"))
            return redirect("/predict")
        if is_locked():
            flash(t("msg.predict.closed"))
            return redirect("/predict")
        east = [request.form.get(f"e{i}") for i in range(1, 16)]
        west = [request.form.get(f"w{i}") for i in range(1, 16)]
        if sorted(east) != sorted(EAST) or sorted(west) != sorted(WEST):
            flash(t("msg.predict.teams"))
            return redirect("/predict")
        awards = {k: " ".join(request.form.get("a_" + k, "").split()) for k, _ in AWARDS}   # trim, collapse spaces
        if any(not v or len(v) > 100 for v in awards.values()):
            flash(t("msg.predict.awards"))
            return redirect("/predict")
        d = db()
        try:                                        # INSERT only; the primary key (email) guarantees one submission
            d.execute("DELETE FROM award_picks WHERE email=?", (email,))     # leftovers of a deleted account, if any
            d.execute("INSERT INTO predictions(email, east, west) VALUES(?,?,?)",
                      (email, json.dumps(east), json.dumps(west)))
            for k, v in awards.items():
                d.execute("INSERT INTO award_picks(email, award, pick) VALUES(?,?,?)", (email, k, v))
            d.commit()                              # picks and awards are saved together or not at all
        except pyodbc.IntegrityError:               # double click or second tab: the first submit wins
            d.rollback()
            flash(t("msg.predict.already"))
            return redirect("/predict")
        late = days_late(datetime.now(timezone.utc))
        flash(t("msg.predict.ok") + (" " + t("msg.predict.ok.late", days=plural(late, "day"), points=plural(late, "point")) if late else ""))
        return redirect("/standings")
    saved = {"e": json.loads(row.east), "w": json.loads(row.west)} if row else {"e": [""] * 15, "w": [""] * 15}
    saved_awards = {r.award: r.pick for r in db().execute("SELECT award, pick FROM award_picks WHERE email=?", (email,)).fetchall()} if row else {}
    return page(PREDICT, saved=saved, saved_awards=saved_awards, awards=AWARDS, confs=[(t("conf.east"), "e", EAST), (t("conf.west"), "w", WEST)],
                start=season_start(), lock=lock_time(), locked=is_locked(), late_now=days_late(datetime.now(timezone.utc)),
                submitted=row is not None, submitted_at=row.updated if row else None)

@app.get("/standings")
@login_required
def standings():
    if not has_prediction(session["email"]):
        if not is_locked():
            flash(t("msg.st.fillfirst"))
            return redirect("/predict")
        flash(t("msg.st.nopicks"))
    rows = db().execute("SELECT conf, pos, team, wins, losses, updated FROM standings ORDER BY conf, pos").fetchall()
    tables = {"E": [r for r in rows if r.conf == "E"], "W": [r for r in rows if r.conf == "W"]}
    actual = {k: [r.team for r in v] for k, v in tables.items()}
    return page(STANDINGS, tables=tables, updated=rows[0].updated if rows else None, board=leaderboard(actual),
                results=award_results(), awards=AWARDS)

@app.get("/favicon.svg")
def favicon():
    return LOGO_SVG, 200, {"Content-Type": "image/svg+xml", "Cache-Control": "public, max-age=86400"}

@app.get("/favicon.ico")
def favicon_ico():
    return redirect("/favicon.svg", 301)

@app.get("/rules")
def rules():
    return page(RULES)

@app.get("/picks")
@login_required
def picks():
    if not has_prediction(session["email"]) and not is_locked():     # no peeking before you've committed to your own picks
        flash(t("msg.picks.submitfirst"))
        return redirect("/predict")
    rows = db().execute("SELECT conf, pos, team FROM standings ORDER BY conf, pos").fetchall()
    actual = {k: [r.team for r in rows if r.conf == k] for k in ("E", "W")}
    return page(PICKS, entries=entries(actual), me=session["email"])

@app.route("/admin", methods=["GET", "POST"])
@login_required
def admin():
    if not is_admin():
        return "Not found", 404                     # don't reveal that the page exists
    if request.method == "POST":
        d = db()
        for key, _ in AWARDS:
            val = " ".join(request.form.get(key, "").split())[:200]
            if not val:
                d.execute("DELETE FROM award_results WHERE award=?", (key,))
            elif d.execute("UPDATE award_results SET winner=?, updated=SYSUTCDATETIME() WHERE award=?", (val, key)).rowcount == 0:
                d.execute("INSERT INTO award_results(award, winner) VALUES(?,?)", (key, val))
        d.commit()
        flash(t("msg.admin.ok"))
        return redirect("/admin")
    return page(ADMIN, results=award_results(), awards=AWARDS)

@app.get("/logout")
def logout():
    session.clear()
    return redirect("/")

if __name__ == "__main__":
    app.run(debug=True)