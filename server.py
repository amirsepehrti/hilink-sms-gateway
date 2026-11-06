#!/usr/bin/env python3
"""SMS Gateway - stdlib only."""
import glob, os, json, re, sys, hmac, logging, time, threading, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import urlparse, parse_qs
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import hilink, db, auth, store, sender, release

TOKEN = os.environ.get("SMSGW_TOKEN", "")
PORT = int(os.environ.get("SMSGW_PORT", "8080"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("smsgw")

VERSION = release.version()
STATIC_DIR = os.path.join(BASE_DIR, "static")
STATIC_MIME = {".woff2": "font/woff2", ".woff": "font/woff",
               ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
               ".png": "image/png", ".svg": "image/svg+xml",
               ".webmanifest": "application/manifest+json; charset=utf-8"}

NTFY_URL = os.environ.get("NTFY_URL", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "smsgw")
NTFY_POLL_INTERVAL = int(os.environ.get("NTFY_POLL_INTERVAL", "30"))
NTFY_STATE_FILE = os.environ.get("SMSGW_NTFY_STATE",
                                 os.path.join(BASE_DIR, "ntfy_state.json"))
NTFY_SETTLE = int(os.environ.get("SMSGW_NTFY_SETTLE", "45"))  # پیام بلند تکه‌تکه می‌رسد، باید کامل شود
STORAGE_ALERT = 90         # درصد پر بودن حافظه‌ی مودم برای هشدار
STORAGE_ALERT_GAP = 6 * 3600

# فقط از این آدرس‌ها هدر X-Forwarded-For پذیرفته می‌شود؛ وگرنه IP واقعی سوکت
# ملاک است. بدون این، هر کسی روی شبکه می‌توانست rate-limit را دور بزند.
TRUSTED_PROXIES = set(filter(None, os.environ.get(
    "SMSGW_TRUSTED_PROXIES", "127.0.0.1").split(",")))
CORS_ORIGINS = set(filter(None, os.environ.get("SMSGW_CORS_ORIGINS", "").split(",")))

MAX_BODY = 2 * 1024 * 1024
USSD_RATE = int(os.environ.get("SMSGW_USSD_RATE", "10"))   # در دقیقه
USSD_RE = re.compile(r"^[0-9A-Za-z*#+]{1,64}$")

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; manifest-src 'self'; worker-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def _merge(msgs):
    g, order = {}, []
    for m in sorted(msgs, key=lambda x: x["index"]):
        k = (m["phone"], m["date"])
        if k not in g:
            g[k] = {"phone": m["phone"], "date": m["date"], "content": "",
                    "unread": False, "indexes": []}
            order.append(k)
        g[k]["content"] += m["content"]
        g[k]["unread"] = g[k]["unread"] or m["unread"]
        g[k]["indexes"].append(m["index"])
    out = [dict(g[k], index=g[k]["indexes"][0], parts=len(g[k]["indexes"])) for k in order]
    out.sort(key=lambda m: m["date"], reverse=True)
    return out


def _ntfy_publish(title, message):
    if not NTFY_URL:        # روی دستگاه بدون ntfy فوروارد خاموش است
        return
    payload = json.dumps({"topic": NTFY_TOPIC, "title": title, "message": message,
                           "tags": ["envelope"]}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(NTFY_URL.rstrip("/") + "/", data=payload,
                                  headers={"Content-Type": "application/json; charset=utf-8"})
    urllib.request.urlopen(req, timeout=10).read()


def _load_last_index():
    try:
        with open(NTFY_STATE_FILE) as f:
            return json.load(f).get("last_index")
    except (OSError, ValueError):
        return None


def _save_last_index(idx):
    tmp = NTFY_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"last_index": idx}, f)
    os.replace(tmp, NTFY_STATE_FILE)


_last_ntfy_index = _load_last_index()
_ntfy_pending = {}
_storage_alert_at = 0.0


def poll_inbox_once():
    """یک بار خواندن صندوق ورودی: آینه‌سازی در دیتابیس + فوروارد به ntfy.

    ⚠️ این تابع مستقیم صدا زده نمی‌شود — فقط `sender.modem_loop` آن را
    اجرا می‌کند، آن هم داخل `sender.GATE.lock`. دلیلش بخش ۷ مشخصات و
    توضیح بالای `sender.py` است: هیچ‌وقت دو مسیر موازی به مودم.

    آینه‌سازی مستقل از ntfy است و همیشه انجام می‌شود — تاریخچه نباید به
    پیکربندی اعلان وابسته باشد.
    """
    global _last_ntfy_index
    if db.setting("modem_wipe", "") == "1":
        return              # تا خالی شدن مودم چیزی دوباره آینه نشود
    merged = _merge(hilink.inbox(count=50, box=1))
    try:
        n = store.mirror_incoming(merged)
        if n:
            log.info("mirrored %d new incoming message(s)", n)
    except Exception:
        log.exception("mirror failed")
    if _last_ntfy_index is None:
        _last_ntfy_index = max((max(m["indexes"]) for m in merged), default=0)
        _save_last_index(_last_ntfy_index)
        return
    top = max((max(m["indexes"]) for m in merged), default=None)
    # حین purge جدیدترین‌ها اول حذف می‌شوند و پایین آمدن ایندکس طبیعی است؛
    # ریست کردن در آن حالت پیام‌های قدیمی را دوباره به ntfy می‌فرستاد.
    if top is not None and top < _last_ntfy_index and not db.setting("modem_purge", ""):
        # مودم بعد از خالی شدن ایندکس را از نو شروع کرده؛ وگرنه هیچ پیامی
        # دیگر «جدید» حساب نمی‌شد.
        log.warning("modem index restarted (%d < %d)", top, _last_ntfy_index)
        _last_ntfy_index = min(min(m["indexes"]) for m in merged) - 1
        _save_last_index(_last_ntfy_index)
    new = sorted((m for m in merged if min(m["indexes"]) > _last_ntfy_index),
                 key=lambda m: min(m["indexes"]))
    now = time.time()
    blocked = False
    for m in new:
        key = min(m["indexes"])
        seen = _ntfy_pending.get(key)
        if not seen or seen[0] != m["content"]:
            # متن هنوز در حال کامل شدن است؛ یک دور صبر تا تکه‌ها برسند
            _ntfy_pending[key] = (m["content"], now)
            blocked = True
        if blocked or now - _ntfy_pending[key][1] < NTFY_SETTLE:
            blocked = True
            continue
        try:
            _ntfy_publish("پیامک جدید از " + m["phone"], m["content"])
        except Exception:
            log.exception("ntfy publish failed")   # دور بعد دوباره تلاش می‌شود
            return
        _ntfy_pending.pop(key, None)
        _last_ntfy_index = max(_last_ntfy_index, max(m["indexes"]))
        _save_last_index(_last_ntfy_index)


def storage_tick():
    """نگهداری حافظه‌ی مودم؛ بعد از هر poll از داخل حلقه‌ی مودم اجرا می‌شود."""
    global _storage_alert_at
    if sender.purge_tick(_merge):
        return
    usage = sender.clean_storage(_merge, _last_ntfy_index or 0)
    if usage["percent"] < STORAGE_ALERT:
        return
    log.warning("modem storage %d/%d", usage["used"], usage["max"])
    if time.time() - _storage_alert_at < STORAGE_ALERT_GAP:
        return
    _storage_alert_at = time.time()
    try:
        _ntfy_publish("حافظه‌ی مودم پیامک پر است",
                      "%d از %d پیام. وقتی پر شود پیامک جدید دریافت نمی‌شود."
                      % (usage["used"], usage["max"]))
    except Exception:
        log.exception("storage alert failed")


# ------------------------------------------------------------------ rate limit
class RateLimit:
    """پنجره‌ی لغزان ساده و درون‌حافظه‌ای."""

    def __init__(self):
        self.hits = {}
        self.lock = threading.Lock()

    def allow(self, key, limit, window):
        now = time.time()
        with self.lock:
            v = [t for t in self.hits.get(key, ()) if t > now - window]
            if len(v) >= limit:
                self.hits[key] = v
                return False
            v.append(now)
            self.hits[key] = v
            return True

    def over(self, key, limit, window):
        """آیا سقف رد شده؟ بدون ثبت رخداد جدید."""
        now = time.time()
        with self.lock:
            v = [t for t in self.hits.get(key, ()) if t > now - window]
            self.hits[key] = v
            return len(v) >= limit

    def hit(self, key):
        with self.lock:
            self.hits.setdefault(key, []).append(time.time())

    def prune(self, window=3600):
        now = time.time()
        with self.lock:
            for k in [k for k, v in self.hits.items()
                      if not v or max(v) < now - window]:
                del self.hits[k]


RL = RateLimit()


def janitor():
    """پاک‌سازی دوره‌ای سشن‌های منقضی و جدول rate-limit."""
    while True:
        time.sleep(600)
        try:
            n = auth.purge_expired_sessions()
            if n:
                log.info("purged %d expired sessions", n)
            RL.prune()
        except Exception:
            log.exception("janitor failed")


# ---------------------------------------------------------------------- audit
def audit(ident, action, detail="", ip=""):
    try:
        db.ex("INSERT INTO audit_log(user_id,actor,action,detail,ip,created_at) "
              "VALUES(?,?,?,?,?,?)",
              (ident.user_id if ident else None,
               ident.label if ident else "anonymous",
               action, detail[:500], ip, db.now()))
    except Exception:
        log.exception("audit write failed")


# ---------------------------------------------------------------------- routes
PUBLIC = "__public__"
GET_ROUTES = {}
POST_ROUTES = {}


def get(path, perm):
    def deco(fn):
        GET_ROUTES[path] = (perm, fn)
        return fn
    return deco


def post(path, perm):
    def deco(fn):
        POST_ROUTES[path] = (perm, fn)
        return fn
    return deco


# تنها مسیرهای مجاز برای حسابی که هنوز رمز اولیه را عوض نکرده است
PW_CHANGE_OPEN = {"/api/me", "/api/password", "/api/logout"}


def _same(a, b):
    """مقایسه‌ی زمان‌ثابت. هدر غیر ASCII نباید استثنا بدهد."""
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


class HttpError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


class Server(ThreadingHTTPServer):
    request_queue_size = 64
    daemon_threads = True


class H(BaseHTTPRequestHandler):
    # HTTP/1.0 (پیش‌فرض) عمداً نگه داشته شده: با keep-alive باید بدنه‌ی
    # درخواست‌هایی که قبل از خواندن رد می‌شوند (۴۰۱/۴۰۳) drain شود وگرنه
    # درخواست بعدی روی همان سوکت خراب پارس می‌شود.
    server_version = "smsgw/2.0"
    sys_version = ""
    timeout = 60            # کلاینت کند نباید thread را برای همیشه نگه دارد
    _head = False
    _q = None

    # ------------------------------------------------------------- utilities
    def _send(self, code, body, ctype, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        if self.secure:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        if ctype.startswith("application/json"):
            self.send_header("Cache-Control", "no-store")
        if self.path.split("?", 1)[0].startswith("/api/v1"):
            self.send_header("X-SMSGW-API-Version", "1")
        origin = self.headers.get("Origin", "")
        if origin and ("*" in CORS_ORIGINS or origin in CORS_ORIGINS):
            self.send_header("Access-Control-Allow-Origin", "*" if "*" in CORS_ORIGINS else origin)
            self.send_header("Vary", "Origin")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if not self._head:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # مرورگر ممکن است هنگام جابه‌جایی صفحه، long-poll را پیش از
                # رسیدن پاسخ ببندد؛ این یک قطع عادی کلاینت است، نه خطای سرویس.
                log.debug("client disconnected before response was written")

    def _json(self, code, obj, extra=()):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def log_message(self, fmt, *a):
        # query string می‌تواند شماره و متن جست‌وجو داشته باشد
        log.info("%s - %s", self.client_address[0],
                 re.sub(r"\?[^ \"]*", "", fmt % a))

    @property
    def query(self):
        """پارامترهای query string به‌صورت dict تک‌مقداری."""
        if self._q is None:
            self._q = {k: v[0] for k, v in
                       parse_qs(urlparse(self.path).query).items()}
        return self._q

    def qint(self, key, default=None, lo=None, hi=None):
        v = self.query.get(key)
        if v in (None, ""):
            return default
        try:
            n = int(v)
        except ValueError:
            raise HttpError(400, "پارامتر %s باید عدد باشد" % key)
        if lo is not None:
            n = max(lo, n)
        if hi is not None:
            n = min(hi, n)
        return n

    @property
    def ip(self):
        peer = self.client_address[0]
        if peer not in TRUSTED_PROXIES:
            return peer
        # پراکسی آدرس واقعی را به *انتهای* زنجیره اضافه می‌کند؛ ابتدای زنجیره
        # دست کلاینت است و با آن می‌شد rate-limit ورود را دور زد.
        for hop in reversed(self.headers.get("X-Forwarded-For", "").split(",")):
            hop = hop.strip()[:64]
            if hop and hop not in TRUSTED_PROXIES:
                return hop
        return peer

    @property
    def secure(self):
        return (self.client_address[0] in TRUSTED_PROXIES and
                self.headers.get("X-Forwarded-Proto", "").lower() == "https")

    def cookies(self, name):
        """تمام مقادیر یک cookie را برگردان، نه فقط آخرین مقدار.

        Chrome ممکن است بعد از تغییر دامنه/نسخه، یک cookie قدیمیِ Domain و
        یک cookie جدیدِ host-only را با یک نام بفرستد. ``SimpleCookie`` در
        این حالت یکی را silently دور می‌اندازد و اگر همان مقدار قدیمی باشد
        ورود موفق بلافاصله 401 می‌شود. هر candidate جداگانه parse و بعد در
        ``identify`` اعتبارسنجی می‌شود.
        """
        raw = self.headers.get("Cookie")
        if not raw:
            return []
        values = []
        for part in raw.split(";"):
            key, sep, _ = part.strip().partition("=")
            if not sep or key != name:
                continue
            try:
                c = SimpleCookie()
                c.load(part.strip())
                if name in c and c[name].value not in values:
                    values.append(c[name].value)
            except Exception:
                continue
        return values

    def cookie(self, name):
        values = self.cookies(name)
        return values[0] if values else ""

    def mkcookie(self, name, value, max_age, http_only=True):
        bits = ["%s=%s" % (name, value), "Path=/", "SameSite=Strict",
                "Max-Age=%d" % max_age]
        if http_only:
            bits.append("HttpOnly")
        if self.secure:
            bits.append("Secure")
        return ("Set-Cookie", "; ".join(bits))

    # -------------------------------------------------------------- identity
    def identify(self):
        """هویت درخواست را تشخیص می‌دهد. Bearer اولویت دارد تا مسیر ماشین
        هرگز با کوکی‌های مرورگر تداخل نکند."""
        h = self.headers.get("Authorization", "")
        if h.startswith("Bearer "):
            raw = h[7:].strip()
            # توکن قدیمی config.env همیشه معتبر است — آلارم‌های زیرساخت به آن
            # وابسته‌اند و نباید با هیچ تغییری در دیتابیس بشکند.
            if TOKEN and _same(raw, TOKEN):
                try:
                    db.ex("UPDATE api_tokens SET last_used_at=? WHERE name=?",
                          (db.now(), auth.LEGACY_TOKEN_NAME))
                except Exception:
                    pass
                return auth.Identity("token", auth.normalize_scopes("*"),
                                     label="token:" + auth.LEGACY_TOKEN_NAME)
            t = auth.lookup_api_token(raw)
            return auth.identity_from_token(t) if t else None
        # همه‌ی cookieهای هم‌نام را امتحان کن. این حالت مخصوصاً در Chrome
        # عادی بعد از باقی‌ماندن cookie قدیمی دیده می‌شود؛ Incognito چون
        # storage تازه دارد معمولاً آن را نشان نمی‌دهد.
        for raw in self.cookies(auth.SESSION_COOKIE):
            s, u = auth.lookup_session(raw)
            if s:
                return auth.identity_from_session(s, u)
        return None

    def require(self, perm, mutating):
        """هویت + مجوز + CSRF. در خطا خودش پاسخ می‌دهد و None برمی‌گرداند."""
        ident = self.identify()
        if ident is None:
            self._json(401, {"ok": False, "error": "unauthorized"})
            return None
        # CSRF فقط برای سشن کوکی‌محور معنا دارد؛ Bearer معاف است (بخش ۳.۴).
        if mutating and ident.kind == "session":
            sent = self.headers.get(auth.CSRF_HEADER, "")
            if not sent or not _same(sent, ident.session["csrf"]):
                self._json(403, {"ok": False, "error": "csrf token نامعتبر است"})
                return None
        if (ident.kind == "session" and ident.user["must_change_pw"] and
                self.path.split("?")[0] not in PW_CHANGE_OPEN):
            self._json(403, {"ok": False, "error": "ابتدا رمز عبور را عوض کن",
                             "code": "must_change_password"})
            return None
        if perm and not ident.can(perm):
            self._json(403, {"ok": False, "error": "دسترسی لازم را ندارید"})
            return None
        return ident

    def body_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HttpError(400, "invalid json")
        if n < 0:
            raise HttpError(400, "invalid json")
        if n > MAX_BODY:
            raise HttpError(413, "بدنه‌ی درخواست بیش از حد بزرگ است")
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            raise HttpError(400, "invalid json")
        if not isinstance(data, dict):
            raise HttpError(400, "invalid json")
        return data

    # ------------------------------------------------------------ static / ui
    def _static(self, p):
        rel = p[len("/static/"):]
        if not rel or ".." in rel.split("/"):
            return self._json(404, {"ok": False, "error": "not found"})
        fp = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not (fp == STATIC_DIR or fp.startswith(STATIC_DIR + os.sep)):
            return self._json(404, {"ok": False, "error": "not found"})
        ctype = STATIC_MIME.get(os.path.splitext(fp)[1].lower())
        if not ctype:
            return self._json(404, {"ok": False, "error": "not found"})
        try:
            b = open(fp, "rb").read()
        except OSError:
            return self._json(404, {"ok": False, "error": "not found"})
        self._send(200, b, ctype,
                   [("Cache-Control", "public, max-age=604800, immutable")])

    def _ui(self):
        try:
            b = open(os.path.join(BASE_DIR, "ui.html"), "rb").read()
        except OSError:
            return self._json(404, {"ok": False, "error": "ui missing"})
        self._send(200, b, "text/html; charset=utf-8",
                   [("Cache-Control", "no-cache")])

    # -------------------------------------------------------------- dispatch
    def _dispatch(self, table, mutating):
        p = self.path.split("?")[0].rstrip("/") or "/"
        if not mutating:
            if p in ("/sw.js", "/manifest.webmanifest", "/app-icon.svg"):
                rel = {"/sw.js": "sw.js", "/manifest.webmanifest": "manifest.webmanifest",
                       "/app-icon.svg": "app-icon.svg"}[p]
                fp = os.path.join(STATIC_DIR, rel)
                try:
                    data = open(fp, "rb").read()
                except OSError:
                    return self._json(404, {"ok": False, "error": "not found"})
                extra = [("Cache-Control", "no-cache")]
                if p == "/sw.js":
                    extra.append(("Service-Worker-Allowed", "/"))
                return self._send(200, data, STATIC_MIME[os.path.splitext(fp)[1]], extra)
            if p.startswith("/static/"):
                return self._static(p)
            if p in ("/", "/index.html"):
                return self._ui()
        entry = table.get(p)
        if entry is None:
            return self._json(404, {"ok": False, "error": "not found"})
        perm, fn = entry
        ident = None
        if perm is not PUBLIC:
            ident = self.require(perm, mutating)
            if ident is None:
                return
        try:
            body = self.body_json() if mutating else {}
            result = fn(self, ident, body)
        except HttpError as e:
            return self._json(e.code, {"ok": False, "error": e.msg})
        except store.Invalid as e:
            return self._json(400, {"ok": False, "error": str(e)})
        except auth.AuthError as e:
            return self._json(e.code, {"ok": False, "error": str(e)})
        except hilink.HiLinkError as e:
            log.error("hilink: %s", e)
            return self._json(502, {"ok": False, "error": str(e)})
        except Exception:
            log.exception("%s %s failed", self.command, p)
            return self._json(500, {"ok": False, "error": "خطای داخلی سرویس"})
        if result is None:
            return
        code, obj, extra = result if len(result) == 3 else (result[0], result[1], ())
        self._json(code, obj, extra)

    def do_GET(self):
        self._q = None
        self._dispatch(GET_ROUTES, False)

    def do_HEAD(self):
        self._head = True
        try:
            self.do_GET()
        finally:
            self._head = False

    def do_POST(self):
        self._q = None
        self._dispatch(POST_ROUTES, True)

    def do_OPTIONS(self):
        self._q = None
        p = self.path.split("?", 1)[0]
        origin = self.headers.get("Origin", "")
        if not p.startswith("/api/v1") or not origin or not (
                "*" in CORS_ORIGINS or origin in CORS_ORIGINS):
            return self._json(403, {"ok": False, "error": "cors origin مجاز نیست"})
        return self._send(204, b"", "text/plain", [
            ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Authorization, Content-Type, Idempotency-Key"),
            ("Access-Control-Max-Age", "86400")])


# --------------------------------------------------------------- سلامت و هویت
@get("/health", PUBLIC)
def r_health(h, i, b):
    # /health فقط «پروسه زنده است» نیست: اگر حلقه‌ی مودم مرده باشد صف و
    # دریافت پیامک ایستاده‌اند و مانیتورینگ باید خبردار شود.
    alive = sender.loop_alive()
    return (200 if alive else 503), {"ok": alive}


@get("/api/v1/health", PUBLIC)
def r_v1_health(h, i, b):
    return 200, {"ok": True, "data": {"service": "smsgw", "version": "1",
                                        "app_version": VERSION,
                                        "status": "healthy", "time": db.now()}}


@post("/api/login", PUBLIC)
def r_login(h, i, b):
    # فقط تلاش‌های *ناموفق* شمرده می‌شوند: این سقف برای کند کردن حمله‌ی
    # حدس رمز است، نه محدود کردن ورودهای موفقِ یک تیم پشت یک NAT.
    # دفاع اصلی، قفل ۱۵ دقیقه‌ای روی خود حساب است (auth.MAX_FAILED).
    fail_key = "loginfail:" + h.ip
    if RL.over(fail_key, 20, 300):
        raise HttpError(429, "تلاش‌های ناموفق زیاد — کمی صبر کن")
    username = str(b.get("username", ""))[:64]
    try:
        u = auth.authenticate(username, str(b.get("password", "")))
    except auth.AuthError:
        RL.hit(fail_key)
        audit(None, "login.failed", "user=%s" % username, h.ip)
        raise
    raw, csrf, exp = auth.create_session(
        u["id"], h.headers.get("User-Agent", ""), h.ip)
    audit(auth.identity_from_session({"csrf": csrf}, u), "login.ok", "", h.ip)
    return 200, {"ok": True, "user": _me(u), "csrf": csrf}, [
        h.mkcookie(auth.SESSION_COOKIE, raw, auth.SESSION_TTL),
        h.mkcookie(auth.CSRF_COOKIE, csrf, auth.SESSION_TTL, http_only=False)]


@post("/api/logout", None)
def r_logout(h, i, b):
    if i.kind == "session":
        # همان سشنی را حذف کن که identify واقعاً معتبر شناخته است؛ در حالت
        # cookie تکراری، اولین مقدار هدر لزوماً سشن فعال نیست.
        db.ex("DELETE FROM sessions WHERE id=?", (i.session["id"],))
        audit(i, "logout", "", h.ip)
    return 200, {"ok": True}, [
        h.mkcookie(auth.SESSION_COOKIE, "", 0),
        h.mkcookie(auth.CSRF_COOKIE, "", 0, http_only=False)]


def _me(u):
    return {"id": u["id"], "username": u["username"], "role": u["role"],
            "display_name": u["display_name"],
            "must_change_pw": bool(u["must_change_pw"]),
            "perms": sorted(auth.ROLE_PERMS.get(u["role"], ()))}


@get("/api/me", None)
def r_me(h, i, b):
    if i.kind == "token":
        return 200, {"ok": True, "kind": "token", "name": i.token["name"],
                     "perms": sorted(i.perms)}
    return 200, {"ok": True, "kind": "session", "user": _me(i.user), "version": VERSION,
                 "perms": sorted(i.perms), "csrf": i.session["csrf"]}


@post("/api/password", None)
def r_password(h, i, b):
    if i.kind != "session":
        raise HttpError(403, "تغییر رمز فقط از طریق ورود تعاملی ممکن است")
    if not auth.verify_password(str(b.get("current", "")), i.user["salt"],
                                i.user["password_hash"]):
        raise HttpError(403, "رمز فعلی اشتباه است")
    new = str(b.get("new", ""))
    if new == str(b.get("current", "")):
        raise HttpError(400, "رمز جدید باید با رمز فعلی فرق کند")
    auth.set_password(i.user["id"], new)
    # همه‌ی سشن‌های دیگر باطل شوند، سشن فعلی بماند
    auth.delete_user_sessions(i.user["id"], keep_id=i.session["id"])
    audit(i, "password.change", "", h.ip)
    return 200, {"ok": True}


# ------------------------------------------------- مدیریت (فقط سشن + admin)
def admin_only(i):
    """مدیریت کاربر و توکن فقط با ورود تعاملی. یک توکن ثابتِ داخل فایل نباید
    بتواند کاربر بسازد یا توکن جدید صادر کند."""
    if i.kind != "session":
        raise HttpError(403, "این عملیات فقط با ورود تعاملی ممکن است")
    if not i.can("admin"):
        raise HttpError(403, "دسترسی لازم را ندارید")


def _count_admins():
    return db.q1("SELECT COUNT(*) c FROM users WHERE role='admin' AND enabled=1")["c"]


def _user_row(u):
    return {"id": u["id"], "username": u["username"], "role": u["role"],
            "display_name": u["display_name"], "enabled": bool(u["enabled"]),
            "must_change_pw": bool(u["must_change_pw"]),
            "created_at": u["created_at"], "last_login_at": u["last_login_at"],
            "failed_attempts": u["failed_attempts"],
            "locked": bool(u["locked_until"] and u["locked_until"] > db.now())}


@get("/api/users", "admin")
def r_users(h, i, b):
    admin_only(i)
    return 200, {"ok": True, "users": [_user_row(u) for u in db.q(
        "SELECT * FROM users ORDER BY username")], "roles": list(auth.ROLES)}


@get("/api/audit-logs", "admin")
def r_audit_logs(h, i, b):
    """گزارش امنیتی/عملیاتی پنل؛ فقط مدیر تعاملی و بدون امکان حذف."""
    admin_only(i)
    limit = h.qint("limit", 100, 1, 500)
    offset = h.qint("offset", 0, 0, 1000000)
    action = str(h.query.get("action", "")).strip()[:64]
    search = str(h.query.get("q", "")).strip()[:100]
    since = h.qint("since", 0, 0)
    where, args = [], []
    if action:
        where.append("a.action=?")
        args.append(action)
    if search:
        where.append("(a.actor LIKE ? OR a.detail LIKE ? OR a.ip LIKE ?)")
        needle = "%" + search + "%"
        args.extend((needle, needle, needle))
    if since:
        where.append("a.created_at>=?")
        args.append(since)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.q1("SELECT COUNT(*) c FROM audit_log a" + clause, tuple(args))["c"]
    rows = db.q(
        "SELECT a.id,a.actor,a.action,a.detail,a.ip,a.created_at," 
        "u.display_name FROM audit_log a LEFT JOIN users u ON u.id=a.user_id" +
        clause + " ORDER BY a.id DESC LIMIT ? OFFSET ?",
        tuple(args + [limit, offset]))
    actions = [r["action"] for r in db.q(
        "SELECT DISTINCT action FROM audit_log ORDER BY action")]
    day = db.now() - 86400
    summary = db.q1(
        "SELECT COUNT(*) total," 
        "SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END) recent," 
        "SUM(CASE WHEN action='login.failed' AND created_at>=? THEN 1 ELSE 0 END) failed "
        "FROM audit_log", (day, day))
    data = [{"id": r["id"], "actor": r["actor"],
             "display_name": r["display_name"] or "", "action": r["action"],
             "detail": r["detail"], "ip": r["ip"],
             "created_at": r["created_at"]} for r in rows]
    return 200, {"ok": True, "logs": data, "actions": actions,
                 "meta": {"total": total, "limit": limit, "offset": offset,
                          "summary": {"total": summary["total"] or 0,
                                      "recent": summary["recent"] or 0,
                                      "failed": summary["failed"] or 0}}}


@post("/api/users", "admin")
def r_user_create(h, i, b):
    admin_only(i)
    uid = auth.create_user(str(b.get("username", "")), str(b.get("password", "")),
                           str(b.get("role", "viewer")),
                           str(b.get("display_name", ""))[:64],
                           must_change=bool(b.get("must_change_pw", True)))
    audit(i, "user.create", "user=%s role=%s" % (b.get("username"), b.get("role")), h.ip)
    return 200, {"ok": True, "id": uid}


def _target_user(b):
    u = db.q1("SELECT * FROM users WHERE id=?", (b.get("id"),))
    if not u:
        raise HttpError(404, "کاربر پیدا نشد")
    return u


@post("/api/users/update", "admin")
def r_user_update(h, i, b):
    admin_only(i)
    u = _target_user(b)
    role = str(b.get("role", u["role"]))
    enabled = 1 if b.get("enabled", u["enabled"]) else 0
    if role not in auth.ROLE_PERMS:
        raise HttpError(400, "نقش نامعتبر است")
    # نباید آخرین مدیر فعال را از دست بدهیم
    losing_admin = u["role"] == "admin" and u["enabled"] and (role != "admin" or not enabled)
    if losing_admin and _count_admins() <= 1:
        raise HttpError(400, "حداقل یک مدیر فعال باید باقی بماند")
    if u["id"] == i.user_id and not enabled:
        raise HttpError(400, "نمی‌توانی حساب خودت را غیرفعال کنی")
    db.ex("UPDATE users SET role=?,display_name=?,enabled=? WHERE id=?",
          (role, str(b.get("display_name", u["display_name"]))[:64], enabled, u["id"]))
    if not enabled or role != u["role"]:
        auth.delete_user_sessions(u["id"])       # تغییر سطح دسترسی = ابطال سشن‌ها
    audit(i, "user.update", "user=%s role=%s enabled=%d" % (u["username"], role, enabled), h.ip)
    return 200, {"ok": True}


@post("/api/users/password", "admin")
def r_user_password(h, i, b):
    admin_only(i)
    u = _target_user(b)
    auth.set_password(u["id"], str(b.get("new", "")), clear_must_change=False)
    auth.delete_user_sessions(u["id"])
    audit(i, "user.password_reset", "user=%s" % u["username"], h.ip)
    return 200, {"ok": True}


@post("/api/users/unlock", "admin")
def r_user_unlock(h, i, b):
    admin_only(i)
    u = _target_user(b)
    db.ex("UPDATE users SET failed_attempts=0,locked_until=NULL WHERE id=?", (u["id"],))
    audit(i, "user.unlock", "user=%s" % u["username"], h.ip)
    return 200, {"ok": True}


@post("/api/users/delete", "admin")
def r_user_delete(h, i, b):
    admin_only(i)
    u = _target_user(b)
    if u["id"] == i.user_id:
        raise HttpError(400, "نمی‌توانی حساب خودت را حذف کنی")
    if u["role"] == "admin" and u["enabled"] and _count_admins() <= 1:
        raise HttpError(400, "حداقل یک مدیر فعال باید باقی بماند")
    db.ex("DELETE FROM users WHERE id=?", (u["id"],))
    audit(i, "user.delete", "user=%s" % u["username"], h.ip)
    return 200, {"ok": True}


@get("/api/tokens", "admin")
def r_tokens(h, i, b):
    admin_only(i)
    rows = []
    for t in db.q("SELECT t.*, u.username AS by_user FROM api_tokens t "
                  "LEFT JOIN users u ON u.id=t.created_by ORDER BY t.name"):
        rows.append({"id": t["id"], "name": t["name"], "scopes": t["scopes"],
                     "enabled": bool(t["enabled"]), "created_at": t["created_at"],
                     "last_used_at": t["last_used_at"], "created_by": t["by_user"],
                     "protected": t["name"] == auth.LEGACY_TOKEN_NAME})
    return 200, {"ok": True, "tokens": rows,
                 "perms": [p for p in auth.PERMS if p not in auth.API_FORBIDDEN_PERMS]}


@post("/api/tokens", "admin")
def r_token_create(h, i, b):
    admin_only(i)
    name = str(b.get("name", ""))
    if name == auth.LEGACY_TOKEN_NAME:
        raise HttpError(400, "این نام رزرو شده است")
    tid, raw = auth.create_api_token(name, b.get("scopes", ""), i.user_id)
    audit(i, "token.create", "name=%s scopes=%s" % (name, b.get("scopes")), h.ip)
    # مقدار خام فقط همین یک‌بار برگردانده می‌شود؛ در دیتابیس فقط هش است.
    return 200, {"ok": True, "id": tid, "token": raw}


def _target_token(b):
    t = db.q1("SELECT * FROM api_tokens WHERE id=?", (b.get("id"),))
    if not t:
        raise HttpError(404, "توکن پیدا نشد")
    return t


@post("/api/tokens/update", "admin")
def r_token_update(h, i, b):
    admin_only(i)
    t = _target_token(b)
    if t["name"] == auth.LEGACY_TOKEN_NAME:
        raise HttpError(400, "توکن زبیکس محافظت‌شده است — آلارم‌های زیرساخت به آن وابسته‌اند")
    enabled = 1 if b.get("enabled") else 0
    scopes = ",".join(sorted(auth.normalize_scopes(b.get("scopes", t["scopes"]))))
    if not scopes:
        raise HttpError(400, "حداقل یک دسترسی معتبر لازم است")
    db.ex("UPDATE api_tokens SET enabled=?,scopes=? WHERE id=?", (enabled, scopes, t["id"]))
    audit(i, "token.update", "name=%s enabled=%d scopes=%s" % (t["name"], enabled, scopes), h.ip)
    return 200, {"ok": True}


@post("/api/tokens/delete", "admin")
def r_token_delete(h, i, b):
    admin_only(i)
    t = _target_token(b)
    if t["name"] == auth.LEGACY_TOKEN_NAME:
        raise HttpError(400, "توکن زبیکس حذف‌شدنی نیست — آلارم‌های زیرساخت به آن وابسته‌اند")
    db.ex("DELETE FROM api_tokens WHERE id=?", (t["id"],))
    audit(i, "token.delete", "name=%s" % t["name"], h.ip)
    return 200, {"ok": True}


# ------------------------------------------------ پاک‌سازی، خروجی و بسته‌ی نصب
def _download(h, data, name, ctype):
    h._send(200, data, ctype, [("Content-Disposition", 'attachment; filename="%s"' % name)])


@post("/api/data/wipe", "admin")
def r_data_wipe(h, i, b):
    """حذف انبوه برگشت‌ناپذیر است؛ توکن مجاز نیست و رمز دوباره پرسیده می‌شود."""
    admin_only(i)
    scope = str(b.get("scope", ""))
    if scope not in store.WIPE_SCOPES:
        raise HttpError(400, "نوع پاک‌سازی نامعتبر است")
    if not RL.allow("wipe:" + h.ip, 5, 300):
        raise HttpError(429, "تلاش زیاد — کمی صبر کن")
    if not auth.verify_password(str(b.get("password", "")), i.user["salt"],
                                i.user["password_hash"]):
        raise HttpError(403, "رمز عبور اشتباه است")
    result = store.wipe(scope, i.user_id)
    if scope in ("messages", "all"):
        sender.request_wipe()
    if scope == "all":
        for path in glob.glob(os.path.join(db.DATA_DIR, "modem-backup-*.json")):
            os.remove(path)
    # ردپای خودِ پاک‌سازی همیشه می‌ماند، حتی بعد از حذف گزارش‌ها
    audit(i, "data.wipe", "scope=%s count=%d" % (scope, sum(result.values())), h.ip)
    return 200, {"ok": True, "scope": scope, **result}


@get("/api/data/export", "admin")
def r_data_export(h, i, b):
    admin_only(i)
    audit(i, "data.export", "", h.ip)
    data = json.dumps(store.export_all(), ensure_ascii=False, indent=1).encode("utf-8")
    _download(h, data, "smsgw-export-%s.json" % time.strftime("%Y%m%d-%H%M"),
              "application/json; charset=utf-8")


@get("/api/package", "admin")
def r_package(h, i, b):
    """بسته‌ی نصب برای دستگاه دیگر؛ فقط فایل‌های allowlist، بدون داده و توکن."""
    admin_only(i)
    audit(i, "package.download", "version=%s" % VERSION, h.ip)
    _download(h, release.build(), "smsgw-%s.tar.gz" % VERSION, "application/gzip")


# ------------------------------------------------------------- مسیرهای مودم
@get("/status", "read")
def r_status(h, i, b):
    def _read():
        d = hilink.status(); d.update(hilink.signal()); d.update(hilink.sms_count())
        return d
    d = sender.modem_call(_read)
    storage = sender.storage_usage(d)
    return 200, {"ok": True,
                 "storage_used": storage["used"], "storage_max": storage["max"],
                 "storage_percent": storage["percent"],
                 "storage_full": storage["full"],
                 "connected": d.get("ConnectionStatus") == "901",
                 "signal_bars": int(d.get("SignalIcon") or 0),
                 "rssi": d.get("rssi", ""), "rscp": d.get("rscp", ""),
                 "network": d.get("CurrentNetworkType", ""),
                 "inbox": int(d.get("LocalInbox") or 0),
                 "unread": int(d.get("LocalUnread") or 0)}


@get("/inbox", "read")
def r_inbox(h, i, b):
    merged = _merge(sender.modem_call(hilink.inbox))
    try:
        store.mirror_incoming(merged)   # فرصت‌طلبانه؛ خطایش نباید نمایش را بشکند
    except Exception:
        log.exception("mirror on /inbox failed")
    return 200, {"ok": True, "messages": merged}


@get("/sent", "read")
def r_sent(h, i, b):
    return 200, {"ok": True,
                 "messages": _merge(sender.modem_call(hilink.inbox, box=2))}


@get("/api/conversations", "read")
def r_conversations(h, i, b):
    """فهرست یکپارچه‌ی مکالمات از آرشیو محلی (ورودی و خروجی)."""
    search = str(h.query.get("q", ""))[:100].strip()
    limit = h.qint("limit", 200, 1, 500)
    return 200, {"ok": True,
                 "conversations": store.conversations(limit, search),
                 "unread": store.unread_total()}


@get("/api/conversation", "read")
def r_conversation(h, i, b):
    phone = str(h.query.get("phone", ""))[:64].strip()
    if not phone:
        raise HttpError(400, "شماره‌ی مکالمه لازم است")
    limit = h.qint("limit", 300, 1, 1000)
    before = h.qint("before", None, 1, 2 ** 53)
    return 200, {"ok": True, **store.conversation(phone, limit, before)}


@post("/api/conversation/read", "read")
def r_conversation_read(h, i, b):
    phone = str(b.get("phone", ""))[:64].strip()
    if not phone:
        raise HttpError(400, "شماره‌ی مکالمه لازم است")
    return 200, {"ok": True, "changed": store.mark_read(phone)}


@post("/api/messages/delete", "delete")
def r_message_delete(h, i, b):
    result = store.soft_delete_message(b.get("id"), i.user_id)
    audit(i, "sms.delete", "id=%d direction=%s" %
          (result["id"], result["direction"]), h.ip)
    return 200, {"ok": True, "deleted": not result["already_deleted"],
                 "id": result["id"], "statistics_preserved": True}


@post("/api/conversation/delete", "delete")
def r_conversation_delete(h, i, b):
    result = store.soft_delete_conversation(b.get("phone"), i.user_id)
    audit(i, "conversation.delete", "phone=%s count=%d" %
          (result["phone"], result["deleted"]), h.ip)
    return 200, {"ok": True, **result, "statistics_preserved": True}


@get("/api/dashboard", "read")
def r_dashboard(h, i, b):
    stats = store.dashboard_stats()
    legacy = db.q1("SELECT last_used_at FROM api_tokens WHERE name=?",
                   (auth.LEGACY_TOKEN_NAME,))
    stats["zabbix_last_seen"] = legacy["last_used_at"] if legacy else None
    return 200, {"ok": True, "stats": stats}


@get("/api/report", "read")
def r_report(h, i, b):
    return 200, {"ok": True, "report": store.report(
        h.qint("days", 180, 1, 366))}


# --------------------------------------------------------- API عمومی نسخه ۱
@get("/api/v1", "read")
def r_v1_root(h, i, b):
    return 200, {"ok": True, "data": {
        "name": "SMS Gateway API", "version": "1.0.0",
        "documentation": "/api/v1/openapi.json",
        "authentication": "Authorization: Bearer <token>",
        "idempotency": "Idempotency-Key header on POST /api/v1/messages",
        "rate_limit": "60 queued messages/minute per identity"}}


@get("/api/v1/openapi.json", "read")
def r_v1_openapi(h, i, b):
    base = h.headers.get("Host", "localhost")
    scheme = "https" if h.secure else "http"
    return 200, {
        "openapi": "3.1.0",
        "info": {"title": "SMS Gateway API", "version": "1.0.0",
                 "description": "Versioned API for SMS, queue, modem and contacts"},
        "servers": [{"url": "%s://%s/api/v1" % (scheme, base)}],
        "components": {"securitySchemes": {"bearerAuth": {
            "type": "http", "scheme": "bearer", "bearerFormat": "API token"}}},
        "security": [{"bearerAuth": []}],
        "paths": {
            "/health": {"get": {"security": [], "summary": "Health check"}},
            "/status": {"get": {"summary": "Gateway and queue status"}},
            "/device": {"get": {"summary": "Full modem information"}},
            "/messages": {
                "get": {"summary": "List archived messages"},
                "post": {"summary": "Queue one or multiple SMS messages",
                         "parameters": [{"in": "header", "name": "Idempotency-Key",
                                         "schema": {"type": "string"}}]}},
            "/queue": {"get": {"summary": "List queue and statistics"}},
            "/queue/pause": {"post": {"summary": "Pause or resume the queue"}},
            "/queue/action": {"post": {"summary": "Retry or cancel a queue item"}},
            "/contacts": {"get": {"summary": "List contacts"}},
            "/calendar": {"get": {"summary": "Daily inbound/outbound statistics"}}
        }}


@get("/api/v1/status", "read")
def r_v1_status(h, i, b):
    _, status = r_status(h, i, b)
    status.pop("ok", None)
    return 200, {"ok": True, "data": {"modem": status,
                                        "queue": sender.queue_stats()},
                 "meta": {"time": db.now(), "api_version": "1"}}


@get("/api/v1/device", "read")
def r_v1_device(h, i, b):
    def collect():
        sections, errors = {}, {}
        for name, fn in (("device", hilink.device_info), ("basic", hilink.basic_info),
                         ("signal", hilink.signal), ("status", hilink.status),
                         ("sms", hilink.sms_count), ("traffic", hilink.traffic),
                         ("network", hilink.current_plmn)):
            try:
                sections[name] = fn()
            except Exception as e:
                errors[name] = str(e)
        return sections, errors
    sections, errors = sender.modem_call(collect)
    return 200, {"ok": True, "data": sections,
                 "meta": {"time": db.now(), "partial_errors": errors}}


@get("/api/v1/messages", "read")
def r_v1_messages(h, i, b):
    rows = store.list_messages(h.qint("limit", 100, 1, 500),
                               str(h.query.get("phone", ""))[:64],
                               str(h.query.get("direction", ""))[:8],
                               h.qint("since", 0, 0))
    return 200, {"ok": True, "data": rows,
                 "meta": {"count": len(rows), "time": db.now()}}


@post("/api/v1/messages", "send")
def r_v1_message_create(h, i, b):
    recipients = b.get("to", b.get("recipients"))
    if isinstance(recipients, str):
        recipients = [recipients]
    if not isinstance(recipients, list):
        raise HttpError(400, "to باید شماره یا آرایه‌ای از شماره‌ها باشد")
    if not RL.allow("v1send:" + (i.label or h.ip), 60, 60):
        raise HttpError(429, "rate limit exceeded")
    scheduled = b.get("scheduled_at")
    if scheduled not in (None, ""):
        try:
            scheduled = int(scheduled)
        except (TypeError, ValueError):
            raise HttpError(400, "scheduled_at باید Unix timestamp باشد")
        if not 0 <= scheduled < 2 ** 40:
            raise HttpError(400, "scheduled_at باید Unix timestamp باشد")
    key = h.headers.get("Idempotency-Key", "").strip()
    batch = ""
    if key:
        if len(key) < 8 or len(key) > 128:
            raise HttpError(400, "Idempotency-Key باید ۸ تا ۱۲۸ کاراکتر باشد")
        batch = "idem:%s:%s" % (i.label, key)
        old = db.q("SELECT id,message_id,phone FROM queue WHERE batch_id=? ORDER BY id",
                   (batch,))
        if old:
            return 200, {"ok": True, "data": [
                {"queue_id": r["id"], "message_id": r["message_id"], "to": r["phone"]}
                for r in old], "meta": {"idempotent_replay": True}}
    rows = sender.enqueue_many(recipients, b.get("text"), scheduled,
                               i.user_id, batch)
    audit(i, "sms.queue", "count=%d" % len(rows), h.ip)
    return 202, {"ok": True, "data": rows,
                 "meta": {"queued": len(rows),
                          "eta_seconds": sender.eta_seconds(len(rows)),
                          "idempotent_replay": False}}


@get("/api/v1/queue", "read")
def r_v1_queue(h, i, b):
    rows = sender.list_queue(h.qint("limit", 200, 1, 500),
                             str(h.query.get("status", ""))[:16])
    return 200, {"ok": True, "data": rows,
                 "meta": {"stats": sender.queue_stats(), "count": len(rows)}}


@post("/api/v1/queue/pause", "send")
def r_v1_queue_pause(h, i, b):
    paused = bool(b.get("paused"))
    sender.set_paused(paused)
    audit(i, "queue.pause" if paused else "queue.resume", "", h.ip)
    return 200, {"ok": True, "data": sender.queue_stats()}


@post("/api/v1/queue/action", "send")
def r_v1_queue_action(h, i, b):
    try:
        qid = int(b.get("id"))
    except (TypeError, ValueError):
        raise HttpError(400, "id صف نامعتبر است")
    action = str(b.get("action", ""))
    sender.queue_action(qid, action)
    audit(i, "queue.%s" % action, "id=%d" % qid, h.ip)
    return 200, {"ok": True, "data": {"id": qid, "action": action}}


@get("/api/v1/contacts", "read")
def r_v1_contacts(h, i, b):
    rows = store.list_contacts(str(h.query.get("q", ""))[:100])
    return 200, {"ok": True, "data": rows, "meta": {"count": len(rows)}}


@get("/api/v1/calendar", "read")
def r_v1_calendar(h, i, b):
    report = store.report(h.qint("days", 366, 1, 366))
    return 200, {"ok": True, "data": report["daily"],
                 "meta": {"days": report["days"], "sent": report["sent"],
                          "failed": report["failed"]}}


@get("/api/live", "read")
def r_live(h, i, b):
    """Long-poll سبک؛ UI بدون رفرش دستی تغییرات پیام را دریافت می‌کند."""
    cursor = h.qint("cursor", 0, 0, 2 ** 53)
    previous_state = str(h.query.get("state", ""))[:300]
    wait = h.qint("wait", 20, 0, 25)
    deadline = time.monotonic() + wait
    while True:
        state = store.live_state(cursor, previous_state)
        if state["changed"] or time.monotonic() >= deadline:
            return 200, {"ok": True, **state}
        time.sleep(1)


@get("/api/contacts", "read")
def r_contacts(h, i, b):
    search = str(h.query.get("q", ""))[:100].strip()
    return 200, {"ok": True, "contacts": store.list_contacts(search)}


@post("/api/contacts", "contacts")
def r_contact_save(h, i, b):
    cid = b.get("id")
    if cid not in (None, ""):
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            raise HttpError(400, "شناسه مخاطب نامعتبر است")
    cid = store.upsert_contact(cid, b.get("name"), b.get("phone"),
                               b.get("note", ""), b.get("group_ids", []))
    audit(i, "contact.save", "id=%s" % cid, h.ip)
    return 200, {"ok": True, "id": cid}


@post("/api/contacts/delete", "contacts")
def r_contact_delete(h, i, b):
    cid = store.delete_contact(b.get("id"))
    audit(i, "contact.delete", "id=%d" % cid, h.ip)
    return 200, {"ok": True}


@post("/api/contacts/import", "contacts")
def r_contacts_import(h, i, b):
    result = store.import_contacts(b.get("contacts"))
    audit(i, "contacts.import", "created=%d updated=%d" %
          (result["created"], result["updated"]), h.ip)
    return 200, {"ok": True, **result}


@post("/api/contacts/import-csv", "contacts")
def r_contacts_import_csv(h, i, b):
    parsed = store.parse_contacts_csv(b.get("csv"))
    result = store.import_contacts(parsed["contacts"])
    audit(i, "contacts.import", "created=%d updated=%d format=%s skipped=%d" %
          (result["created"], result["updated"], parsed["format"], parsed["skipped"]), h.ip)
    return 200, {"ok": True, **result, "format": parsed["format"],
                 "skipped": parsed["skipped"]}


@get("/info", "read")
def r_info(h, i, b):
    return 200, {"ok": True, "device": sender.modem_call(hilink.device_info)}


@post("/send", "send")
def r_send(h, i, b):
    if b.get("to") in (None, "") or b.get("text") in (None, ""):
        raise HttpError(400, "to/text required")
    to = store.clean_destination(b.get("to"))
    text = store.validate_text(b.get("text"))
    if not RL.allow("send:" + (i.label or h.ip), 20, 60):
        raise HttpError(429, "محدودیت ارسال — کمی صبر کن")
    # قبل از تماس با مودم ثبت می‌شود تا اگر سرویس وسط ارسال بمیرد، ردی بماند
    mid = store.record_outgoing(to, text, "sending", i.user_id)
    try:
        # از دروازه‌ی مشترک رد می‌شود تا با حلقه‌ی مودم هم‌پوشانی نکند
        r = sender.send_now(to, text)
    except Exception as e:
        store.update_message(mid, status="failed", error=str(e)[:200])
        raise
    ok = r == "OK"
    store.update_message(mid, status="sent" if ok else "failed",
                         sent_at=db.now(), error="" if ok else str(r)[:200])
    log.info("sent to=%s len=%d result=%s", to, len(text), r)
    audit(i, "sms.send", "to=%s len=%d" % (to, len(text)), h.ip)
    return 200, {"ok": ok, "result": r, "id": mid}


@post("/delete", "delete")
def r_delete(h, i, b):
    # سازگاری مسیر قدیمی: id محلی اولویت دارد، index مودم نیز قابل تبدیل است.
    mid = b.get("id")
    if mid in (None, "") and b.get("index") not in (None, ""):
        row = db.q1("SELECT id FROM messages WHERE modem_index=?", (b.get("index"),))
        if not row:
            raise HttpError(404, "پیام پیدا نشد")
        mid = row["id"]
    result = store.soft_delete_message(mid, i.user_id)
    audit(i, "sms.delete", "id=%d direction=%s" %
          (result["id"], result["direction"]), h.ip)
    return 200, {"ok": True, "deleted": not result["already_deleted"],
                 "id": result["id"], "statistics_preserved": True}


@post("/ussd", "ussd")
def r_ussd(h, i, b):
    code = str(b.get("code", "")).strip().translate(store.FA_DIGITS)
    if not code:
        raise HttpError(400, "code required")
    if not USSD_RE.match(code):
        raise HttpError(400, "کد USSD فقط می‌تواند عدد، حرف و * # + باشد")
    if not RL.allow("ussd:" + (i.label or h.ip), USSD_RATE, 60):
        raise HttpError(429, "محدودیت USSD — کمی صبر کن")
    # رمز کارت شارژ یا OTP نباید در لاگ و گزارش بماند
    masked = re.sub(r"\d{8,}", "****", code)
    log.info("ussd send code=%s", masked)
    audit(i, "ussd", "code=%s" % masked, h.ip)
    return 200, {"ok": True, "text": sender.modem_call(hilink.ussd, code)}


@post("/ussd/release", "ussd")
def r_ussd_release(h, i, b):
    """پایان جلسه‌ی چندمرحله‌ای USSD به درخواست کاربر."""
    return 200, {"ok": True, "released": sender.modem_call(hilink.ussd_release)}


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("SMSGW_TOKEN تنظیم نشده")
    ver, was = db.migrate()
    os.chmod(db.DB_PATH, 0o600)
    log.info("db schema v%d (was v%d) at %s", ver, was, db.DB_PATH)
    auth.bootstrap_admin()
    auth.ensure_legacy_token(TOKEN)
    # ⚠️ دقیقاً یک thread حق دارد با مودم حرف بزند — نگاه کن به sender.py
    threading.Thread(target=sender.modem_loop, daemon=True,
                     args=(poll_inbox_once, NTFY_POLL_INTERVAL),
                     kwargs={"maintenance": storage_tick}).start()
    threading.Thread(target=janitor, daemon=True).start()
    log.info("listening on 0.0.0.0:%d", PORT)
    Server(("0.0.0.0", PORT), H).serve_forever()
