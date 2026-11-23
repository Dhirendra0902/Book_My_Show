"""BookMyShow booking server v3 (port 8000). Run: python server.py  (standard library only, fully offline)
Default admin login: admin / admin123  (change it in the code before sharing the app)"""
import hashlib, hmac, json, os, secrets, sqlite3, threading, time, urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlencode
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
GATEWAY, MERCHANT_KEY, WEBHOOK_SECRET = "http://localhost:9000", "mk_demo_bookmyshow", b"whsec_demo_change_me"
HOLD_SECONDS, CANCEL_CUTOFF, GST_RATE, DAY = 300, 2 * 3600, 0.18, 86400
# Seat layouts: list of (row letter, seats in row, price). Category comes from the price.
LAYOUTS = {
    "standard": [(r, 12, p) for r, p in zip("ABCDEFGH", [150, 150, 150, 250, 250, 250, 350, 350])],
    "mini": [(r, 8, p) for r, p in zip("ABCDEF", [120, 120, 220, 220, 320, 320])],
    "balcony": [(r, 14, 150) for r in "ABC"] + [(r, 14, 250) for r in "DE"] + [(r, 10, 350) for r in "FGH"]}
LABELS = {"standard": "Standard screen", "mini": "Mini screen", "balcony": "Balcony screen"}
PRICES = {n: {f"{r}{i}": p for r, c, p in rows for i in range(1, c + 1)} for n, rows in LAYOUTS.items()}
cat = lambda p: "Silver" if p < 200 else "Gold" if p < 300 else "Premium"

lock = threading.Lock()
db = sqlite3.connect(os.path.join(HERE, "bms3.db"), check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, salt TEXT, hash TEXT, is_admin INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS movies(id TEXT PRIMARY KEY, title TEXT, description TEXT, cast_list TEXT, minutes INTEGER,
  release REAL, critic REAL, quotes TEXT);
CREATE TABLE IF NOT EXISTS shows(id TEXT PRIMARY KEY, movie_id TEXT, theatre TEXT, start REAL, layout TEXT);
CREATE TABLE IF NOT EXISTS orders(order_id TEXT PRIMARY KEY, username TEXT, show_id TEXT, subtotal INTEGER, discount INTEGER,
  gst INTEGER, total INTEGER, coupon TEXT, status TEXT, created REAL);
CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, username TEXT, show_id TEXT,
  seat TEXT, status TEXT DEFAULT 'booked');
