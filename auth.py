#!/usr/bin/env python3
"""احراز هویت، سشن، نقش‌ها و توکن‌های API — فقط stdlib.

دو مسیر موازی برای احراز هویت وجود دارد و هر دو باید سالم بمانند:
  • انسان در مرورگر → نام کاربری/رمز → کوکی سشن (+ توکن CSRF)
  • ماشین (Zabbix/اسکریپت) → هدر ``Authorization: Bearer <TOKEN>``
"""
import hashlib, hmac, logging, re, secrets
import db

log = logging.getLogger("smsgw.auth")

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN = 16384, 8, 1, 64
SCRYPT_MAXMEM = 64 * 1024 * 1024

MIN_PW_LEN = 8
MAX_FAILED = 5
LOCK_SECONDS = 15 * 60
SESSION_TTL = 7 * 24 * 3600

SESSION_COOKIE = "smsgw_sess"
CSRF_COOKIE = "smsgw_csrf"
CSRF_HEADER = "X-CSRF-Token"

# ------------------------------------------------------------------ دسترسی‌ها
# نقش‌ها و مجوزها. بررسی همیشه سمت سرور انجام می‌شود (بخش ۱۰ مشخصات).
PERMS = ("read", "send", "delete", "ussd", "contacts", "templates", "admin")
ROLE_PERMS = {
    "viewer":   {"read"},
    "operator": {"read", "send", "delete", "ussd", "contacts", "templates"},
    "admin":    set(PERMS),
}
ROLES = ("admin", "operator", "viewer")

# مجوزهایی که هرگز به توکن API داده نمی‌شوند. مدیریت کاربر/توکن فقط با سشنِ
# تعاملی انجام می‌شود تا یک توکن ثابتِ داخل فایل نتواند کاربر بسازد.
API_FORBIDDEN_PERMS = {"admin"}

LEGACY_TOKEN_NAME = "legacy-zabbix"

USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,32}$")


class AuthError(Exception):
    """خطای قابل نمایش به کاربر (پیام فارسی)."""

    def __init__(self, msg, code=401):
        super().__init__(msg)
        self.code = code


# ------------------------------------------------------------------- رمز عبور
def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                       r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN,
                       maxmem=SCRYPT_MAXMEM)
    return h, salt


def verify_password(password, salt, expected):
    h, _ = hash_password(password, salt)
    return hmac.compare_digest(h, expected)


def check_password_policy(pw):
    if not isinstance(pw, str) or len(pw) < MIN_PW_LEN:
        raise AuthError("رمز عبور باید حداقل %d کاراکتر باشد" % MIN_PW_LEN, 400)
    if len(pw) > 256:
        raise AuthError("رمز عبور بیش از حد بلند است", 400)


