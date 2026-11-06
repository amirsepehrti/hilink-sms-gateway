#!/usr/bin/env python3
"""تست رگرسیون آفلاین: نمونه‌ی جدا + مودم ساختگی. اجرا: python3 tests/offline_test.py"""
import glob, json, os, socket, sqlite3, sys, time, unittest
from harness import Instance, PORT, wait


class Case(unittest.TestCase):
    capacity = 500

    def setUp(self):
        self.i = Instance(capacity=self.capacity)
        self.m = self.i.modem
        self.addCleanup(self.i.close)

    def db(self):
        c = sqlite3.connect(os.path.join(self.i.dir, "smsgw.db"), timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def row_in(self, idx):
        return self.db().execute("select * from messages where direction='in' and modem_index=?",
                                 (idx,)).fetchone()


class Archive(Case):
    def test_newest_messages_shown_past_300(self):
        c = self.db()
        for n in range(350):
            c.execute("insert into messages(direction,phone,content,status,created_at) "
                      "values('out','09120000001',?,'sent',?)", ("m%d" % n, 1000 + n))
        c.commit()
        _, j, _ = self.i.req("GET", "/api/conversation?phone=09120000001&limit=300")
        self.assertEqual(j["messages"][-1]["content"], "m349")
        self.assertEqual(j["messages"][0]["content"], "m50")
        self.assertTrue(j["has_more"])
        _, k, _ = self.i.req("GET", "/api/conversation?phone=09120000001&limit=300&before=%d"
                             % j["messages"][0]["id"])
        self.assertEqual([m["content"] for m in k["messages"]][-1], "m49")
        self.assertEqual(len(k["messages"]), 50)
        self.assertFalse(k["has_more"])


class Multipart(Case):
    def bodies(self):
        return [p["message"] for p in self.i.ntfy.posts if p["message"] != "seed"]

    def test_late_parts_update_row_and_ntfy_waits(self):
        self.m.add(1, "09121111111", "seed")          # baseline ntfy
        wait(lambda: self.row_in(40000))
        time.sleep(1.2)
        idx = self.m.add(1, "09122222222", "part1", "2026-11-06 10:00:00")
        wait(lambda: self.row_in(idx))
        self.assertEqual(self.bodies(), [], "پیام نیمه‌کاره نباید فوروارد شود")
        for m in self.m.boxes[1]:
            if m["Index"] == idx:
                m["Content"] = "part1 part2 part3"
        wait(lambda: self.row_in(idx)["content"] == "part1 part2 part3")
        self.assertEqual(self.row_in(idx)["content"], "part1 part2 part3")
        wait(self.bodies)
        self.assertEqual(self.bodies(), ["part1 part2 part3"])
        n = self.db().execute("select count(*) from messages where direction='in'").fetchone()[0]
        self.assertEqual(n, 2)

    def test_ntfy_outage_retries_instead_of_losing(self):
        self.m.add(1, "09121111111", "seed")
        wait(lambda: self.row_in(40000))
        time.sleep(1.2)
        self.i.ntfy.fail = True
        self.m.add(1, "09122222222", "important")
        time.sleep(3.5)
        self.i.ntfy.fail = False
        wait(self.bodies)
        self.assertEqual(self.bodies(), ["important"])

    def test_recycled_index_is_not_dropped(self):
        idx = self.m.add(1, "09121111111", "first", "2026-11-01 10:00:00")
        wait(lambda: self.row_in(idx))
        self.m.boxes[1].clear()
        self.m.add(1, "09123333333", "second", "2026-11-06 10:00:00", index=idx)
        wait(lambda: self.row_in(idx)["content"] == "second")
        self.assertEqual(self.row_in(idx)["phone"], "09123333333")
        r = self.db().execute("select modem_index from messages where content='first'").fetchone()
        self.assertIsNone(r[0], "رکورد قدیمی باید حفظ شود")


class Storage(Case):
    def test_status_reports_capacity(self):
        self.m.fill(inbox=10, outbox=20, draft=1)
        _, j, _ = self.i.req("GET", "/status")
        self.assertEqual((j["storage_used"], j["storage_max"], j["storage_full"]), (31, 500, False))
        self.assertTrue(j["connected"])

    def test_auto_clean_starts_at_80_percent(self):
        self.m.fill(outbox=399, draft=2)
        wait(lambda: self.m.used() <= 300, 25)
        self.assertEqual(self.m.used(), 300)
        self.assertEqual(len(self.m.boxes[3]), 2, "پیش‌نویس نباید خودکار حذف شود")
        left = sorted(m["Index"] for m in self.m.boxes[2])
        self.assertGreaterEqual(left[0], 40000 + 100, "قدیمی‌ترین‌ها باید حذف شوند")

    def test_below_threshold_is_untouched(self):
        self.m.fill(outbox=399)
        time.sleep(3)
        self.assertEqual(self.m.used(), 399)

    def test_inbox_only_cleaned_when_mirrored(self):
        self.m.fill(inbox=420)      # فقط ۵۰ تای جدید در poll آینه می‌شوند
        wait(lambda: self.m.used() < 420, 8)
        kept = {m["Index"] for m in self.m.boxes[1]}
        c = self.db()
        for idx in set(range(40000, 40420)) - kept:
            self.assertIsNotNone(c.execute("select 1 from messages where modem_index=?",
                                           (idx,)).fetchone(), "حذف بدون آرشیو: %d" % idx)

    def test_full_modem_alerts_once(self):
        self.m.fill(inbox=500)
        wait(lambda: self.i.ntfy.posts, 10)
        time.sleep(2.5)
        alerts = [p for p in self.i.ntfy.posts if "حافظه" in p["title"]]
        self.assertEqual(len(alerts), 1)


class Purge(Case):
    def set_purge(self, v):
        c = self.db()
        c.execute("insert into settings(key,value) values('modem_purge',?) "
                  "on conflict(key) do update set value=excluded.value", (v,))
        c.commit()

    def backups(self):
        return glob.glob(os.path.join(self.i.dir, "modem-backup-*.json"))

    def test_single_then_all_with_verified_backup(self):
        self.m.fill(inbox=120, outbox=70, draft=2)
        self.set_purge("1")
        wait(lambda: self.m.used() == 191, 10)
        self.assertEqual(self.m.used(), 191)
        self.assertEqual(len(self.m.boxes[3]), 1, "اول پیش‌نویس")
        time.sleep(2)
        self.assertEqual(self.m.used(), 191, "بیش از مقدار خواسته‌شده حذف نشود")
        b = self.backups()
        self.assertEqual(len(b), 1)
        d = json.load(open(b[0], encoding="utf-8"))
        self.assertEqual([len(d["boxes"][k]) for k in "123"], [120, 70, 2])
        self.assertEqual(os.stat(b[0]).st_mode & 0o777, 0o600)
        self.set_purge("all")
        wait(lambda: self.m.used() == 0, 30)
        self.assertEqual(self.m.used(), 0)
        self.assertEqual(len(self.backups()), 2)

    def test_purge_does_not_refire_old_messages_to_ntfy(self):
        for n in range(120):
            self.m.add(1, "09124444444", "old %d" % n, "2026-11-01 10:%02d:%02d" % (n // 60, n % 60))
        wait(lambda: self.row_in(40119), 10)
        time.sleep(2.5)
        before = len(self.i.ntfy.posts)
        self.set_purge("all")
        wait(lambda: self.m.used() == 0, 30)
        time.sleep(2.5)
        self.assertNotIn("index restarted", self.i.logs())
        self.assertEqual(len(self.i.ntfy.posts), before)

    def test_truncated_row_is_repaired_before_delete(self):
        idx = self.m.add(1, "09122222222", "short", "2026-11-06 10:00:00")
        self.m.fill(outbox=1)
        wait(lambda: self.row_in(idx))
        for m in self.m.boxes[1]:
            m["Content"] = "short and now complete"
        self.set_purge("all")
        wait(lambda: self.m.used() == 0, 15)
        self.assertEqual(self.row_in(idx)["content"], "short and now complete")


class Wipe(Case):
    def session(self):
        import auth_helper, urllib.request
        c = self.db()
        auth_helper.add_user(c, "boss", "password-123", role="admin")
        st, j, h = self.i.req("POST", "/api/login", {"username": "boss", "password": "password-123"})
        ck = "; ".join(v.split(";")[0] for v in h.get_all("Set-Cookie"))
        self.hdr = {"Cookie": ck, "X-CSRF-Token": j["csrf"]}

    def wipe(self, scope, password="password-123"):
        import urllib.request, urllib.error
        r = urllib.request.Request("http://127.0.0.1:%d/api/data/wipe" % PORT, method="POST",
                                   data=json.dumps({"scope": scope, "password": password}).encode(),
                                   headers=dict(self.hdr, **{"Content-Type": "application/json"}))
        try:
            with urllib.request.urlopen(r, timeout=30) as x:
                return x.status, json.load(x)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def seed(self):
        c = self.db()
        c.execute("insert into messages(direction,phone,content,status,created_at) "
                  "values('out','09120000001','secret text','sent',?)", (int(time.time()),))
        c.execute("insert into messages(direction,phone,content,status,created_at) "
                  "values('in','09120000001','secret in','received',?)", (int(time.time()),))
        c.execute("insert into messages(direction,phone,content,status,created_at) "
                  "values('out','09120000002','waiting','queued',?)", (int(time.time()),))
        c.execute("insert into audit_log(action,detail,created_at) values('sms.send','x',1)")
        c.commit()

    def stats(self):
        return self.i.req("GET", "/api/dashboard")[1]["stats"]

    def test_token_cannot_wipe_and_password_required(self):
        self.session()
        self.seed()
        self.assertEqual(self.i.req("POST", "/api/data/wipe",
                                    {"scope": "all", "password": "x"})[0], 403)
        self.assertEqual(self.wipe("all", "wrong")[0], 403)
        self.assertEqual(self.stats()["total_out"], 2)

    def test_messages_scope_keeps_numbers(self):
        self.session()
        self.seed()
        self.m.add(1, "09125555555", "on modem", "2026-11-06 10:00:00")
        wait(lambda: self.row_in(40000))
        before = self.stats()
        self.assertEqual(self.wipe("messages")[0], 200)
        after = self.stats()
        self.assertEqual((after["total_in"], after["total_out"]),
                         (before["total_in"], before["total_out"]))
        wait(lambda: self.m.used() == 0, 10)
        time.sleep(2.5)
        c = self.db()
        self.assertEqual(c.execute("select count(*) from messages where content like '%secret%' "
                                   "or content='on modem'").fetchone()[0], 0)
        self.assertEqual(c.execute("select content from messages where status='queued'"
                                   ).fetchone()[0], "waiting")
        self.assertEqual(self.stats()["total_in"], before["total_in"], "نباید دوباره آینه شود")
        self.assertEqual(self.i.req("GET", "/api/conversations")[1]["conversations"][0]["phone"],
                         "09120000002")

    def test_logs_scope_only_touches_logs(self):
        self.session()
        self.seed()
        before = self.stats()["total_out"]
        self.assertEqual(self.wipe("logs")[0], 200)
        c = self.db()
        self.assertEqual([r[0] for r in c.execute("select action from audit_log")], ["data.wipe"])
        self.assertEqual(self.stats()["total_out"], before)
        self.assertEqual(c.execute("select content from messages where status='sent'"
                                   ).fetchone()[0], "secret text")

    def test_all_scope_clears_everything_but_users_and_contacts(self):
        self.session()
        self.seed()
        c = self.db()
        c.execute("insert into contacts(name,phone,created_at,updated_at) values('a','0912',1,1)")
        c.commit()
        self.m.add(1, "09125555555", "on modem", "2026-11-06 10:00:00")
        wait(lambda: self.row_in(40000))
        open(os.path.join(self.i.dir, "modem-backup-x.json"), "w").close()
        self.assertEqual(self.wipe("all")[0], 200)
        wait(lambda: self.m.used() == 0, 10)
        time.sleep(2.5)
        c = self.db()
        self.assertEqual(c.execute("select count(*) from messages").fetchone()[0], 0)
        self.assertEqual(self.stats()["total_out"], 0)
        self.assertEqual(c.execute("select count(*) from contacts").fetchone()[0], 1)
        self.assertEqual(c.execute("select count(*) from users").fetchone()[0], 2)
        self.assertEqual(glob.glob(os.path.join(self.i.dir, "modem-backup-*")), [])
        idx = self.m.add(1, "09126666666", "after wipe", "2026-11-06 11:00:00")
        wait(lambda: self.row_in(idx))
        self.assertIsNotNone(self.row_in(idx), "دریافت باید بعد از پاک‌سازی ادامه یابد")


class Packaging(Case):
    def test_package_has_no_secrets_and_runs_from_other_dir(self):
        import io, subprocess, sys, tarfile, tempfile, urllib.request
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import release
        d = tempfile.mkdtemp(prefix="smsgw-pack-")
        self.addCleanup(subprocess.run, ["rm", "-rf", d])
        with tarfile.open(fileobj=io.BytesIO(release.build())) as t:
            names = t.getnames()
            t.extractall(d, filter="data")
        for n in names:
            for bad in ("config.env", "/data/", ".db", "ntfy_state", "modem-backup"):
                self.assertNotIn(bad, n)
        root = os.path.join(d, "smsgw-" + release.version())
        out = subprocess.run(["sh", os.path.join(root, "install.sh"), "--prefix", d + "/app",
                              "--no-systemd", "--port", "18090"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(os.stat(d + "/app/config.env").st_mode & 0o777, 0o600)
        env = dict(os.environ, SMSGW_PORT="18090", SMSGW_TOKEN="t" * 20,
                   SMSGW_DATA_DIR=d + "/app/data", HILINK_URL="http://127.0.0.1:18081", NTFY_URL="")
        p = subprocess.Popen([sys.executable, d + "/app/server.py"], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(p.wait)
        self.addCleanup(p.terminate)
        ok = wait(lambda: self._up(18090), 8)
        self.assertTrue(ok)

    def _up(self, port):
        import urllib.request
        try:
            return urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=1).status == 200
        except Exception:
            return False


class Ussd(Case):
    def test_multi_step_and_release(self):
        self.m.ussd_reply = {"*10#": "1) balance\n2) exit", "1": "your balance is 5"}
        st, j, _ = self.i.req("POST", "/ussd", {"code": "*۱۰#".replace("۱", "1").replace("۰", "0")})
        self.assertEqual((st, j["text"]), (200, "1) balance\n2) exit"))
        st, j, _ = self.i.req("POST", "/ussd", {"code": "۱"})
        self.assertEqual((st, j["text"]), (200, "your balance is 5"))
        self.assertEqual(self.m.ussd_log, ["*10#", "1"])
        st, j, _ = self.i.req("POST", "/ussd/release", {})
        self.assertEqual((st, self.m.released), (200, 1))

    def test_timeout_is_clear_and_releases(self):
        st, j, _ = self.i.req("POST", "/ussd", {"code": "*999#"})
        self.assertEqual(st, 502)
        self.assertIn("مهلت", j["error"])
        self.assertNotIn("111019", j["error"])
        self.assertEqual(self.m.released, 1)

    def test_rejects_garbage_and_masks_secrets(self):
        self.assertEqual(self.i.req("POST", "/ussd", {"code": "<x>"})[0], 400)
        self.m.ussd_reply = {"*140*#1234567890123#": "ok"}
        self.assertEqual(self.i.req("POST", "/ussd", {"code": "*140*#1234567890123#"})[0], 200)
        self.assertNotIn("1234567890123", self.i.logs())
        d = self.db().execute("select detail from audit_log where action='ussd'").fetchone()[0]
        self.assertNotIn("1234567890123", d)


class Resilience(Case):
    def test_token_retry(self):
        self.m.fail_tokens = 1
        self.assertEqual(self.i.req("GET", "/status")[0], 200)

    def test_modem_down_gives_persian_error_and_recovers(self):
        self.m.down = True
        st, j, _ = self.i.req("GET", "/status")
        self.assertEqual(st, 502)
        self.assertIn("مودم در دسترس نیست", j["error"])
        self.m.down = False
        self.assertEqual(self.i.req("GET", "/status")[0], 200)
        self.assertNotIn("Traceback", self.i.logs())

    def test_queue_sends_through_fake_modem(self):
        st, j, _ = self.i.req("POST", "/api/v1/messages", {"to": "09120000000", "text": "سلام <'\"&>"})
        self.assertEqual(st, 202)
        wait(lambda: self.m.sent)
        self.assertEqual(len(self.m.sent), 1)

    def test_interrupted_send_is_not_stuck(self):
        c = self.db()
        c.execute("insert into messages(direction,phone,content,status,created_at) "
                  "values('out','09120000000','x','sending',1)")
        c.commit()
        self.i.stop()
        self.i.start()
        wait(lambda: self.db().execute("select status from messages where content='x'").fetchone()[0] == "failed")
        self.assertEqual(self.db().execute("select status from messages where content='x'").fetchone()[0], "failed")


class Security(Case):
    def raw(self, data):
        s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        s.sendall(data)
        b = b""
        try:
            while True:
                x = s.recv(4096)
                if not x:
                    break
                b += x
        except socket.timeout:
            pass
        s.close()
        return b

    def test_negative_content_length(self):
        b = self.raw(b"POST /api/login HTTP/1.0\r\nContent-Length: -1\r\n\r\n")
        self.assertIn(b" 400 ", b.split(b"\r\n")[0])

    def test_non_object_json(self):
        self.assertEqual(self.i.req("POST", "/delete", raw=b"[1,2]")[0], 400)

    def test_non_ascii_bearer_does_not_crash(self):
        b = self.raw("GET /status HTTP/1.0\r\nAuthorization: Bearer تست\r\n\r\n".encode())
        self.assertIn(b" 401 ", b.split(b"\r\n")[0])

    def test_internal_error_does_not_leak(self):
        st, j, _ = self.i.req("POST", "/api/v1/queue/action", {"id": 10 ** 30, "action": "cancel"})
        self.assertIn(st, (400, 500))
        self.assertNotIn("Error", j.get("error", ""))

    def test_security_headers(self):
        import urllib.request
        r = urllib.request.urlopen("http://127.0.0.1:%d/" % PORT)
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")

    def test_xff_first_hop_cannot_spoof(self):
        # پراکسی مورد اعتماد (127.0.0.1) هاپ واقعی را آخر زنجیره می‌گذارد
        for n in range(21):
            self.i.req("POST", "/api/login", {"username": "nobody", "password": "x"},
                       headers={"X-Forwarded-For": "1.2.3.%d, 8.8.8.8" % n})
        st, j, _ = self.i.req("POST", "/api/login", {"username": "nobody", "password": "x"},
                              headers={"X-Forwarded-For": "9.9.9.9, 8.8.8.8"})
        self.assertEqual(st, 429)
        ip = self.db().execute("select distinct ip from audit_log where action='login.failed'").fetchall()
        self.assertEqual([r[0] for r in ip], ["8.8.8.8"])

    def test_must_change_password_enforced_by_server(self):
        c = self.db()
        import auth_helper
        auth_helper.add_user(c, "tester", "password-123", must_change=1)
        st, j, h = self.i.req("POST", "/api/login", {"username": "tester", "password": "password-123"})
        self.assertEqual(st, 200)
        ck = "; ".join(v.split(";")[0] for v in h.get_all("Set-Cookie"))
        import urllib.request, urllib.error
        def call(p):
            r = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, p), headers={"Cookie": ck})
            try:
                return urllib.request.urlopen(r).status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertEqual(call("/api/me"), 200)
        self.assertEqual(call("/api/conversations"), 403)


if __name__ == "__main__":
    unittest.main(verbosity=1)
