import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
from pathlib import Path

import requests
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

from avatars import AVATARS as FREE_AVATARS, BUILTIN, PREMIUM_AVATARS, default_avatar
from data import CURATED, load_scraped, _key
from poster import lookup as poster_lookup, lookup_batch as poster_batch
from scrape import get_page

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("guidetoanime")

BASE = Path(__file__).parent
REFRESH_EVERY = 6 * 3600  # auto-update the list every 6 hours (when someone visits)
ADMIN_USER = os.environ.get("OWNER_USERNAME", "khush-meet").strip().lower()
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "").strip()   # shown on the Contact page
POLICY_DATE = "October 22, 2026"
PREMIUM_COST = int(os.environ.get("PREMIUM_COST_TOKENS", "100"))   # tokens needed for one Premium period
PREMIUM_DAYS = int(os.environ.get("PREMIUM_DAYS", "30"))           # length of one Premium period
PAYMENTS_ENABLED = False   # switch on only after a payment gateway (e.g. Razorpay) is connected
ADS_ENABLED = False        # switch on only after a rewarded-ads network with server-side verification is connected

app = Flask(__name__)
_secret = os.environ.get("SECRET_KEY")
if not _secret:
    _secret = secrets.token_hex(32)
    log.warning("SECRET_KEY is not set: using a temporary random key (everyone is logged out on each restart). "
                "Set SECRET_KEY on your host.")
app.config["SECRET_KEY"] = _secret
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE") == "1"   # set COOKIE_SECURE=1 once the site is on https
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 90
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024  # biggest upload allowed: 4 MB

db_url = os.environ.get("DATABASE_URL", "sqlite:///" + str(BASE / "anitrack.db"))
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
db = SQLAlchemy(app)


# ---------------------------------------------------------------- database
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(30), unique=True, nullable=False)
    pw = db.Column(db.String(255), nullable=False)
    premium = db.Column(db.Boolean, default=False)       # can change avatar
    avatar = db.Column(db.String(30), default="")        # chosen character key (premium only)
    tokens = db.Column(db.Integer, default=0)            # token balance
    premium_until = db.Column(db.Integer, default=0)     # unix time when bought Premium ends (0 = none)
    created = db.Column(db.Integer, default=lambda: int(time.time()))   # when the account was made (0 = before this was recorded)
    last_login = db.Column(db.Integer, default=0)


class TokenLog(db.Model):
    """Every change of a token balance (audit trail)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, index=True, nullable=False)
    delta = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(100), default="")
    ts = db.Column(db.Integer, default=lambda: int(time.time()))


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    uid = db.Column(db.String(300), unique=True, index=True, nullable=False)
    title = db.Column(db.String(500), nullable=False)
    hindi = db.Column(db.Boolean, default=False)
    movie = db.Column(db.Boolean, default=False)
    image = db.Column(db.String(500), default="")
    year = db.Column(db.Integer)
    eps = db.Column(db.Integer)
    mins = db.Column(db.Integer)
    genres = db.Column(db.String(200), default="")
    curated = db.Column(db.Boolean, default=False)   # written by you (data.py or added on the site)
    listed = db.Column(db.Boolean, default=True)     # currently on the website
    looked_up = db.Column(db.Boolean, default=False) # poster/details already fetched
    img_try = db.Column(db.Integer, default=0)       # last time a poster lookup was tried
    added = db.Column(db.Integer, default=lambda: int(time.time()))


class Extra(db.Model):
    """Priority, note and uploaded poster for an anime."""
    uid = db.Column(db.String(300), primary_key=True)
    tier = db.Column(db.String(20), default="optional")
    note = db.Column(db.String(300), default="")
    mime = db.Column(db.String(50), default="")
    data = db.Column(db.LargeBinary)


class Watched(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True, nullable=False)
    uid = db.Column(db.String(300), nullable=False)
    __table_args__ = (db.UniqueConstraint("user_id", "uid"),)


class CustomAvatar(db.Model):
    """Avatar pictures the admin uploads; only Premium users and the admin can pick them."""
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(30), unique=True, nullable=False)
    name = db.Column(db.String(60), nullable=False)
    mime = db.Column(db.String(50), default="")
    data = db.Column(db.LargeBinary)
    owner_id = db.Column(db.Integer, index=True)        # who added it
    shared = db.Column(db.Boolean, default=False)       # True = added by admin, everyone sees it
    ver = db.Column(db.Integer, default=1)              # bumped when the picture is edited


class AvatarOverride(db.Model):
    """Admin's edits to a built-in character: a new name and/or a new picture."""
    key = db.Column(db.String(30), primary_key=True)
    name = db.Column(db.String(60), default="")
    has_pic = db.Column(db.Boolean, default=False)
    mime = db.Column(db.String(50), default="")
    data = db.Column(db.LargeBinary)
    ver = db.Column(db.Integer, default=1)
    hidden = db.Column(db.Boolean, default=False)       # admin "deleted" this character


class Meta(db.Model):
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(100))


def get_meta(key, default=""):
    m = db.session.get(Meta, key)
    return m.value if m else default


def set_meta(key, value):
    m = db.session.get(Meta, key)
    if m:
        m.value = str(value)
    else:
        db.session.add(Meta(key=key, value=str(value)))
    db.session.commit()


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def seed():
    """First run only: fill the database from data.py + the JSON files you already have."""
    db.create_all()
    if Item.query.first():
        return
    seen = set()
    for c in CURATED:
        uid = _key(c["title"])
        seen.add(uid)
        db.session.add(Item(uid=uid, title=c["title"], hindi=c["hindi"],
                            movie=(c["kind"] == "Movie"), curated=True))
    for s in load_scraped():  # rows from scraped_title(s).json via data.py
        uid = _key(s.get("title", ""))
        if not uid or uid in seen:
            continue
        seen.add(uid)
        db.session.add(Item(uid=uid, title=s["title"], hindi=bool(s.get("hindi")),
                            movie=(s.get("kind") == "Movie")))
    db.session.commit()


