#!/usr/bin/env python3
"""تست‌های خودکار smsgw — فقط stdlib، بدون وابستگی pip.

    python3 selftest.py            # همه
    python3 selftest.py -v         # پرگو
    python3 selftest.py Roles      # فقط یک کلاس

⚠️ قاعده‌ی طلایی: این سرویس به یک سیم‌کارت واقعی وصل است.
   هیچ تستی نباید `/send` را با بدنه‌ی معتبر صدا بزند. برای بررسی مجوزِ
   ارسال از بدنه‌ی خالی استفاده کن: مجاز → ۴۰۰، غیرمجاز → ۴۰۳.

الگوی منابع: هر چیزی که تست می‌سازد از طریق `Res` ساخته می‌شود تا در
teardown حتماً پاک شود — حتی اگر تست وسط کار بترکد. همه‌ی نام‌ها با
`TEST_PREFIX` شروع می‌شوند و در شروع و پایانِ کل اجرا جاروب می‌شوند، پس
یک اجرای نصفه‌کاره هم آشغال جا نمی‌گذارد.
"""
import ast, http.cookiejar, json, os, sys, unittest, urllib.error, urllib.parse, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import db, auth, store, sender

BASE = "http://127.0.0.1:8080"
TEST_PREFIX = "zz_test_"
TEST_PW = "test-password-9271"
# شماره‌های تست همه با این پیشوند شروع می‌شوند تا جاروب بتواند فقط ردیف‌های
# تست را از جدول messages پاک کند و به پیامک‌های واقعی دست نزند.
TEST_PHONE_PREFIX = "0999000"