CREATE UNIQUE INDEX IF NOT EXISTS one_seat ON bookings(show_id, seat) WHERE status='booked';
CREATE TABLE IF NOT EXISTS coupons(code TEXT PRIMARY KEY, kind TEXT, value INTEGER, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY AUTOINCREMENT, movie_id TEXT, username TEXT, rating INTEGER,
  text TEXT, created REAL, UNIQUE(movie_id, username));""")
holds, orders, tokens, fails = {}, {}, {}, {}   # (show,seat)->(order,expiry) | pending orders | token->user | login failures
run = lambda sql, a=(): db.execute(sql, a).fetchall()


def pw_hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def seed():  # first run only: demo movies, shows, coupons and the admin account
    if run("SELECT 1 FROM movies"):
        return
    now = time.time()
    movies = [
        ("m1", "Skyline Run", "A retired pilot is pulled back for one last rescue across a collapsing city.",
         "Kabir Anand, Meera Joshi, Dev Malhotra", 142, now - 10 * DAY, 4.5,
         [("The Daily Reel", "Breathless action with a surprising heart."), ("Screen & Sound", "The best thrill ride of the year.")]),
        ("m2", "The Last Lighthouse", "A keeper and a stranded sailor share one stormy winter on a forgotten island.",
         "Isha Verma, Rohit Bhalla", 118, now - 3 * DAY, 4.2,
         [("The Daily Reel", "Quiet, beautiful and quietly devastating.")]),
        ("m3", "Laughter Lane", "Four neighbours try to hide a surprise wedding from the bride's famously nosy mother.",
         "Tanya Kapoor, Sameer Gill, Neha Rawat", 126, now, 3.4, [])]
    with db:
        for m in movies:
            db.execute("INSERT INTO movies VALUES(?,?,?,?,?,?,?,?)", (*m[:7], json.dumps(m[7])))
        for sid, mid, th, hrs, lay in [("s1", "m1", "PVR Jaipur", 1, "standard"), ("s2", "m1", "INOX Jaipur", 5, "balcony"),
                                       ("s3", "m2", "PVR Jaipur", 3, "standard"), ("s4", "m2", "Cinepolis Jaipur", 26, "mini"),
                                       ("s5", "m3", "INOX Jaipur", 30, "balcony"), ("s6", "m3", "Cinepolis Jaipur", 34, "mini")]:
            db.execute("INSERT INTO shows VALUES(?,?,?,?,?)", (sid, mid, th, now + hrs * 3600, lay))
        db.executemany("INSERT INTO coupons VALUES(?,?,?,1)", [("SAVE10", "percent", 10), ("FLAT50", "flat", 50)])
        salt = secrets.token_hex(8)
        db.execute("INSERT OR IGNORE INTO users VALUES('admin',?,?,1)", (salt, pw_hash("admin123", salt)))


seed()


def label(t):  # "Today 6:00 PM"
    d = (datetime.fromtimestamp(t).date() - datetime.now().date()).days
    day = "Today" if d == 0 else "Tomorrow" if d == 1 else datetime.fromtimestamp(t).strftime("%d %b")
    return day + " " + datetime.fromtimestamp(t).strftime("%I:%M %p").lstrip("0")


def days_since(t):
    return (datetime.now().date() - datetime.fromtimestamp(t).date()).days


def purge():  # call while holding `lock`
    for k, (_, exp) in list(holds.items()):
        if exp < time.time():
            del holds[k]


def release_holds(oid):
    with lock:
        for k in [k for k, v in holds.items() if v[0] == oid]:
            del holds[k]


def booked_in(show):
    return {r[0] for r in run("SELECT seat FROM bookings WHERE show_id=? AND status='booked'", (show,))}


def gateway(path, body):
    req = urllib.request.Request(GATEWAY + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "X-Merchant-Key": MERCHANT_KEY})
    return json.load(urllib.request.urlopen(req, timeout=5))


def is_admin(u):
    r = db.execute("SELECT is_admin FROM users WHERE username=?", (u,)).fetchone()
    return bool(u and r and r[0])


# ---------- real movies from TMDB (needs internet and your API key) ----------
sync_status = {"state": "idle", "message": "Not synced yet", "count": 0}
THEATRES = [("PVR Jaipur", "standard"), ("INOX Jaipur", "balcony"), ("Cinepolis Jaipur", "mini")]


def tmdb_key():  # key comes from the TMDB_API_KEY setting or from a file named tmdb_key.txt next to this script
    k = os.environ.get("TMDB_API_KEY", "").strip()
    f = os.path.join(HERE, "tmdb_key.txt")
    if not k and os.path.exists(f):
        k = open(f, encoding="utf-8").read().strip()
    return k


def tmdb_get(path, **params):
    key, headers = tmdb_key(), {"accept": "application/json"}
    if len(key) > 40:
        headers["Authorization"] = "Bearer " + key      # long "read access token"
    else:
        params["api_key"] = key                         # normal 32-character key
    req = urllib.request.Request("https://api.themoviedb.org/3" + path + "?" + urlencode(params), headers=headers)
    return json.load(urllib.request.urlopen(req, timeout=20))


def ensure_shows():  # give every listed movie some upcoming shows (today and tomorrow) if it has none
    now = time.time()
    real = db.execute("SELECT 1 FROM movies WHERE id LIKE 't%'").fetchone()
    with lock, db:
        ids = [r[0] for r in run("SELECT id FROM movies WHERE " + ("id LIKE 't%'" if real else "1=1") + " ORDER BY id")]
        for i, mid in enumerate(ids):
            if db.execute("SELECT 1 FROM shows WHERE movie_id=? AND start>?", (mid, now)).fetchone():
                continue
            slots = ["11:00", "14:30", "18:00", "21:30"]
            for t in range(2):
                th, lay = THEATRES[(i + t) % 3]
                for day in (0, 1):
                    for sl in (slots[(i + t) % 4], slots[(i + t + 2) % 4]):
                        hh, mm = map(int, sl.split(":"))
                        when = (datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=day)).timestamp()
                        if when > now + 1800:
                            db.execute("INSERT INTO shows VALUES(?,?,?,?,?)", ("x" + secrets.token_hex(4), mid, th, when, lay))


def sync_movies():  # download the "now playing in India" list, then each movie's cast, runtime and a review
    sync_status.update(state="running", message="Fetching the now-playing list...")
    try:
        basics, page = [], 1
        while page <= 5:
            d = tmdb_get("/movie/now_playing", region="IN", language="en-US", page=page)
            basics += d["results"]
            if page >= d.get("total_pages", 1):
                break
            page += 1

        def detail(m):
            try:
                return m, tmdb_get(f"/movie/{m['id']}", append_to_response="credits,reviews", language="en-US")
            except Exception:
                return m, {}
        with ThreadPoolExecutor(8) as ex:
            results = list(ex.map(detail, basics))
        n = 0
        with lock, db:
            for m, d in results:
                if not m.get("release_date") or not m.get("title"):
                    continue
                cast = ", ".join(c["name"] for c in d.get("credits", {}).get("cast", [])[:5]) or "Cast not listed"
                critic = round(m.get("vote_average", 0) / 2, 1) if m.get("vote_count", 0) >= 20 else 0   # TMDB scores are out of 10
                quotes = []
                if critic >= 4.0:   # an excerpt of a viewer review, only for highly rated movies
                    for r in d.get("reviews", {}).get("results", [])[:1]:
                        txt = " ".join(r.get("content", "").split())
                        quotes.append({"critic": "viewer review by " + r.get("author", "a viewer"), "quote": txt[:200] + ("..." if len(txt) > 200 else "")})
                db.execute("INSERT OR REPLACE INTO movies VALUES(?,?,?,?,?,?,?,?)",
                           ("t%d" % m["id"], m["title"], m.get("overview") or "No description available.", cast, d.get("runtime") or 0,
                            datetime.strptime(m["release_date"], "%Y-%m-%d").timestamp(), critic, json.dumps(quotes)))
                n += 1
        ensure_shows()
        sync_status.update(state="done", message=f"Imported {n} movies at {time.strftime('%H:%M')}", count=n)
    except Exception as e:
        why = type(e).__name__ + " - " + str(e)[:150]   # the reason, e.g. "HTTP Error 401" (wrong key) or a timeout (blocked internet)
        print("TMDB sync failed:", why)
        sync_status.update(state="error", message="Sync failed: " + why)


def start_sync():
    if not tmdb_key():
        return 400, {"error": "No API key found. Put your TMDB key in tmdb_key.txt next to server.py and restart."}
    if sync_status["state"] != "running":
        threading.Thread(target=sync_movies, daemon=True).start()
    return 200, {"message": "Sync started"}


def background():  # runs at start and then every 6 hours
    while True:
        try:
            sync_movies() if tmdb_key() else ensure_shows()
        except Exception as e:
            print("background task failed:", type(e).__name__)
        time.sleep(6 * 3600)


# ---------- movies and reviews ----------
def list_movies(q):
    out = []
    flt = "id LIKE 't%'" if db.execute("SELECT 1 FROM movies WHERE id LIKE 't%'").fetchone() else "1=1"   # real movies replace the demo ones
    for mid, title, mins, rel, critic in run(f"SELECT id,title,minutes,release,critic FROM movies WHERE title LIKE ? AND {flt} AND EXISTS(SELECT 1 FROM shows WHERE movie_id=movies.id AND start>?) ORDER BY release DESC", (f"%{q}%", time.time())):
        d = days_since(rel)
        avg = db.execute("SELECT AVG(rating) FROM reviews WHERE movie_id=?", (mid,)).fetchone()[0]
        out.append({"id": mid, "title": title, "minutes": mins, "critic": critic, "first_day": d == 0,
                    "rating": round(avg, 1) if avg and d > 0 else None,
                    "released": "Releases today" if d == 0 else f"Released {d} day(s) ago"})
    return out


def movie_detail(mid, user):
    m = db.execute("SELECT id,title,description,cast_list,minutes,release,critic,quotes FROM movies WHERE id=?", (mid,)).fetchone()
    if not m:
        return None
    first = days_since(m[5]) == 0
    out = {"id": m[0], "title": m[1], "description": m[2], "cast": m[3], "minutes": m[4], "critic": m[6], "first_day": first,
           "quotes": json.loads(m[7]) if m[6] >= 4.0 else [],   # critic quotes only for highly rated movies
           "shows": [{"id": s[0], "theatre": s[1], "time": label(s[2]), "layout": LABELS[s[3]]} for s in
                     run("SELECT id,theatre,start,layout FROM shows WHERE movie_id=? AND start>? ORDER BY start", (mid, time.time()))],
           "reviews_hidden": first, "reviews": [], "can_review": False}
    if not first:   # no reviews on the first day of release
        out["reviews"] = [{"user": r[0], "rating": r[1], "text": r[2]} for r in
                          run("SELECT username,rating,text FROM reviews WHERE movie_id=? ORDER BY created DESC", (mid,))]
        out["can_review"] = bool(user and run("SELECT 1 FROM bookings b JOIN shows s ON s.id=b.show_id WHERE s.movie_id=? AND b.username=? AND b.status='booked'", (mid, user)))
    return out


def add_review(user, d):
    m = db.execute("SELECT release FROM movies WHERE id=?", (d.get("movie_id"),)).fetchone()
    rating, text = d.get("rating"), str(d.get("text", "")).strip()[:500]
    if not m or days_since(m[0]) == 0:
        return 403, {"error": "Reviews open from the second day of a movie's release"}
    if not movie_detail(d["movie_id"], user)["can_review"]:
        return 403, {"error": "Only people with a confirmed booking can review"}
    if rating not in (1, 2, 3, 4, 5):
        return 400, {"error": "Rating must be 1 to 5"}
    with db:
        db.execute("INSERT OR REPLACE INTO reviews(movie_id,username,rating,text,created) VALUES(?,?,?,?,?)", (d["movie_id"], user, rating, text, time.time()))
    return 200, {"ok": True}


# ---------- seats, prices, holds ----------
def seat_map(show):
    s = db.execute("SELECT s.theatre,s.start,s.layout,m.title FROM shows s JOIN movies m ON m.id=s.movie_id WHERE s.id=?", (show,)).fetchone()
    if not s:
        return None
    with lock:
        purge()
        booked = booked_in(show)
        rows = [{"row": r, "seats": [{"id": f"{r}{i}", "price": p, "cat": cat(p),
                 "status": "booked" if f"{r}{i}" in booked else "held" if (show, f"{r}{i}") in holds else "free"}
                 for i in range(1, c + 1)]} for r, c, p in LAYOUTS[s[2]]]
    return {"title": s[3], "theatre": s[0], "time": label(s[1]), "layout": LABELS[s[2]], "rows": rows}


def check(show, seats):
    s = db.execute("SELECT start, layout FROM shows WHERE id=?", (show,)).fetchone()
    if not s:
        return None, "No such show"
    if time.time() >= s[0]:
        return None, "This show has already started"
    seats = list(dict.fromkeys(seats)) if isinstance(seats, list) else []
    if not seats or len(seats) > 10 or any(not isinstance(x, str) or x not in PRICES[s[1]] for x in seats):
        return None, "Pick 1 to 10 valid seats"
    return (seats, s[1]), None


def price_info(subtotal, code):
    disc, code = 0, (code or "").strip().upper()
    if code:
        c = db.execute("SELECT kind, value FROM coupons WHERE code=? AND active=1", (code,)).fetchone()
        if not c:
            return None, "Invalid or expired coupon code"
        disc = min(subtotal, subtotal * c[1] // 100 if c[0] == "percent" else c[1])
    gst = round((subtotal - disc) * GST_RATE)
    return {"subtotal": subtotal, "discount": disc, "gst": gst, "total": subtotal - disc + gst, "coupon": code}, None


def quote(show, seats, coupon):
    ok, err = check(show, seats)
    if err:
        return 400, {"error": err}
    info, err = price_info(sum(PRICES[ok[1]][s] for s in ok[0]), coupon)
    return (400, {"error": err}) if err else (200, info)


def create_hold(user, show, seats, coupon):
    code, info = quote(show, seats, coupon)
    if code != 200:
        return code, info
    seats = list(dict.fromkeys(seats))
    with lock:
        purge()
        booked = booked_in(show)
        taken = [s for s in seats if s in booked or (show, s) in holds]
        if taken:
            return 409, {"error": "Just taken by someone else: " + ", ".join(taken)}
        oid, exp = "ORD" + secrets.token_hex(4).upper(), time.time() + HOLD_SECONDS
        for s in seats:
            holds[(show, s)] = (oid, exp)
        orders[oid] = {"user": user, "show": show, "seats": seats, "info": info, "status": "pending", "expires": exp}
    title = db.execute("SELECT m.title FROM shows s JOIN movies m ON m.id=s.movie_id WHERE s.id=?", (show,)).fetchone()[0]
    try:
        url = gateway("/api/sessions", {"order_id": oid, "amount": info["total"], "webhook_url": "http://localhost:8000/api/webhook/payment",
                                        "description": f"{title} - seats " + ", ".join(seats)})["payment_url"]
    except Exception:
        release_holds(oid)
        orders[oid]["status"] = "failed"
        return 502, {"error": "Payment gateway is unavailable"}
    return 200, {"order_id": oid, "total": info["total"], "seats": seats, "payment_url": url, "expires_in": HOLD_SECONDS}


# ---------- bookings and cancel ----------
def my_bookings(user):
    out = []
    for oid, show, sub, disc, gst, total, status, coupon in run("SELECT order_id,show_id,subtotal,discount,gst,total,status,coupon FROM orders WHERE username=? ORDER BY created DESC", (user,)):
        th, start, title = db.execute("SELECT s.theatre,s.start,m.title FROM shows s JOIN movies m ON m.id=s.movie_id WHERE s.id=?", (show,)).fetchone()
        out.append({"order_id": oid, "movie": title, "theatre": th, "time": label(start), "status": status, "total": total,
                    "subtotal": sub, "discount": disc, "gst": gst, "coupon": coupon,
                    "seats": [r[0] for r in run("SELECT seat FROM bookings WHERE order_id=?", (oid,))],
                    "can_cancel": status == "booked" and start - time.time() >= CANCEL_CUTOFF})
    return out


def cancel(user, oid):
    with lock:
        o = db.execute("SELECT o.total, s.start FROM orders o JOIN shows s ON s.id=o.show_id WHERE o.order_id=? AND o.username=? AND o.status='booked'", (oid, user)).fetchone()
        if not o:
            return 404, {"error": "No active booking found"}
        if o[1] - time.time() < CANCEL_CUTOFF:
            return 403, {"error": "Cancellation is closed. You can cancel only until 2 hours before the show."}
        try:
            refund = gateway("/api/refunds", {"order_id": oid, "amount": o[0]})
        except Exception:
            return 502, {"error": "Refund failed, booking not cancelled. Is the gateway running?"}
        with db:
            db.execute("UPDATE orders SET status='cancelled' WHERE order_id=?", (oid,))
            db.execute("UPDATE bookings SET status='cancelled' WHERE order_id=?", (oid,))
    return 200, {"refund_id": refund["refund_id"], "amount": o[0]}


# ---------- admin ----------
def stats():
    rev, n = db.execute("SELECT COALESCE(SUM(total),0), COUNT(*) FROM orders WHERE status='booked'").fetchone()
    shows = []
    for sid, title, th, st, lay in run("SELECT s.id,m.title,s.theatre,s.start,s.layout FROM shows s JOIN movies m ON m.id=s.movie_id ORDER BY s.start"):
        sold = db.execute("SELECT COUNT(*) FROM bookings WHERE show_id=? AND status='booked'", (sid,)).fetchone()[0]
        r = db.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE show_id=? AND status='booked'", (sid,)).fetchone()[0]
        shows.append({"id": sid, "movie": title, "theatre": th, "time": label(st), "layout": LABELS[lay], "sold": sold, "capacity": len(PRICES[lay]), "revenue": r})
    return {"revenue": rev, "orders": n, "cancelled": db.execute("SELECT COUNT(*) FROM orders WHERE status='cancelled'").fetchone()[0],
            "shows": shows, "coupons": [{"code": c[0], "kind": c[1], "value": c[2]} for c in run("SELECT code,kind,value FROM coupons")],
            "movies": [{"id": m[0], "title": m[1]} for m in run("SELECT id,title FROM movies")], "layouts": LABELS,
            "tmdb": {"configured": bool(tmdb_key()), **sync_status}}


def admin_post(p, d):
    if p == "/api/admin/show":
        try:
            hrs = float(d["hours"])
        except (KeyError, ValueError, TypeError):
            return 400, {"error": "Hours from now must be a number"}
        if not db.execute("SELECT 1 FROM movies WHERE id=?", (d.get("movie_id"),)).fetchone() or d.get("layout") not in LAYOUTS or not str(d.get("theatre", "")).strip():
            return 400, {"error": "Choose a movie, layout and theatre name"}
        with db:
            db.execute("INSERT INTO shows VALUES(?,?,?,?,?)", ("s" + secrets.token_hex(3), d["movie_id"], str(d["theatre"]).strip()[:40], time.time() + hrs * 3600, d["layout"]))
        return 200, {"ok": True}
    code, kind = str(d.get("code", "")).strip().upper(), d.get("kind")
    val = d.get("value")
    if not (3 <= len(code) <= 12 and code.isalnum()) or kind not in ("percent", "flat") or not isinstance(val, int) or not 0 < val <= (90 if kind == "percent" else 5000):
        return 400, {"error": "Code 3-12 letters/digits, kind percent or flat, value 1-90 (percent) or 1-5000 (flat)"}
    with db:
        db.execute("INSERT OR REPLACE INTO coupons VALUES(?,?,?,1)", (code, kind, val))
    return 200, {"ok": True}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, code, obj, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def user(self):
        return tokens.get(self.headers.get("Authorization", "").replace("Bearer ", ""))

    def do_GET(self):
        p, _, q = self.path.partition("?")
        qs, u = parse_qs(q), self.user()
        if p == "/":
            return self.send(200, open(os.path.join(HERE, "index.html"), "rb").read(), "text/html")
        if p == "/api/movies":
            return self.send(200, list_movies(qs.get("q", [""])[0]))
        if p.startswith("/api/movies/"):
            d = movie_detail(unquote(p.rsplit("/", 1)[1]), u)
            return self.send(200, d) if d else self.send(404, {"error": "No such movie"})
        if p == "/api/seats":
            d = seat_map(qs.get("show", [""])[0])
            return self.send(200, d) if d else self.send(404, {"error": "No such show"})
        if p == "/api/bookings":
            return self.send(200, my_bookings(u)) if u else self.send(401, {"error": "Please log in"})
        if p == "/api/admin/stats":
            return self.send(200, stats()) if is_admin(u) else self.send(403, {"error": "Admins only"})
        if p.startswith("/api/orders/"):
            oid = p.rsplit("/", 1)[1]
            o = orders.get(oid)
            if not u or not o or o["user"] != u:
                return self.send(404, {"error": "Order not found"})
            if o["status"] == "pending" and o["expires"] < time.time():
                o["status"] = "expired"
                release_holds(oid)
            return self.send(200, {"status": o["status"], "seats": o["seats"], "total": o["info"]["total"]})
        self.send(404, {"error": "Not found"})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            d = json.loads(raw or b"{}")
            assert isinstance(d, dict)
        except (ValueError, AssertionError):
            return self.send(400, {"error": "Bad JSON"})
        p, u = self.path, self.user()
        if p in ("/api/signup", "/api/login"):
            name, pw = str(d.get("username", "")).strip().lower(), str(d.get("password", ""))
            if p == "/api/signup":
                if len(name) < 3 or len(pw) < 6:
                    return self.send(400, {"error": "Username 3+ chars, password 6+ chars"})
                salt = secrets.token_hex(8)
                try:
                    with db:
                        db.execute("INSERT INTO users(username,salt,hash) VALUES(?,?,?)", (name, salt, pw_hash(pw, salt)))
                except sqlite3.IntegrityError:
                    return self.send(409, {"error": "Username already exists"})
            else:
                fails[name] = [t for t in fails.get(name, []) if t > time.time() - 300]   # rate limit: 5 misses per 5 min
                if len(fails[name]) >= 5:
                    return self.send(429, {"error": "Too many wrong attempts. Try again in 5 minutes."})
                row = db.execute("SELECT salt, hash FROM users WHERE username=?", (name,)).fetchone()
                if not row or not hmac.compare_digest(row[1], pw_hash(pw, row[0])):
                    fails[name].append(time.time())
                    return self.send(401, {"error": "Wrong username or password"})
                fails.pop(name, None)
            t = secrets.token_hex(16)
            tokens[t] = name
            return self.send(200, {"token": t, "username": name, "is_admin": is_admin(name)})
        if p == "/api/logout":
            tokens.pop(self.headers.get("Authorization", "").replace("Bearer ", ""), None)
            return self.send(200, {"ok": True})
        if p == "/api/webhook/payment":  # called by the gateway only; signature proves it
            if not hmac.compare_digest(hmac.new(WEBHOOK_SECRET, raw, "sha256").hexdigest(), self.headers.get("X-Signature", "")):
                return self.send(401, {"error": "Bad signature"})
            oid = d.get("order_id")
            o = orders.get(oid)
            if o and o["status"] == "pending":
                if d.get("status") == "paid":
                    o["status"] = "paid"
                    i = o["info"]
                    try:
                        with lock, db:
                            for s in o["seats"]:
                                db.execute("INSERT INTO bookings(order_id,username,show_id,seat) VALUES(?,?,?,?)", (oid, o["user"], o["show"], s))
                            db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)", (oid, o["user"], o["show"], i["subtotal"], i["discount"], i["gst"], i["total"], i["coupon"], "booked", time.time()))
                    except sqlite3.IntegrityError:
                        o["status"] = "refund"
                else:
                    o["status"] = "failed"
                release_holds(oid)
            return self.send(200, {"ok": True})
        if not u:
            return self.send(401, {"error": "Please log in"})
        if p == "/api/quote":
            code, out = quote(d.get("show"), d.get("seats"), d.get("coupon"))
        elif p == "/api/hold":
            code, out = create_hold(u, d.get("show"), d.get("seats"), d.get("coupon"))
        elif p == "/api/cancel":
            code, out = cancel(u, d.get("order_id"))
        elif p == "/api/review":
            code, out = add_review(u, d)
        elif p == "/api/admin/sync":
            code, out = start_sync() if is_admin(u) else (403, {"error": "Admins only"})
        elif p in ("/api/admin/show", "/api/admin/coupon"):
            code, out = admin_post(p, d) if is_admin(u) else (403, {"error": "Admins only"})
        else:
            code, out = 404, {"error": "Not found"}
        self.send(code, out)


if __name__ == "__main__":
    print("Booking site: http://localhost:8000")
    print("TMDB key found: loading the latest movies in the background" if tmdb_key() else "No tmdb_key.txt found: showing the demo movies")
    threading.Thread(target=background, daemon=True).start()
    ThreadingHTTPServer(("", 8000), H).serve_forever()