def sync_scraped():
    """Every start: add any title from scraped_title.json that is not in the
    database yet, so the catalogue grows even when the database already has data."""
    existing = {i.uid for i in Item.query.with_entities(Item.uid).all()}
    added = False
    for s in load_scraped():
        title = (s.get("title") or "").strip()
        uid = _key(title)
        if not uid or uid in existing:
            continue
        existing.add(uid)
        db.session.add(Item(uid=uid, title=title, hindi=bool(s.get("hindi")),
                            movie=(s.get("kind") == "Movie"), listed=True))
        added = True
    if added:
        db.session.commit()


def migrate_curated():
    """One time: copy the details written in data.py into the database,
    so that every anime (even those) can be edited from the website."""
    if get_meta("curated_migrated") == "1":
        return
    items = {i.uid: i for i in Item.query.all()}
    for c in CURATED:
        uid = _key(c["title"])
        it = items.get(uid)
        if not it:
            it = Item(uid=uid, title=c["title"])
            db.session.add(it)
            items[uid] = it
        it.title = c["title"]
        it.curated = True
        it.hindi = c["hindi"]
        it.movie = c["kind"] == "Movie"
        it.year, it.eps, it.mins = c["year"], c["eps"], c["mins"]
        it.genres = ",".join(c["genres"])
        if not db.session.get(Extra, uid):
            db.session.add(Extra(uid=uid, tier=c["tier"], note=c.get("note", "")))
    db.session.commit()
    set_meta("curated_migrated", "1")


def load_local_images():
    """Every start: use posters from images.json for anime that have none yet."""
    images = read_json(BASE / "images.json", {})
    changed = False
    for it in Item.query.filter((Item.image == "") | (Item.image.is_(None))).all():
        url = images.get(it.uid)
        if url:
            it.image = url
            changed = True
    if changed:
        db.session.commit()


def ensure_columns():
    """Old databases were made before 'premium' and 'avatar' existed: add them."""
    from sqlalchemy import inspect, text
    cols = {c["name"] for c in inspect(db.engine).get_columns("user")}
    false = "0" if db.engine.dialect.name == "sqlite" else "FALSE"
    if "premium" not in cols:
        db.session.execute(text(f'ALTER TABLE "user" ADD COLUMN premium BOOLEAN DEFAULT {false}'))
    if "avatar" not in cols:
        db.session.execute(text("ALTER TABLE \"user\" ADD COLUMN avatar VARCHAR(30) DEFAULT ''"))
    if "tokens" not in cols:
        db.session.execute(text('ALTER TABLE "user" ADD COLUMN tokens INTEGER DEFAULT 0'))
    if "premium_until" not in cols:
        db.session.execute(text('ALTER TABLE "user" ADD COLUMN premium_until INTEGER DEFAULT 0'))
    if "created" not in cols:
        db.session.execute(text('ALTER TABLE "user" ADD COLUMN created INTEGER DEFAULT 0'))
    if "last_login" not in cols:
        db.session.execute(text('ALTER TABLE "user" ADD COLUMN last_login INTEGER DEFAULT 0'))
    ccols = {c["name"] for c in inspect(db.engine).get_columns("custom_avatar")}
    if "owner_id" not in ccols:
        db.session.execute(text("ALTER TABLE custom_avatar ADD COLUMN owner_id INTEGER"))
    if "shared" not in ccols:
        db.session.execute(text(f"ALTER TABLE custom_avatar ADD COLUMN shared BOOLEAN DEFAULT {false}"))
        db.session.execute(text("UPDATE custom_avatar SET shared = " + ("1" if false == "0" else "TRUE")))  # older uploads were admin's
    if "ver" not in ccols:
        db.session.execute(text("ALTER TABLE custom_avatar ADD COLUMN ver INTEGER DEFAULT 1"))
    icols = {c["name"] for c in inspect(db.engine).get_columns("item")}
    if "img_try" not in icols:
        db.session.execute(text("ALTER TABLE item ADD COLUMN img_try INTEGER DEFAULT 0"))
    ocols = {c["name"] for c in inspect(db.engine).get_columns("avatar_override")}
    if "hidden" not in ocols:
        db.session.execute(text(f"ALTER TABLE avatar_override ADD COLUMN hidden BOOLEAN DEFAULT {false}"))
    db.session.commit()


def ensure_admin():
    """Create the admin account on startup, and keep its password in sync with ADMIN_PASSWORD
    (set it on your hosting site). With no ADMIN_PASSWORD, a new admin gets a random password
    that is printed once in the log."""
    env_pw = os.environ.get("ADMIN_PASSWORD")
    u = User.query.filter_by(username=ADMIN_USER).first()
    if not u:
        pw = env_pw or secrets.token_urlsafe(12)
        db.session.add(User(username=ADMIN_USER, pw=generate_password_hash(pw)))
        db.session.commit()
        if not env_pw:
            log.warning("Admin account '%s' created with the RANDOM password: %s  (set ADMIN_PASSWORD to choose your own)",
                        ADMIN_USER, pw)
        return
    if env_pw and not check_password_hash(u.pw, env_pw):
        u.pw = generate_password_hash(env_pw)
        db.session.commit()
        log.info("Admin password updated from ADMIN_PASSWORD.")
    elif not env_pw and check_password_hash(u.pw, "Khush-meet"):
        log.warning("SECURITY: the admin password is still the old default. Set ADMIN_PASSWORD to a long, private password.")