# ------------------------------------------------------------ کلاینت HTTP
class Client:
    """کلاینت کوکی‌دار سبک روی urllib."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""

    def req(self, method, path, body=None, bearer=None, csrf=True, headers=None):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(BASE + path, data=data, method=method)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        if bearer:
            r.add_header("Authorization", "Bearer " + bearer)
        if csrf and self.csrf and method != "GET":
            r.add_header(auth.CSRF_HEADER, self.csrf)
        for key, value in (headers or {}).items():
            r.add_header(key, value)
        try:
            with self.opener.open(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except ValueError:
                return e.code, {"raw": raw[:200].decode("utf-8", "replace")}

    def get(self, p, **kw):
        return self.req("GET", p, **kw)

    def post(self, p, body=None, **kw):
        return self.req("POST", p, body if body is not None else {}, **kw)

    def login(self, username, password):
        st, j = self.post("/api/login", {"username": username, "password": password})
        if st == 200:
            self.csrf = j.get("csrf", "")
        return st, j


# --------------------------------------------------------- مدیریت منابع تست
def sweep():
    """هر چیزی با پیشوند تست را پاک می‌کند. هم در شروع، هم در پایان."""
    like = TEST_PREFIX + "%"
    with db.tx() as c:
        c.execute("DELETE FROM users WHERE username LIKE ?", (like,))
        c.execute("DELETE FROM api_tokens WHERE name LIKE ?", (like,))
        c.execute("DELETE FROM contacts WHERE name LIKE ? OR note LIKE ?", (like, like))
        c.execute("DELETE FROM contact_groups WHERE name LIKE ?", (like,))
        c.execute("DELETE FROM templates WHERE name LIKE ?", (like,))
        c.execute("DELETE FROM messages WHERE phone LIKE ?", (TEST_PHONE_PREFIX + "%",))
        c.execute("DELETE FROM queue WHERE phone LIKE ?", (TEST_PHONE_PREFIX + "%",))
        c.execute("DELETE FROM audit_log WHERE actor LIKE ? OR detail LIKE ?",
                  (like, "%" + TEST_PREFIX + "%"))


def assert_clean():
    """گارد نهایی: هیچ ردپای تستی نباید بماند."""
    like = TEST_PREFIX + "%"
    leftovers = []
    for tbl, col, pat in (("users", "username", like), ("api_tokens", "name", like),
                          ("contacts", "name", like), ("contact_groups", "name", like),
                          ("templates", "name", like),
                          ("audit_log", "actor", like),
                          ("messages", "phone", TEST_PHONE_PREFIX + "%"),
                          ("queue", "phone", TEST_PHONE_PREFIX + "%")):
        n = db.q1("SELECT COUNT(*) c FROM %s WHERE %s LIKE ?" % (tbl, col),
                  (pat,))["c"]
        if n:
            leftovers.append("%s=%d" % (tbl, n))
    if leftovers:
        raise AssertionError("پاک‌سازی ناقص: " + ", ".join(leftovers))


class Res:
    """منابع تست را می‌سازد و در `close()` حتماً پاک می‌کند.

    استفاده در هر TestCase:
        def setUp(self):
            self.res = Res(); self.addCleanup(self.res.close)
    """

    def __init__(self):
        self._undo = []

    def close(self):
        # به ترتیب معکوس، و خطای یک پاک‌سازی مانع بقیه نشود
        errs = []
        while self._undo:
            fn = self._undo.pop()
            try:
                fn()
            except Exception as e:
                errs.append(repr(e))
        if errs:
            raise AssertionError("خطا در teardown: " + "; ".join(errs))

    def user(self, suffix, role="viewer", password=TEST_PW, must_change=False):
        name = TEST_PREFIX + suffix
        db.ex("DELETE FROM users WHERE username=?", (name,))
        uid = auth.create_user(name, password, role, name, must_change=must_change)
        self._undo.append(lambda: db.ex("DELETE FROM users WHERE id=?", (uid,)))
        return {"id": uid, "username": name, "password": password, "role": role}

    def token(self, suffix, scopes="read"):
        name = TEST_PREFIX + suffix
        db.ex("DELETE FROM api_tokens WHERE name=?", (name,))
        tid, raw = auth.create_api_token(name, scopes)
        self._undo.append(lambda: db.ex("DELETE FROM api_tokens WHERE id=?", (tid,)))
        return {"id": tid, "name": name, "raw": raw}

    def phone(self, n=1):
        """شماره‌ی تست یکتا — جاروب بر اساس پیشوندش پاکش می‌کند."""
        return "%s%03d" % (TEST_PHONE_PREFIX, n)

    def message(self, direction, phone, content, status="received", modem_index=None):
        mid = db.insert(
            "INSERT INTO messages(direction,phone,content,status,modem_index,"
            "created_at) VALUES(?,?,?,?,?,?)",
            (direction, phone, content, status, modem_index, db.now()))
        self._undo.append(lambda: db.ex("DELETE FROM messages WHERE id=?", (mid,)))
        return mid

    def client_for(self, user):
        c = Client()
        st, j = c.login(user["username"], user["password"])
        assert st == 200, "ورود تست ناموفق: %s" % j
        return c


class Base(unittest.TestCase):
    def setUp(self):
        self.res = Res()
        self.addCleanup(self.res.close)


# ------------------------------------------------------------------ تست‌ها
class Health(Base):
    def test_health_is_public(self):
        self.assertEqual(Client().get("/health"), (200, {"ok": True}))

    def test_protected_without_auth(self):
        st, _ = Client().get("/status")
        self.assertEqual(st, 401)

    def test_pwa_assets_are_public(self):
        for path, marker in (("/manifest.webmanifest", b'"standalone"'),
                             ("/sw.js", b"notificationclick"),
                             ("/app-icon.svg", b"<svg"),
                             ("/static/icon-192.png", b"\x89PNG"),
                             ("/static/icon-512.png", b"\x89PNG")):
            with urllib.request.urlopen(BASE + path, timeout=10) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(marker, response.read())


class Login(Base):
    def test_login_and_me(self):
        u = self.res.user("login", "operator")
        c = self.res.client_for(u)
        st, j = c.get("/api/me")
        self.assertEqual(st, 200)
        self.assertEqual(j["user"]["username"], u["username"])
        self.assertEqual(j["user"]["role"], "operator")

    def test_wrong_password(self):
        u = self.res.user("badpw")
        st, j = Client().login(u["username"], "definitely-wrong")
        self.assertEqual(st, 401)
        self.assertNotIn(u["password"], json.dumps(j))

    def test_lockout_after_five(self):
        u = self.res.user("lock")
        c = Client()
        codes = [c.login(u["username"], "nope")[0] for _ in range(5)]
        self.assertEqual(codes[-1], 429, "باید بعد از ۵ تلاش قفل شود")
        st, _ = c.login(u["username"], u["password"])
        self.assertEqual(st, 429, "رمز درست هم در حالت قفل نباید کار کند")

    def test_disabled_user_cannot_login(self):
        u = self.res.user("disabled")
        db.ex("UPDATE users SET enabled=0 WHERE id=?", (u["id"],))
        st, _ = Client().login(u["username"], u["password"])
        self.assertEqual(st, 403)

    def test_logout_kills_session(self):
        u = self.res.user("logout")
        c = self.res.client_for(u)
        self.assertEqual(c.post("/api/logout")[0], 200)
        self.assertEqual(c.get("/api/me")[0], 401)

    def test_duplicate_chrome_session_cookie_uses_valid_candidate(self):
        """Domain/host-only هم‌نام نباید ورود موفق را فوراً 401 کند."""
        u = self.res.user("dupecookie")
        raw, _, _ = auth.create_session(u["id"], "Chrome", "127.0.0.1")
        for value in ("expired-cookie; smsgw_sess=" + raw,
                      raw + "; smsgw_sess=expired-cookie"):
            st, j = Client().get("/api/me", headers={
                "Cookie": "smsgw_sess=" + value})
            self.assertEqual(st, 200, value)
            self.assertEqual(j["user"]["username"], u["username"])


class Csrf(Base):
    def test_post_without_csrf_rejected(self):
        u = self.res.user("csrf1")
        c = self.res.client_for(u)
        st, j = c.post("/api/logout", csrf=False)
        self.assertEqual(st, 403)
        self.assertIn("csrf", json.dumps(j, ensure_ascii=False).lower())

    def test_post_with_wrong_csrf_rejected(self):
        u = self.res.user("csrf2")
        c = self.res.client_for(u)
        c.csrf = "bogus-value"
        self.assertEqual(c.post("/api/logout")[0], 403)

    def test_bearer_is_exempt_from_csrf(self):
        t = self.res.token("csrfexempt", "send")
        # بدنه‌ی خالی: اگر CSRF مانع نشود، اعتبارسنجی ۴۰۰ می‌دهد نه ۴۰۳
        st, _ = Client().post("/send", {}, bearer=t["raw"])
        self.assertEqual(st, 400)


class Roles(Base):
    """مجوزها باید سمت سرور اعمال شوند — نه فقط با پنهان‌کردن دکمه."""

    def _send_perm(self, client):
        # بدنه‌ی خالی عمداً: هرگز پیامک واقعی نمی‌فرستد
        return client.post("/send", {})[0]

    def test_viewer_cannot_send_or_ussd(self):
        c = self.res.client_for(self.res.user("viewer", "viewer"))
        self.assertEqual(self._send_perm(c), 403)
        self.assertEqual(c.post("/ussd", {})[0], 403)
        self.assertEqual(c.get("/status")[0], 200)

    def test_operator_can_send_cannot_admin(self):
        c = self.res.client_for(self.res.user("oper", "operator"))
        self.assertEqual(self._send_perm(c), 400, "اپراتور باید از گارد مجوز رد شود")
        self.assertEqual(c.get("/api/users")[0], 403)
        self.assertEqual(c.post("/api/tokens", {"name": "x", "scopes": "read"})[0], 403)

    def test_admin_reaches_admin_routes(self):
        c = self.res.client_for(self.res.user("adm", "admin"))
        self.assertEqual(c.get("/api/users")[0], 200)
        self.assertEqual(c.get("/api/tokens")[0], 200)


class Tokens(Base):
    def test_scopes_are_enforced(self):
        t = self.res.token("scoped", "read")
        cl = Client()
        self.assertEqual(cl.get("/status", bearer=t["raw"])[0], 200)
        self.assertEqual(cl.post("/ussd", {}, bearer=t["raw"])[0], 403)

    def test_admin_scope_never_granted_to_token(self):
        with self.assertRaises(auth.AuthError):
            auth.create_api_token(TEST_PREFIX + "adminscope", "admin")

    def test_token_cannot_reach_admin_routes(self):
        t = self.res.token("noadmin", "read,send,delete,ussd,contacts,templates")
        self.assertEqual(Client().get("/api/users", bearer=t["raw"])[0], 403)

    def test_disabled_token_rejected(self):
        t = self.res.token("disabled", "read")
        db.ex("UPDATE api_tokens SET enabled=0 WHERE id=?", (t["id"],))
        self.assertEqual(Client().get("/status", bearer=t["raw"])[0], 401)

    def test_raw_token_is_not_stored(self):
        t = self.res.token("nostore", "read")
        row = db.q1("SELECT * FROM api_tokens WHERE id=?", (t["id"],))
        self.assertNotIn(t["raw"], " ".join(str(v) for v in tuple(row)))


class LegacyZabbix(Base):
    """آلارم‌های زیرساخت به این مسیر وابسته‌اند — نباید هرگز بشکند."""

    def setUp(self):
        super().setUp()
        # config.env فقط برای root قابل‌خواندن است؛ تست با کاربر سرویس اجرا
        # می‌شود، پس توکن از متغیر محیطی می‌آید .
        self.tok = os.environ.get("SMSGW_TOKEN", "")
        if not self.tok:
            with open(os.path.join(HERE, "config.env")) as f:
                for line in f:
                    if line.startswith("SMSGW_TOKEN="):
                        self.tok = line.split("=", 1)[1].strip()
        self.assertTrue(self.tok, "SMSGW_TOKEN در config.env نیست")

    def test_env_token_still_works(self):
        st, j = Client().get("/status", bearer=self.tok)
        self.assertEqual(st, 200)
        self.assertIn("connected", j)

    def test_legacy_row_exists_and_is_protected(self):
        c = self.res.client_for(self.res.user("legadm", "admin"))
        st, j = c.get("/api/tokens")
        self.assertEqual(st, 200)
        row = [t for t in j["tokens"] if t["name"] == auth.LEGACY_TOKEN_NAME]
        self.assertEqual(len(row), 1, "ردیف legacy-zabbix باید وجود داشته باشد")
        self.assertTrue(row[0]["protected"])
        tid = row[0]["id"]
        self.assertEqual(c.post("/api/tokens/delete", {"id": tid})[0], 400)
        self.assertEqual(c.post("/api/tokens/update", {"id": tid, "enabled": 0})[0], 400)
        self.assertTrue(db.q1("SELECT enabled FROM api_tokens WHERE id=?",
                              (tid,))["enabled"], "legacy نباید غیرفعال شده باشد")


class AdminGuards(Base):
    def test_cannot_delete_or_disable_self(self):
        u = self.res.user("selfadm", "admin")
        c = self.res.client_for(u)
        self.assertEqual(c.post("/api/users/delete", {"id": u["id"]})[0], 400)
        st, _ = c.post("/api/users/update",
                       {"id": u["id"], "role": "admin", "enabled": 0})
        self.assertEqual(st, 400)

    def test_role_change_invalidates_sessions(self):
        victim = self.res.user("demote", "operator")
        vc = self.res.client_for(victim)
        self.assertEqual(vc.get("/api/me")[0], 200)
        admin = self.res.client_for(self.res.user("promoter", "admin"))
        st, _ = admin.post("/api/users/update",
                           {"id": victim["id"], "role": "viewer", "enabled": 1})
        self.assertEqual(st, 200)
        self.assertEqual(vc.get("/api/me")[0], 401, "تغییر نقش باید سشن را باطل کند")

    def test_password_policy(self):
        c = self.res.client_for(self.res.user("pwpolicy", "admin"))
        st, _ = c.post("/api/users", {"username": TEST_PREFIX + "weak",
                                      "password": "short", "role": "viewer"})
        self.assertEqual(st, 400)

    def test_secrets_never_in_response(self):
        u = self.res.user("nosecret", "admin")
        c = self.res.client_for(u)
        blob = json.dumps(c.get("/api/users")[1]) + json.dumps(c.get("/api/tokens")[1])
        for bad in ("password_hash", "token_hash", "salt", TEST_PW):
            self.assertNotIn(bad, blob)


class Mirroring(Base):
    """گام ۴ — پیام‌ها باید در دیتابیس آینه شوند و تکراری نسازند."""

    def test_mirror_is_idempotent(self):
        p = self.res.phone(1)
        merged = [{"phone": p, "date": "2026-07-29 19:37:43",
                   "content": "متن تست", "indexes": [999001, 999002]}]
        first = store.mirror_incoming(merged)
        again = store.mirror_incoming(merged) + store.mirror_incoming(merged)
        self.assertEqual((first, again), (1, 0), "poll مکرر نباید تکراری بسازد")
        rows = db.q("SELECT * FROM messages WHERE phone=?", (p,))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["modem_index"], 999001, "باید کوچک‌ترین ایندکس بماند")
        db.ex("DELETE FROM messages WHERE phone=?", (p,))

    def test_outgoing_has_null_modem_index(self):
        # ثابت معماری: کلید یکتا (direction, modem_index) در هر دو جهت
        # می‌ماند؛ NULLها در sqlite با هم برخورد نمی‌کنند پس چند خروجی
        # هم‌زمان مشکلی ندارد.
        p = self.res.phone(2)
        a = store.record_outgoing(p, "یک", "sent")
        b = store.record_outgoing(p, "دو", "sent")
        self.assertNotEqual(a, b)
        for mid in (a, b):
            self.assertIsNone(db.q1("SELECT modem_index FROM messages WHERE id=?",
                                    (mid,))["modem_index"])
        db.ex("DELETE FROM messages WHERE phone=?", (p,))

    def test_persian_digits_normalized(self):
        p = self.res.phone(3)
        fa = p.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
        mid = store.record_outgoing(fa, "متن", "sent")
        self.assertEqual(db.q1("SELECT phone FROM messages WHERE id=?", (mid,))["phone"], p)
        db.ex("DELETE FROM messages WHERE id=?", (mid,))

    def test_conversation_grouping_and_read(self):
        p = self.res.phone(4)
        self.res.message("in", p, "سلام", modem_index=999101)
        self.res.message("in", p, "خبری نشد؟", modem_index=999102)
        self.res.message("out", p, "پاسخ", status="sent")
        conv = [c for c in store.conversations() if c["phone"] == p]
        self.assertEqual(len(conv), 1, "همه‌ی پیام‌های یک شماره باید یک گفتگو باشند")
        self.assertEqual(conv[0]["total"], 3)
        self.assertEqual(conv[0]["unread"], 2, "فقط ورودی‌ها خوانده‌نشده‌اند")
        self.assertEqual(store.mark_read(p), 2)
        self.assertEqual([c for c in store.conversations()
                          if c["phone"] == p][0]["unread"], 0)
        msgs = store.conversation(p)["messages"]
        self.assertEqual([m["direction"] for m in msgs], ["in", "in", "out"])
        self.assertEqual([m["error"] for m in msgs], ["", "", ""],
                         "نشانه‌ی read نباید به‌عنوان خطا بیرون بیاید")

    def test_invalid_destination_rejected(self):
        t = self.res.token("badphone", "send")
        st, j = Client().post("/send", {"to": "abcd", "text": "x"}, bearer=t["raw"])
        self.assertEqual(st, 400)
        self.assertIn("نامعتبر", j["error"])

    def test_overlong_text_rejected(self):
        t = self.res.token("longtext", "send")
        st, _ = Client().post("/send", {"to": self.res.phone(5), "text": "x" * 2000},
                              bearer=t["raw"])
        self.assertEqual(st, 400)


class UiApi(Base):
    """قرارداد endpointهای پنل یکپارچه و گارد حذف‌ناپذیری تاریخچه."""

    def setUp(self):
        super().setUp()
        self.client = self.res.client_for(self.res.user("ui", "admin"))
        self.phone = self.res.phone(80)
        self.res.message("in", self.phone, "ورودی پنل", modem_index=999880)
        self.res.message("out", self.phone, "خروجی پنل", status="sent")

    def test_conversation_endpoints_merge_directions(self):
        st, j = self.client.get("/api/conversations")
        self.assertEqual(st, 200)
        self.assertEqual(len([x for x in j["conversations"]
                              if x["phone"] == self.phone]), 1)
        st, j = self.client.get("/api/conversation?phone=" + self.phone)
        self.assertEqual(st, 200)
        self.assertEqual([m["direction"] for m in j["messages"]], ["in", "out"])
        self.assertEqual(self.client.post("/api/conversation/read",
                                          {"phone": self.phone})[0], 200)

    def test_dashboard_and_calendar_report(self):
        st, j = self.client.get("/api/dashboard")
        self.assertEqual(st, 200)
        self.assertGreaterEqual(j["stats"]["total_in"], 1)
        st, j = self.client.get("/api/report?days=180")
        self.assertEqual(st, 200)
        self.assertEqual(j["report"]["days"], 180)
        self.assertTrue(j["report"]["daily"])

    def test_admin_audit_log_is_filtered_and_read_only(self):
        db.ex("INSERT INTO audit_log(user_id,actor,action,detail,ip,created_at) "
              "VALUES(NULL,?,?,?,?,?)",
              (TEST_PREFIX + "audit", "login.failed", "user=" + TEST_PREFIX + "audit",
               "127.0.0.1", db.now()))
        st, j = self.client.get("/api/audit-logs?action=login.failed&q=" + TEST_PREFIX)
        self.assertEqual(st, 200)
        self.assertTrue(any(x["actor"] == TEST_PREFIX + "audit" for x in j["logs"]))
        self.assertIn("summary", j["meta"])
        self.assertEqual(self.client.post("/api/audit-logs", {"id": 1})[0], 404,
                         "گزارش فعالیت نباید endpoint حذف/ویرایش داشته باشد")

    def test_soft_delete_hides_message_but_preserves_statistics(self):
        row = db.q1("SELECT id FROM messages WHERE modem_index=999880")
        before = store.dashboard_stats()
        before_report = store.report(7)
        st, j = self.client.post("/api/messages/delete", {"id": row["id"]})
        self.assertEqual(st, 200)
        self.assertTrue(j["statistics_preserved"])
        self.assertTrue(db.q1("SELECT deleted_at FROM messages WHERE id=?",
                              (row["id"],))["deleted_at"])
        self.assertNotIn(row["id"], [m["id"] for m in
                                     store.conversation(self.phone)["messages"]])
        after = store.dashboard_stats()
        after_report = store.report(7)
        self.assertEqual((before["total_in"], before["total_out"]),
                         (after["total_in"], after["total_out"]),
                         "حذف از مکالمه نباید آمار تاریخی را کم کند")
        self.assertEqual(before_report["daily"], after_report["daily"],
                         "تقویم و نمودار نیز نباید با حذف پیام تغییر کنند")

    def test_delete_whole_conversation_preserves_statistics(self):
        before = store.dashboard_stats()
        before_report = store.report(7)
        st, j = self.client.post("/api/conversation/delete", {"phone": self.phone})
        self.assertEqual(st, 200)
        self.assertEqual(j["deleted"], 2)
        self.assertTrue(j["statistics_preserved"])
        self.assertEqual(store.conversation(self.phone)["messages"], [])
        after = store.dashboard_stats()
        self.assertEqual((before["total_in"], before["total_out"]),
                         (after["total_in"], after["total_out"]))
        self.assertEqual(before_report["daily"], store.report(7)["daily"])

    def test_contacts_add_search_and_atomic_import(self):
        st, j = self.client.post("/api/contacts", {
            "name": TEST_PREFIX + "contact", "phone": self.res.phone(81),
            "note": "دفترچه تلفن"})
        self.assertEqual(st, 200)
        st, j = self.client.get("/api/contacts?q=" + TEST_PREFIX)
        self.assertEqual(st, 200)
        self.assertTrue(any(c["phone"] == self.res.phone(81) for c in j["contacts"]))
        st, j = self.client.post("/api/contacts/import", {"contacts": [
            {"name": TEST_PREFIX + "contact updated", "phone": self.res.phone(81)},
            {"name": TEST_PREFIX + "contact new", "phone": self.res.phone(82)}]})
        self.assertEqual(st, 200)
        self.assertEqual((j["created"], j["updated"]), (1, 1))

    def test_google_contacts_csv_uses_named_phone_columns(self):
        # شماره‌های موبایل تستی ۱۱ رقمی تا نرمال‌سازی +98 هم واقعاً آزموده شود.
        phone_1 = TEST_PHONE_PREFIX + "0083"
        phone_2 = TEST_PHONE_PREFIX + "0084"
        google_phone_1 = "+98 %s %s %s" % (phone_1[1:4], phone_1[4:7], phone_1[7:])
        google_phone_2 = "+98 %s %s %s" % (phone_2[1:4], phone_2[4:7], phone_2[7:])
        csv_text = ("\ufeffName,Given Name,Additional Name,Family Name,Middle Name,"
                    "Phone 1 - Type,Phone 1 - Value,Phone 2 - Type,Phone 2 - Value,Notes\n"
                    "%s Google,Test,Middle,Person,Middle Name,Mobile,\"%s ::: %s\",Home,,\"خانه, تست\"\n"
                    "%s No Phone,Empty,,,Middle Name,,,,,skip\n" %
                    (TEST_PREFIX, google_phone_1, google_phone_2, TEST_PREFIX))
        parsed = store.parse_contacts_csv(csv_text)
        self.assertEqual(parsed["format"], "google")
        self.assertEqual([x["phone"] for x in parsed["contacts"]],
                         [google_phone_1, google_phone_2])
        self.assertEqual(parsed["skipped"], 1)
        self.assertNotIn("Middle Name", [x["phone"] for x in parsed["contacts"]])
        st, j = self.client.post("/api/contacts/import-csv", {"csv": csv_text})
        self.assertEqual(st, 200)
        self.assertEqual((j["created"], j["updated"], j["skipped"]), (2, 0, 1))
        rows = store.list_contacts(TEST_PREFIX + " Google")
        self.assertEqual({x["phone"] for x in rows},
                         {phone_1, phone_2})

    def test_live_endpoint_returns_cursor_and_incoming(self):
        st, j = self.client.get("/api/live?cursor=0&wait=0")
        self.assertEqual(st, 200)
        self.assertTrue(j["changed"])
        self.assertGreater(j["cursor"], 0)
        self.assertIn("unread", j)
        st, quiet = self.client.get("/api/live?cursor=%d&state=%s&wait=0" %
                                    (j["cursor"], urllib.parse.quote(j["state"])))
        self.assertEqual(st, 200)
        self.assertFalse(quiet["changed"],
                         "timeout بدون رخداد نباید رابط را دوباره رندر کند")

    def test_versioned_api_read_contract(self):
        for path in ("/api/v1/status", "/api/v1/messages?limit=2",
                     "/api/v1/queue?limit=2", "/api/v1/contacts",
                     "/api/v1/calendar?days=7"):
            st, j = self.client.get(path)
            self.assertEqual(st, 200, path)
            self.assertTrue(j["ok"], path)
            self.assertIn("data", j, path)
        st, j = Client().get("/api/v1/health")
        self.assertEqual(st, 200)
        self.assertEqual(j["data"]["version"], "1")

    def test_v1_send_is_queued_and_idempotent(self):
        was_paused = sender.is_paused()
        sender.set_paused(True)       # سیم‌کارت واقعی: worker نباید تست را بفرستد
        def safe_restore():
            # حتی اگر assertion وسط تست شکست، قبل از resume هیچ کار واقعی نماند.
            db.ex("DELETE FROM queue WHERE phone LIKE ?", (TEST_PHONE_PREFIX + "%",))
            db.ex("DELETE FROM messages WHERE phone LIKE ?", (TEST_PHONE_PREFIX + "%",))
            sender.set_paused(was_paused)
        self.addCleanup(safe_restore)
        body = {"to": self.res.phone(83), "text": "queued api test"}
        idem = {"Idempotency-Key": "zz-test-idempotency-83"}
        st, first = self.client.post("/api/v1/messages", body, headers=idem)
        self.assertEqual(st, 202)
        qid = first["data"][0]["queue_id"]
        st, replay = self.client.post("/api/v1/messages", body, headers=idem)
        self.assertEqual(st, 200)
        self.assertTrue(replay["meta"]["idempotent_replay"])
        self.assertEqual(replay["data"][0]["queue_id"], qid)
        self.assertEqual(db.q1("SELECT COUNT(*) c FROM queue WHERE batch_id LIKE ?",
                               ("%zz-test-idempotency-83",))["c"], 1)
        row = db.q1("SELECT status FROM queue WHERE id=?", (qid,))
        self.assertEqual(row["status"], "queued")
        st, _ = self.client.post("/api/v1/queue/action",
                                 {"id": qid, "action": "cancel"})
        self.assertEqual(st, 200)
        self.assertEqual(db.q1("SELECT status FROM queue WHERE id=?", (qid,))["status"],
                         "canceled")

    def test_v1_bulk_limit_and_invalid_number(self):
        st, _ = self.client.post("/api/v1/messages",
                                 {"to": [self.res.phone(84)] * 51, "text": "x"})
        self.assertEqual(st, 400)
        st, _ = self.client.post("/api/v1/messages", {"to": "bad", "text": "x"})
        self.assertEqual(st, 400)


class SinglePhoneEntryPoint(Base):
    """`clean_destination` تنها دروازه‌ی شماره‌ی مقصد است.

    `normalize_phone` عمداً ورودی نامعتبر را دست‌نخورده برمی‌گرداند (چون
    فرستنده‌ی پیامِ ورودی می‌تواند «HAMRAHAVAL» باشد). اگر یک مسیر ارسال
    اشتباهی آن را صدا بزند، شماره‌ی خراب بی‌صدا وارد صف می‌شود. این کلاس
    جلوی آن را می‌گیرد — هم ساختاری، هم رفتاری.
    """

    #: فقط store.py حق دارد normalize_phone را مستقیم صدا بزند
    OWNER = "store.py"

    def _project_sources(self):
        d = os.path.dirname(os.path.abspath(__file__))
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".py") and fn not in (self.OWNER, os.path.basename(__file__)):
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    yield fn, f.read()

    def test_no_module_calls_normalize_directly(self):
        offenders = []
        for fn, src in self._project_sources():
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.Attribute) and node.attr == "normalize_phone":
                    offenders.append("%s:%d" % (fn, node.lineno))
                if isinstance(node, ast.ImportFrom) and node.module == "store":
                    for a in node.names:
                        if a.name == "normalize_phone":
                            offenders.append("%s:%d (import)" % (fn, node.lineno))
        self.assertEqual(offenders, [], "این نقاط باید clean_destination را صدا بزنند: "
                                        + ", ".join(offenders))

    def test_write_boundary_validates(self):
        """حتی اگر صداکننده فراموش کند، مرز نوشتن باید رد کند."""
        for bad in ("abcd", "", "  ", "12", "*555#"):
            with self.assertRaises(store.Invalid, msg="پذیرفته شد: %r" % bad):
                store.record_outgoing(bad, "متن", "sent")
        self.assertFalse(db.q("SELECT 1 FROM messages WHERE content='متن' "
                              "AND direction='out' AND created_at>=?", (db.now() - 5,)),
                         "هیچ ردیفی نباید نوشته شده باشد")

    def test_contact_write_boundary_validates(self):
        with self.assertRaises(store.Invalid):
            store.upsert_contact(None, TEST_PREFIX + "bad", "abcd", "", [])

    def test_mirror_still_accepts_alphanumeric_sender(self):
        """ورودی نباید قربانی سخت‌گیری خروجی شود."""
        n = store.mirror_incoming([{"phone": "HAMRAHAVAL", "date": "2026-07-29 19:37:43",
                                    "content": "تست", "indexes": [999501]}])
        self.assertEqual(n, 1)
        row = db.q1("SELECT phone FROM messages WHERE modem_index=999501")
        self.assertEqual(row["phone"], "HAMRAHAVAL")
        db.ex("DELETE FROM messages WHERE modem_index=999501")

    def test_clean_destination_round_trip(self):
        for raw, want in (("\u06f0\u06f9\u06f1\u06f2\u06f0\u06f0\u06f0\u06f0\u06f0\u06f0\u06f0", "09120000000"),
                          ("+98 912 000-0000", "09120000000"),
                          ("00989120000000", "09120000000"),
                          ("10005", "10005")):
            self.assertEqual(store.clean_destination(raw), want)

    def test_old_name_is_gone(self):
        """اگر کسی validate_destination را برگرداند، دو دروازه می‌شود."""
        self.assertFalse(hasattr(store, "validate_destination"),
                         "نام قدیمی نباید برگردد — یک نقطه‌ی ورود بیشتر نباید باشد")


def main():
    sweep()                      # بقایای اجرای قبلی
    try:
        argv = [sys.argv[0]] + sys.argv[1:]
        result = unittest.main(module=__name__, argv=argv, exit=False,
                               verbosity=2).result
    finally:
        sweep()                  # همیشه، حتی اگر تست‌ها بترکند
        assert_clean()
        print("\n✓ پاک‌سازی کامل — هیچ ردیف تستی باقی نماند")
        db.close()
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