def _token_hash(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# -------------------------------------------------------------------- کاربران
def create_user(username, password, role, display_name="", must_change=False):
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise AuthError("نام کاربری نامعتبر است (۳ تا ۳۲ کاراکتر: حروف، عدد، . _ -)", 400)
    if role not in ROLE_PERMS:
        raise AuthError("نقش نامعتبر است", 400)
    check_password_policy(password)
    h, salt = hash_password(password)
    try:
        return db.insert(
            "INSERT INTO users(username,password_hash,salt,role,display_name,"
            "enabled,must_change_pw,created_at) VALUES(?,?,?,?,?,1,?,?)",
            (username, h, salt, role, (display_name or username)[:64],
             1 if must_change else 0, db.now()))
    except Exception as e:
        if "UNIQUE" in str(e):
            raise AuthError("این نام کاربری قبلاً وجود دارد", 409)
        raise


def set_password(user_id, password, clear_must_change=True):
    check_password_policy(password)
    h, salt = hash_password(password)
    db.ex("UPDATE users SET password_hash=?,salt=?,failed_attempts=0,"
          "locked_until=NULL,must_change_pw=? WHERE id=?",
          (h, salt, 0 if clear_must_change else 1, user_id))


def authenticate(username, password):
    """در موفقیت ردیف کاربر را برمی‌گرداند، وگرنه AuthError.

    پیام خطا عمداً مبهم است تا وجود/نبودِ نام کاربری لو نرود — به‌جز حالت
    قفل‌شدن که کاربر باید بداند چرا نمی‌تواند وارد شود.
    """
    u = db.q1("SELECT * FROM users WHERE username=?", ((username or "").strip(),))
    generic = AuthError("نام کاربری یا رمز عبور اشتباه است")
    if not u:
        # کار بیهوده برای هم‌زمان‌سازی پاسخ و جلوگیری از user enumeration
        hash_password(password or "", b"\x00" * 16)
        raise generic
    if u["locked_until"] and u["locked_until"] > db.now():
        left = (u["locked_until"] - db.now() + 59) // 60
        raise AuthError("حساب موقتاً قفل است — %d دقیقه دیگر تلاش کن" % left, 429)
    good = verify_password(password or "", u["salt"], u["password_hash"])
    if not u["enabled"]:
        # فقط صاحب رمز درست می‌فهمد حساب غیرفعال است؛ بقیه پاسخ عادی می‌گیرند
        if good:
            raise AuthError("این حساب غیرفعال است", 403)
        raise generic
    if not good:
        # بعد از پایان قفل، شمارش از صفر شروع می‌شود
        n = (0 if u["locked_until"] else u["failed_attempts"]) + 1
        lock = db.now() + LOCK_SECONDS if n >= MAX_FAILED else None
        db.ex("UPDATE users SET failed_attempts=?,locked_until=? WHERE id=?",
              (n, lock, u["id"]))
        if lock:
            raise AuthError("پس از %d تلاش ناموفق، حساب %d دقیقه قفل شد"
                            % (MAX_FAILED, LOCK_SECONDS // 60), 429)
        raise generic
    db.ex("UPDATE users SET failed_attempts=0,locked_until=NULL,last_login_at=? "
          "WHERE id=?", (db.now(), u["id"]))
    return db.q1("SELECT * FROM users WHERE id=?", (u["id"],))


# ---------------------------------------------------------------------- سشن‌ها
def create_session(user_id, user_agent="", ip=""):
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    exp = db.now() + SESSION_TTL
    db.ex("INSERT INTO sessions(user_id,token_hash,csrf,created_at,expires_at,"
          "user_agent,ip) VALUES(?,?,?,?,?,?,?)",
          (user_id, _token_hash(raw), csrf, db.now(), exp,
           (user_agent or "")[:200], (ip or "")[:64]))
    return raw, csrf, exp


def lookup_session(raw):
    """سشن معتبر → (session_row, user_row) وگرنه (None, None). عمر سشن با هر
    فعالیت تمدید می‌شود (sliding expiry)."""
    if not raw:
        return None, None
    s = db.q1("SELECT * FROM sessions WHERE token_hash=?", (_token_hash(raw),))
    if not s:
        return None, None
    if s["expires_at"] <= db.now():
        db.ex("DELETE FROM sessions WHERE id=?", (s["id"],))
        return None, None
    u = db.q1("SELECT * FROM users WHERE id=? AND enabled=1", (s["user_id"],))
    if not u:
        db.ex("DELETE FROM sessions WHERE id=?", (s["id"],))
        return None, None
    # تمدید حداکثر هر ۵ دقیقه یک‌بار تا نوشتن بی‌مورد در db نداشته باشیم
    new_exp = db.now() + SESSION_TTL
    if new_exp - s["expires_at"] > 300:
        db.ex("UPDATE sessions SET expires_at=? WHERE id=?", (new_exp, s["id"]))
    return s, u


def delete_session(raw):
    if raw:
        db.ex("DELETE FROM sessions WHERE token_hash=?", (_token_hash(raw),))


def delete_user_sessions(user_id, keep_id=None):
    if keep_id:
        db.ex("DELETE FROM sessions WHERE user_id=? AND id<>?", (user_id, keep_id))
    else:
        db.ex("DELETE FROM sessions WHERE user_id=?", (user_id,))


def purge_expired_sessions():
    return db.ex("DELETE FROM sessions WHERE expires_at<=?", (db.now(),)).rowcount


# ---------------------------------------------------------------- توکن‌های API
def normalize_scopes(scopes):
    """رشته/لیست اسکوپ → مجموعه‌ی مجوزهای معتبر. `*` یعنی همه‌چیز به‌جز
    مجوزهای مدیریتی (API_FORBIDDEN_PERMS)."""
    if isinstance(scopes, str):
        scopes = [s.strip() for s in scopes.replace(",", " ").split()]
    out = set()
    for s in scopes or []:
        if s == "*":
            out |= set(PERMS)
        elif s in PERMS:
            out.add(s)
    return out - API_FORBIDDEN_PERMS


def create_api_token(name, scopes, created_by=None):
    name = (name or "").strip()
    if not name or len(name) > 64:
        raise AuthError("نام توکن نامعتبر است", 400)
    perms = normalize_scopes(scopes)
    if not perms:
        raise AuthError("حداقل یک دسترسی معتبر لازم است", 400)
    raw = "sgw_" + secrets.token_urlsafe(32)
    try:
        tid = db.insert(
            "INSERT INTO api_tokens(name,token_hash,scopes,enabled,created_at,"
            "created_by) VALUES(?,?,?,1,?,?)",
            (name, _token_hash(raw), ",".join(sorted(perms)), db.now(), created_by))
    except Exception as e:
        if "UNIQUE" in str(e):
            raise AuthError("توکنی با این نام وجود دارد", 409)
        raise
    return tid, raw


def lookup_api_token(raw):
    if not raw:
        return None
    t = db.q1("SELECT * FROM api_tokens WHERE token_hash=? AND enabled=1",
              (_token_hash(raw),))
    if t:
        db.ex("UPDATE api_tokens SET last_used_at=? WHERE id=?", (db.now(), t["id"]))
    return t


def ensure_legacy_token(raw_token):
    """توکن موجود در config.env را به جدول api_tokens مهاجرت می‌دهد تا در UI
    دیده شود و last_used ثبت شود. این ردیف هرگز حذف نمی‌شود — آلارم‌های
    زیرساخت به آن وابسته‌اند (بخش ۳.۱ مشخصات)."""
    if not raw_token:
        return
    th = _token_hash(raw_token)
    scopes = ",".join(sorted(normalize_scopes("*")))
    row = db.q1("SELECT * FROM api_tokens WHERE name=?", (LEGACY_TOKEN_NAME,))
    if row is None:
        db.ex("INSERT INTO api_tokens(name,token_hash,scopes,enabled,created_at) "
              "VALUES(?,?,?,1,?)", (LEGACY_TOKEN_NAME, th, scopes, db.now()))
        log.info("legacy token migrated into api_tokens as %r", LEGACY_TOKEN_NAME)
    elif row["token_hash"] != th:
        # توکن در config.env عوض شده — ردیف را همگام کن
        db.ex("UPDATE api_tokens SET token_hash=?,scopes=?,enabled=1 WHERE id=?",
              (th, scopes, row["id"]))
        log.info("legacy token hash refreshed from config.env")


# ------------------------------------------------------------ راه‌اندازی اولیه
def bootstrap_admin():
    """اگر هیچ کاربری نیست، یک admin با رمز تصادفی بساز و رمز را یک‌بار در
    لاگ systemd چاپ کن. هیچ رمز پیش‌فرض ثابتی وجود ندارد."""
    if db.q1("SELECT 1 FROM users LIMIT 1"):
        return None
    pw = secrets.token_urlsafe(15)
    create_user("admin", pw, "admin", "مدیر سیستم", must_change=True)
    log.warning("=" * 64)
    log.warning("کاربر اولیه ساخته شد → نام کاربری: admin   رمز: %s", pw)
    log.warning("این رمز فقط همین یک‌بار چاپ می‌شود. در اولین ورود عوضش کن.")
    log.warning("=" * 64)
    return pw


# ---------------------------------------------------------------- مدل «هویت»
class Identity:
    """نتیجه‌ی احراز هویت یک درخواست — چه با کوکی، چه با Bearer."""

    __slots__ = ("kind", "user", "session", "token", "perms", "label")

    def __init__(self, kind, perms, user=None, session=None, token=None, label=""):
        self.kind = kind            # "session" | "token"
        self.perms = perms
        self.user = user
        self.session = session
        self.token = token
        self.label = label

    @property
    def user_id(self):
        return self.user["id"] if self.user else None

    @property
    def role(self):
        return self.user["role"] if self.user else "token"

    def can(self, perm):
        return perm in self.perms


def identity_from_token(row):
    return Identity("token", normalize_scopes(row["scopes"]), token=row,
                    label="token:" + row["name"])


def identity_from_session(s, u):
    return Identity("session", set(ROLE_PERMS.get(u["role"], ())), user=u,
                    session=s, label=u["username"])