# ------------------------------------------------- auto-update from the site
# Everything below runs on the SERVER. Only the admin can start it by hand or
# switch the automatic updates on/off (see the /api/admin/... routes further down).
_lock = threading.Lock()
RETRY_IMAGE_AFTER = 3 * 86400      # an anime still without a poster is retried every 3 days
status = dict(running=False, task="", msg="Idle", done=0, total=0, finished=0)


def update_titles():
    """Read animedubhindi.link, add new titles, hide titles that were removed."""
    status.update(msg="Reading animedubhindi.link ...", done=0, total=0)
    found = {}
    for n in range(1, 56):
        try:
            page = get_page(n)
        except Exception as e:
            log.warning("refresh: page %s stopped: %s", n, e)
            break
        if not page:
            break
        for p in page:
            found.setdefault(_key(p["title"]), p)
        status.update(msg=f"Reading animedubhindi.link ... page {n} ({len(found)} titles)")
        time.sleep(1)
    if not found:
        return "Titles: could not read the website."

    existing = {i.uid: i for i in Item.query.all()}
    new = 0
    for uid, p in found.items():
        if not uid:
            continue
        it = existing.get(uid)
        if it:
            it.listed = True
        else:
            db.session.add(Item(uid=uid, title=p["title"], hindi=p["hindi"], movie=p["movie"]))
            new += 1
    hidden = 0
    scraped_listed = [i for i in existing.values() if not i.curated and i.listed]
    if len(found) >= max(30, len(scraped_listed) // 2):  # only if the scrape looks complete
        for i in scraped_listed:
            if i.uid not in found:
                i.listed = False
                hidden += 1
    db.session.commit()
    return f"Titles: {new} new, {hidden} removed."


def update_images(deadline=None, force=False):
    """Fetch posters (and fill blank year/episodes/genres) from AniList, MyAnimeList and Kitsu.
    Never replaces a poster or detail that is already there, so the admin's edits are safe.
    force=True also retries titles that failed recently."""
    now = int(time.time())
    no_image = db.or_(Item.image == "", Item.image.is_(None))
    q = Item.query.filter(db.or_(Item.listed.is_(True), Item.curated.is_(True)), no_image)
    if not force:
        q = q.filter(db.or_(Item.img_try.is_(None), Item.img_try < now - RETRY_IMAGE_AFTER))
    todo = q.order_by(Item.id).all()
    if not force:   # the admin's manual buttons fetch missing posters only; the slow detail top-up runs in the automatic update
        todo += Item.query.filter(Item.listed.is_(True), Item.looked_up.is_(False), ~no_image).order_by(Item.id).all()

    status.update(done=0, total=len(todo), msg=f"Fetching posters for {len(todo)} titles ...")
    found = missed = net_fail = 0
    pre = {}                                             # fast pass: 8 titles per AniList request
    for i in range(0, len(todo), 8):
        pre.update(poster_batch([it.title for it in todo[i:i + 8]]))
        status.update(msg=f"Quick pass: {min(i + 8, len(todo))}/{len(todo)} checked ({len(pre)} matched) ...")

    def pick(title):                                     # quick-pass result, or the normal careful search
        return pre.get(title) or poster_lookup(title)

    with ThreadPoolExecutor(max_workers=4) as pool:      # 4 titles at a time (network only; the database is used here)
        futs = {pool.submit(pick, it.title): it for it in todo}

        def stop(msg):
            for f in futs:
                f.cancel()
            return msg

        for n, fut in enumerate(as_completed(futs), 1):
            it = futs[fut]
            try:
                m = fut.result()
            except Exception as e:
                log.warning("poster lookup error: %s", e)
                m = None
            if m is None:                  # network trouble
                net_fail += 1
                if net_fail >= 5:
                    return stop(f"Posters: {found} found. Stopped: cannot reach the image sites.")
                continue
            net_fail = 0
            had_image = bool(it.image)
            it.img_try = int(time.time())
            if m:
                it.image = it.image or m["image"]
                it.year = it.year or m.get("year")
                it.eps = it.eps or m.get("eps")
                it.mins = it.mins or m.get("mins")
                it.genres = it.genres or ",".join(m.get("genres") or [])
            if not had_image:
                found, missed = (found + 1, missed) if m else (found, missed + 1)
            it.looked_up = True
            db.session.commit()
            status.update(done=n, msg=f"Posters: {found} found, {missed} not found ({n}/{len(todo)})")
            log.info("[poster %s/%s] %s %s", n, len(todo), "OK  " if m else "MISS", it.title)
            if deadline and time.time() > deadline:
                return stop(f"Posters: {found} found, {missed} not found (time limit, will continue next time).")
    return f"Posters: {found} found, {missed} not found."


def run_task(what="all", quick=False, force=False):
    """Start an update in the background. Returns False if one is already running."""
    if not _lock.acquire(blocking=False):
        return False
    status.update(running=True, task=what, msg="Starting ...", done=0, total=0)

    def work():
        parts = []
        try:
            with app.app_context():
                set_meta("last_refresh", int(time.time()))  # set first so a failure doesn't retry on every visit
                if what in ("all", "titles"):
                    parts.append(update_titles())
                if what in ("all", "images"):
                    parts.append(update_images(deadline=time.time() + 150 if quick else None, force=force))
            status["msg"] = " ".join(parts) or "Done."
        except Exception as e:
            log.exception("update failed: %s", e)
            status["msg"] = f"Failed: {e}"
        finally:
            status.update(running=False, finished=int(time.time()))
            _lock.release()

    threading.Thread(target=work, daemon=True).start()
    return True


def maybe_refresh():
    """Automatic update. Visitors never see or control it; the admin can switch it off."""
    if get_meta("auto_update", "1") != "1":
        return
    last = float(get_meta("last_refresh", "0") or 0)
    if time.time() - last > REFRESH_EVERY and not _lock.locked():
        run_task("all", quick=True)


# ------------------------------------------------------------------ titles
def build_titles():
    rows = Item.query.filter(db.or_(Item.curated, Item.listed)).order_by(Item.id).all()
    extras = {e.uid: e for e in db.session.query(Extra.uid, Extra.tier, Extra.note)}
    out = []
    for it in rows:
        ex = extras.get(it.uid)
        out.append(dict(
            uid=it.uid,
            title=it.title,
            kind="Movie" if it.movie else "Series",
            year=it.year, eps=it.eps, mins=it.mins,
            genres=[g for g in (it.genres or "").split(",") if g] or ["Unsorted"],
            hindi=bool(it.hindi),
            tier=(ex.tier if ex and ex.tier else "optional"),
            note=(ex.note if ex and ex.note else ""),
            image=it.image or "",
            added=it.added or 0,
            custom=bool(it.curated),  # can be deleted
        ))
    return out


# -------------------------------------------------------------------- login
_fails = {}  # ip -> (count, first_time): simple brute-force guard


def login_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify(error="login"), 401
            return redirect(url_for("login"))
        return f(*a, **k)
    return wrapper


def is_owner():
    """Only the admin account can add / edit / delete anime."""
    return "user_id" in session and session.get("username") == ADMIN_USER


def all_avatars(uid=None):
    """Built-in characters + uploaded ones this user may see:
    everything the admin added (shared) plus the user's own private uploads."""
    uid = uid if uid is not None else session.get("user_id")
    out = [dict(a, premium=False) for a in FREE_AVATARS] + [dict(a, premium=True) for a in PREMIUM_AVATARS]
    ov = {o.key: o for o in AvatarOverride.query.with_entities(
        AvatarOverride.key, AvatarOverride.name, AvatarOverride.has_pic, AvatarOverride.ver, AvatarOverride.hidden)}
    admin = is_owner()
    keep = []
    for a in out:
        o = ov.get(a["key"])
        if o and o.hidden:
            if not admin:
                continue                       # deleted: only the admin still sees it (to restore it)
            a["hidden"] = True
        keep.append(a)
        if o:
            a["edited"] = True
            if o.name:
                a["name"] = o.name
            if o.has_pic:
                a["pic"] = f"/avatar-img/{a['key']}?v={o.ver or 1}"
    out = keep
    q = CustomAvatar.query.filter((CustomAvatar.shared == True) | (CustomAvatar.owner_id == uid)).order_by(CustomAvatar.id)  # noqa: E712
    for c in q:
        out.append(dict(key=c.key, name=c.name, premium=True, shared=bool(c.shared),
                        mine=(c.owner_id == uid), src=f"/avatar-img/{c.key}?v={c.ver or 1}"))
    return out


def avatar_keys():
    return {a["key"] for a in all_avatars() if not a.get("hidden")}


def default_for(username):
    pool = [a["key"] for a in all_avatars() if not a["premium"] and not a.get("hidden")]
    return default_avatar(username, pool)


def current_user():
    return db.session.get(User, session.get("user_id"))


def premium_active(u):
    """Premium = given by the admin (no expiry) or bought with tokens (until premium_until)."""
    return bool(u.premium or (u.premium_until or 0) > time.time())


def is_premium():
    """Premium accounts (and the admin) can change their avatar."""
    u = current_user()
    return bool(u and (premium_active(u) or is_owner()))


def credit_tokens(user_id, amount, reason):
    """Add (or remove, if negative) tokens and write the audit line. The payment / ad code will call this."""
    User.query.filter_by(id=user_id).update({User.tokens: db.func.coalesce(User.tokens, 0) + amount},
                                            synchronize_session=False)
    db.session.add(TokenLog(user_id=user_id, delta=amount, reason=reason[:100]))
    db.session.commit()


def owner_required(f):
    @wraps(f)
    @login_required
    def wrapper(*a, **k):
        if not is_owner():
            return jsonify(error="Only the admin can do this."), 403
        return f(*a, **k)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        count, first = _fails.get(ip, (0, time.time()))
        if time.time() - first > 600:
            count, first = 0, time.time()
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        action = request.form.get("action", "login")
        if count >= 8:
            error = "Too many attempts. Try again in 10 minutes."
        elif action == "register":
            if not re.fullmatch(r"[a-z0-9_-]{3,30}", username):
                error = "Username: 3 to 30 letters, numbers, - or _ ."
            elif len(password) < 6:
                error = "Password must be at least 6 characters."
            elif User.query.filter_by(username=username).first():
                error = "That username is taken."
            else:
                u = User(username=username, pw=generate_password_hash(password), last_login=int(time.time()))
                db.session.add(u)
                db.session.commit()
                session.permanent = True
                session["user_id"], session["username"] = u.id, u.username
                return redirect(url_for("index"))
        else:
            u = User.query.filter_by(username=username).first()
            if u and check_password_hash(u.pw, password):
                u.last_login = int(time.time())
                db.session.commit()
                session.permanent = True
                session["user_id"], session["username"] = u.id, u.username
                _fails.pop(ip, None)
                return redirect(url_for("index"))
            _fails[ip] = (count + 1, first)
            error = "Wrong username or password."
    return render_template("login.html", error=error, avatars=[a for a in all_avatars(0) if not a["premium"]])


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# --------------------------------------------------------------------- pages
@app.route("/")
def index():
    maybe_refresh()
    u = current_user() if "user_id" in session else None
    if not u:                                   # visitors see the public landing page
        session.clear()
        titles = [{k: t[k] for k in ("uid", "title", "kind", "year", "genres", "hindi", "image", "added", "tier")}
                  for t in build_titles()]
        chars = [a for a in all_avatars(0) if not a["premium"]]
        return render_template("landing.html", titles=titles, avatars=chars)
    titles = build_titles()
    genres = sorted({g for x in titles for g in x["genres"]})
    watched = [w.uid for w in Watched.query.filter_by(user_id=u.id)]
    premium = is_premium()
    avatar = u.avatar if (premium and u.avatar in avatar_keys()) else default_for(u.username)
    return render_template("index.html", titles=titles, genres=genres,
                           watched=watched, username=session.get("username", ""),
                           is_owner=is_owner(), premium=premium, tokens=(u.tokens or 0),
                           avatar_key=avatar, default_key=default_for(u.username), avatars=all_avatars())


@app.route("/api/watch", methods=["POST"])
@login_required
def api_watch():
    data = request.get_json(silent=True) or {}
    uid, on = data.get("uid", ""), bool(data.get("on"))
    if not Item.query.filter_by(uid=uid).first():
        return jsonify(error="unknown title"), 400
    row = Watched.query.filter_by(user_id=session["user_id"], uid=uid).first()
    if on and not row:
        db.session.add(Watched(user_id=session["user_id"], uid=uid))
    elif not on and row:
        db.session.delete(row)
    db.session.commit()
    return jsonify(ok=True)


@app.route("/api/avatar", methods=["POST"])
@login_required
def api_avatar():
    if not is_premium():
        return jsonify(error="Changing avatar is a Premium feature."), 403
    key = (request.get_json(silent=True) or {}).get("key", "")
    u = current_user()
    if key == "":                                   # "use my default avatar"
        u.avatar = ""
        db.session.commit()
        return jsonify(ok=True, key=default_for(u.username))
    if key not in avatar_keys():
        return jsonify(error="Unknown character."), 400
    u.avatar = key
    db.session.commit()
    return jsonify(ok=True, key=key)


MAX_PRIVATE_AVATARS = 10
BUILTIN_KEYS = {a["key"] for a in BUILTIN}


def _avatar_file():
    file = request.files.get("image")
    if not file:
        return None, None
    data = file.read()
    mime = sniff_image(data)
    if not mime or len(data) > 1024 * 1024:
        return None, "Use a JPG, PNG, WEBP or GIF under 1 MB."
    return (data, mime), None


@app.route("/api/avatar/add", methods=["POST"])
@login_required
def api_avatar_add():
    """Admin: adds a character everyone can see. Premium user: adds one only they can see."""
    if not is_premium():
        return jsonify(error="Adding avatars is a Premium feature."), 403
    name = request.form.get("name", "").strip()[:60]
    pic, err = _avatar_file()
    if err or not name or not pic:
        return jsonify(error=err or "Give a name and a picture."), 400
    uid, admin = session["user_id"], is_owner()
    if not admin and CustomAvatar.query.filter_by(owner_id=uid).count() >= MAX_PRIVATE_AVATARS:
        return jsonify(error=f"You can add up to {MAX_PRIVATE_AVATARS} avatars."), 400
    taken = {a.key for a in CustomAvatar.query.with_entities(CustomAvatar.key)} | {a["key"] for a in BUILTIN}
    base = re.sub(r"[^a-z0-9]+", "", name.lower())[:20] or "custom"
    key, n = "x_" + base, 1
    while key in taken:
        n += 1
        key = f"x_{base}{n}"
    c = CustomAvatar(key=key, name=name, mime=pic[1], data=pic[0], owner_id=uid, shared=admin)
    db.session.add(c)
    db.session.commit()
    return jsonify(ok=True, key=key, name=name, shared=admin, src=f"/avatar-img/{key}?v=1")


def _visible_custom(key):
    c = CustomAvatar.query.filter_by(key=key).first()
    if c and (c.shared or c.owner_id == session.get("user_id")):
        return c
    return None


@app.route("/api/avatar/<key>/edit", methods=["POST"])
@owner_required
def api_avatar_edit(key):
    """Admin only: rename a character and/or replace its picture (built-in or uploaded)."""
    if key in BUILTIN_KEYS:
        name = request.form.get("name", "").strip()[:60]
        pic, err = _avatar_file()
        if err:
            return jsonify(error=err), 400
        o = db.session.get(AvatarOverride, key) or AvatarOverride(key=key, ver=0)
        if name:
            o.name = name
        if pic:
            o.data, o.mime, o.has_pic = pic[0], pic[1], True
            o.ver = (o.ver or 0) + 1
        db.session.add(o)
        db.session.commit()
        base = next(a["name"] for a in BUILTIN if a["key"] == key)
        return jsonify(ok=True, name=o.name or base, pic=(f"/avatar-img/{key}?v={o.ver}" if o.has_pic else ""))
    c = _visible_custom(key)
    if not c:
        return jsonify(error="Not found."), 404
    name = request.form.get("name", "").strip()[:60]
    pic, err = _avatar_file()
    if err:
        return jsonify(error=err), 400
    if name:
        c.name = name
    if pic:
        c.data, c.mime = pic
        c.ver = (c.ver or 1) + 1
    db.session.commit()
    return jsonify(ok=True, name=c.name, src=f"/avatar-img/{c.key}?v={c.ver or 1}")


@app.route("/api/avatar/<key>/reset", methods=["POST"])
@owner_required
def api_avatar_reset(key):
    """Admin only: undo edits to a built-in character."""
    o = db.session.get(AvatarOverride, key)
    if o:
        db.session.delete(o)
        db.session.commit()
    base = next((a for a in BUILTIN if a["key"] == key), None)
    if not base:
        return jsonify(error="Not found."), 404
    return jsonify(ok=True, name=base["name"])


@app.route("/api/avatar/<key>/hide", methods=["POST"])
@owner_required
def api_avatar_hide(key):
    """Admin only: delete (hide) or restore a built-in character."""
    if key not in BUILTIN_KEYS:
        return jsonify(error="Not found."), 404
    on = bool((request.get_json(silent=True) or {}).get("hidden", True))
    o = db.session.get(AvatarOverride, key) or AvatarOverride(key=key, ver=0)
    o.hidden = on
    db.session.add(o)
    if on:
        User.query.filter_by(avatar=key).update({"avatar": ""})
    db.session.commit()
    return jsonify(ok=True, hidden=on)


def delete_user_data(uid):
    """Remove a user and everything that belongs only to them."""
    TokenLog.query.filter_by(user_id=uid).delete()
    Watched.query.filter_by(user_id=uid).delete()
    CustomAvatar.query.filter_by(owner_id=uid, shared=False).delete()
    User.query.filter_by(id=uid).delete()
    db.session.commit()


@app.route("/api/account/delete", methods=["POST"])
@login_required
def api_account_delete():
    data = request.get_json(silent=True) or {}
    if str(data.get("confirm", "")).strip().lower() != "i agree":
        return jsonify(error='Type "I agree" to confirm.'), 400
    if is_owner():
        return jsonify(error="The admin account can't be deleted here."), 403
    if not check_password_hash(current_user().pw, str(data.get("password", ""))):
        return jsonify(error="Wrong password."), 403
    delete_user_data(session["user_id"])
    session.clear()
    return jsonify(ok=True)


@app.route("/api/avatar/<key>/delete", methods=["POST"])
@login_required
def api_avatar_delete(key):
    c = _visible_custom(key)
    if not c or not (is_owner() or c.owner_id == session["user_id"]):
        return jsonify(error="Not found."), 404
    User.query.filter_by(avatar=key).update({"avatar": ""})
    db.session.delete(c)
    db.session.commit()
    return jsonify(ok=True)


@app.route("/avatar-img/<key>")
def avatar_img(key):
    if key in BUILTIN_KEYS:                                   # admin's replacement picture for a built-in
        o = db.session.get(AvatarOverride, key)
        if not o or not o.has_pic or not o.data:
            return Response(status=404)
        return Response(o.data, mimetype=o.mime or "image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})
    c = _visible_custom(key)
    if not c or not c.data:
        return Response(status=404)
    return Response(c.data, mimetype=c.mime or "image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


@app.route("/api/premium", methods=["POST"])
@owner_required
def api_premium():
    data = request.get_json(silent=True) or {}
    u = User.query.filter_by(username=str(data.get("username", "")).strip().lower()).first()
    if not u:
        return jsonify(error="No such user."), 404
    u.premium = bool(data.get("on"))
    db.session.commit()
    return jsonify(ok=True, username=u.username, premium=u.premium)


# ------------------------------------------- site updates (admin ONLY, not premium)
@app.route("/api/admin/update", methods=["POST"])
@owner_required
def api_admin_update():
    what = (request.get_json(silent=True) or {}).get("what", "all")
    if what not in ("all", "titles", "images"):
        return jsonify(error="Unknown update type."), 400
    if not run_task(what, force=(what != "titles")):
        return jsonify(error="An update is already running."), 409
    return jsonify(ok=True)


@app.route("/api/admin/status")
@owner_required
def api_admin_status():
    no_image = db.or_(Item.image == "", Item.image.is_(None))
    shown = db.or_(Item.listed.is_(True), Item.curated.is_(True))
    return jsonify(**status, auto=get_meta("auto_update", "1") == "1",
                   last=int(float(get_meta("last_refresh", "0") or 0)),
                   missing=Item.query.filter(shown, no_image).count(),
                   total_titles=Item.query.filter(shown).count())


@app.route("/api/admin/auto", methods=["POST"])
@owner_required
def api_admin_auto():
    on = bool((request.get_json(silent=True) or {}).get("on"))
    set_meta("auto_update", "1" if on else "0")
    return jsonify(ok=True, auto=on)


# ------------------------------------------- user management (admin ONLY)
USERS_PER_PAGE = 20


def _premium_expr():
    return db.or_(User.premium.is_(True), db.func.coalesce(User.premium_until, 0) > int(time.time()))


@app.route("/api/admin/users")
@owner_required
def api_admin_users():
    q = request.args.get("q", "").strip().lower()[:30]
    flt = request.args.get("filter", "all")
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    others = User.query.filter(User.username != ADMIN_USER)
    counts = dict(total=User.query.count(),
                  premium=others.filter(_premium_expr()).count(),
                  free=others.filter(~_premium_expr()).count())
    rows = others
    if q:
        rows = rows.filter(User.username.contains(q))
    if flt == "premium":
        rows = rows.filter(_premium_expr())
    elif flt == "free":
        rows = rows.filter(~_premium_expr())
    matched = rows.count()
    users = rows.order_by(User.id.desc()).offset((page - 1) * USERS_PER_PAGE).limit(USERS_PER_PAGE).all()
    ids = [u.id for u in users]
    watched = dict(db.session.query(Watched.user_id, db.func.count(Watched.id))
                   .filter(Watched.user_id.in_(ids)).group_by(Watched.user_id).all()) if ids else {}
    return jsonify(counts=counts, matched=matched, page=page, per=USERS_PER_PAGE, users=[dict(
        id=u.id, username=u.username, permanent=bool(u.premium), premium_until=u.premium_until or 0,
        premium=premium_active(u), tokens=u.tokens or 0, watched=watched.get(u.id, 0),
        joined=u.created or 0, last_login=u.last_login or 0) for u in users])


def _managed_user(uid):
    u = db.session.get(User, uid)
    if not u or u.username == ADMIN_USER:
        return None
    return u


@app.route("/api/admin/users/<int:uid>/premium", methods=["POST"])
@owner_required
def api_admin_user_premium(uid):
    u = _managed_user(uid)
    if not u:
        return jsonify(error="No such user."), 404
    mode = (request.get_json(silent=True) or {}).get("mode")
    if mode == "on":                       # Premium with no expiry
        u.premium = True
    elif mode == "off":                    # back to a free account
        u.premium, u.premium_until = False, 0
    else:
        return jsonify(error="Unknown action."), 400
    db.session.commit()
    return jsonify(ok=True)


@app.route("/api/admin/users/<int:uid>/password", methods=["POST"])
@owner_required
def api_admin_user_password(uid):
    """Accounts have no email, so the admin can set a temporary password for someone who forgot theirs."""
    u = _managed_user(uid)
    if not u:
        return jsonify(error="No such user."), 404
    pw = secrets.token_urlsafe(9)
    u.pw = generate_password_hash(pw)
    db.session.commit()
    return jsonify(ok=True, password=pw)


@app.route("/api/admin/users/<int:uid>/delete", methods=["POST"])
@owner_required
def api_admin_user_delete(uid):
    u = _managed_user(uid)
    if not u:
        return jsonify(error="No such user."), 404
    log.info("admin deleted user %s", u.username)
    delete_user_data(u.id)
    return jsonify(ok=True)


# ----------------------------------------------------- tokens and Premium
def _token_state(u):
    return dict(tokens=u.tokens or 0, premium=premium_active(u) or is_owner(), permanent=bool(u.premium) or is_owner(),
                premium_until=u.premium_until or 0, cost=PREMIUM_COST, days=PREMIUM_DAYS,
                payments=PAYMENTS_ENABLED, ads=ADS_ENABLED)


@app.route("/api/tokens")
@login_required
def api_tokens():
    return jsonify(_token_state(current_user()))


@app.route("/api/premium/buy", methods=["POST"])
@login_required
def api_premium_buy():
    """Spend tokens to get (or extend) Premium. The balance check and the deduction are ONE database
    statement, so two quick clicks can never spend the same tokens twice."""
    u = current_user()
    if is_owner() or u.premium:
        return jsonify(error="Your account already has Premium with no expiry."), 400
    spent = User.query.filter(User.id == u.id, db.func.coalesce(User.tokens, 0) >= PREMIUM_COST).update(
        {User.tokens: db.func.coalesce(User.tokens, 0) - PREMIUM_COST}, synchronize_session=False)
    if not spent:
        db.session.rollback()
        return jsonify(error=f"You need {PREMIUM_COST} tokens for Premium."), 400
    db.session.refresh(u)
    start = max(int(time.time()), u.premium_until or 0)          # buying again extends the current period
    u.premium_until = start + PREMIUM_DAYS * 86400
    db.session.add(TokenLog(user_id=u.id, delta=-PREMIUM_COST, reason=f"Premium {PREMIUM_DAYS} days"))
    db.session.commit()
    return jsonify(ok=True, **_token_state(u))


@app.route("/api/tokens/buy", methods=["POST"])
@login_required
def api_tokens_buy():
    # Placeholder: real purchases need a payment gateway whose verified webhook calls credit_tokens().
    # Never add tokens here from data the browser sends.
    return jsonify(error="Buying tokens is coming soon."), 503


@app.route("/api/tokens/ad-reward", methods=["POST"])
@login_required
def api_tokens_ad():
    # Placeholder: rewarded ads must be verified by the ad network's server callback before calling credit_tokens().
    return jsonify(error="Earning tokens by watching ads is coming soon."), 503


@app.route("/api/admin/tokens", methods=["POST"])
@owner_required
def api_admin_tokens():
    """Admin only: add tokens to a user (negative removes). Useful for testing, support and refunds."""
    data = request.get_json(silent=True) or {}
    u = User.query.filter_by(username=str(data.get("username", "")).strip().lower()).first()
    if not u:
        return jsonify(error="No such user."), 404
    try:
        amount = int(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify(error="Type a whole number of tokens."), 400
    if amount == 0 or abs(amount) > 100000:
        return jsonify(error="Amount must be between -100000 and 100000, and not 0."), 400
    amount = max(amount, -(u.tokens or 0))                         # balance never goes below 0
    if amount == 0:
        return jsonify(error="That user has no tokens to remove."), 400
    credit_tokens(u.id, amount, "admin adjustment")
    db.session.refresh(u)
    return jsonify(ok=True, username=u.username, tokens=u.tokens or 0)


@app.route("/api/export")
@login_required
def api_export():
    data = {w.uid: True for w in Watched.query.filter_by(user_id=session["user_id"])}
    return Response(json.dumps(data, indent=1), mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=guidetoanime-watched.json"})


@app.route("/api/import", methods=["POST"])
@login_required
def api_import():
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        uids = [k for k, v in data.items() if v]
    elif isinstance(data, list):
        uids = [u for u in data if isinstance(u, str)]
    else:
        return jsonify(error="bad file"), 400
    uids = uids[:5000]
    valid = {i.uid for i in Item.query.filter(Item.uid.in_(uids)).all()}
    have = {w.uid for w in Watched.query.filter_by(user_id=session["user_id"])}
    new = valid - have
    for u in new:
        db.session.add(Watched(user_id=session["user_id"], uid=u))
    db.session.commit()
    return jsonify(added=len(new))


# ------------------------------------------- add / edit / delete (admin only)
def sniff_image(data):
    """Work out the real image type from the file itself (never trust the filename)."""
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return ""


def num(value, lo, hi):
    try:
        n = int(str(value).strip())
    except ValueError:
        return None
    return n if lo <= n <= hi else None


@app.route("/api/title", methods=["POST"])
@owner_required
def api_title():
    f = request.form
    title = f.get("title", "").strip()
    if not title or len(title) > 200:
        return jsonify(error="Please type the anime name (up to 200 letters)."), 400
    kind = "Movie" if f.get("kind") == "Movie" else "Series"
    tier = f.get("tier") if f.get("tier") in ("essential", "recommended", "optional") else "optional"
    genres = [g.strip() for g in f.get("genres", "").split(",") if g.strip()][:8]
    note = f.get("note", "").strip()[:300]
    image_url = f.get("image_url", "").strip()
    file = request.files.get("image")
    orig = f.get("uid", "").strip()

    if orig:  # editing an existing one
        it = Item.query.filter_by(uid=orig).first()
        if not it:
            return jsonify(error="That anime was not found."), 400
        it.title = title
    else:  # adding a new one
        uid = _key(title)
        if not uid:
            return jsonify(error="Please use letters or numbers in the name."), 400
        if Item.query.filter_by(uid=uid).first():
            return jsonify(error="That anime is already in the list. Use its Edit button."), 400
        it = Item(uid=uid, title=title, curated=True, listed=True)
        db.session.add(it)

    it.movie = kind == "Movie"
    it.year = num(f.get("year"), 1900, 2100)
    it.eps = num(f.get("eps"), 1, 5000)
    it.mins = num(f.get("mins"), 1, 600)
    it.genres = ",".join(genres)
    it.hindi = f.get("hindi") == "1"
    it.looked_up = True  # stops the auto-updater from touching your details

    ex = db.session.get(Extra, it.uid)
    if not ex:
        ex = Extra(uid=it.uid)
        db.session.add(ex)
    ex.tier, ex.note = tier, note

    if file and file.filename:
        data = file.read()
        mime = sniff_image(data)
        if not mime:
            db.session.rollback()
            return jsonify(error="That file is not a picture (use JPG, PNG, WEBP or GIF)."), 400
        if len(data) > 2 * 1024 * 1024:
            db.session.rollback()
            return jsonify(error="Picture is too big (max 2 MB)."), 400
        ex.data, ex.mime = data, mime
        it.image = f"/img/{it.uid}?v={int(time.time())}"
    elif image_url:
        if not re.match(r"^https?://", image_url, re.I) or len(image_url) > 500:
            db.session.rollback()
            return jsonify(error="Image link must start with http:// or https://"), 400
        it.image = image_url
        ex.data, ex.mime = None, ""

    db.session.commit()
    return jsonify(ok=True)


@app.route("/api/title/<uid>/delete", methods=["POST"])
@owner_required
def api_title_delete(uid):
    it = Item.query.filter_by(uid=uid).first()
    if not it or not it.curated:
        return jsonify(error="Anime that come from the website cannot be deleted."), 400
    Watched.query.filter_by(uid=uid).delete()
    Extra.query.filter_by(uid=uid).delete()
    db.session.delete(it)
    db.session.commit()
    return jsonify(ok=True)


@app.route("/img/<uid>")
def img(uid):
    ex = db.session.get(Extra, uid)
    if not ex or not ex.data:
        return Response(status=404)
    return Response(ex.data, mimetype=ex.mime or "image/jpeg",
                    headers={"Cache-Control": "public, max-age=31536000",
                             "X-Content-Type-Options": "nosniff"})


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


# ---------------------------------------------------- public info pages
def _legal(page, heading):
    return render_template("legal.html", page=page, heading=heading, contact=CONTACT_EMAIL, updated=POLICY_DATE)


@app.route("/privacy")
def privacy():
    return _legal("privacy", "Privacy Policy")


@app.route("/terms")
def terms():
    return _legal("terms", "Terms of Use")


@app.route("/contact")
def contact():
    return _legal("contact", "Contact")


@app.route("/favicon.ico")
def favicon():
    return redirect("/static/favicon.svg", code=301)


@app.route("/robots.txt")
def robots():
    base = request.url_root.rstrip("/")
    body = (f"User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /admin/\nDisallow: /login\n"
            f"Sitemap: {base}/sitemap.xml\n")
    return Response(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    base = request.url_root.rstrip("/")
    urls = "".join(f"<url><loc>{base}{p}</loc></url>" for p in ("/", "/privacy", "/terms", "/contact"))
    return Response('<?xml version="1.0" encoding="UTF-8"?>'
                    f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
                    mimetype="application/xml")


def _error(code, title, msg):
    if request.path.startswith("/api/"):
        return jsonify(error=msg), code
    return render_template("error.html", code=code, title=title, msg=msg), code


@app.errorhandler(404)
def not_found(e):
    return _error(404, "Page not found", "That page does not exist or has moved.")


@app.errorhandler(500)
def server_error(e):
    db.session.rollback()
    log.exception("server error: %s", e)
    return _error(500, "Something went wrong", "An unexpected error happened. Please try again in a moment.")


@app.errorhandler(413)
def too_big(e):
    return jsonify(error="Picture is too big (max 4 MB)."), 413


@app.route("/admin/refresh")
def admin_refresh():
    """Force an update: /admin/refresh?token=YOUR_ADMIN_TOKEN"""
    token = os.environ.get("ADMIN_TOKEN")
    if not token or not hmac.compare_digest(request.args.get("token", ""), token):
        return "Not allowed", 403
    if not run_task("all", force=True):
        return "An update is already running."
    return "Refresh started. Check again in a few minutes."


with app.app_context():
    db.create_all()
    ensure_columns()      # must run before anything reads the tables (adds new columns to old databases)
    seed()
    sync_scraped()
    migrate_curated()
    load_local_images()
    ensure_admin()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1")
