#!/usr/bin/env python3
"""یک نمونه‌ی کاملاً جدا از سرویس: دیتابیس موقت، مودم ساختگی، پورت جدا."""
import json, os, shutil, subprocess, sys, tempfile, time, urllib.error, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fake_hilink

PORT, MODEM_PORT, NTFY_PORT = 18080, 18081, 18082
TOKEN = "offline-test-token"


class Instance:
    def __init__(self, env=None, capacity=500):
        self.dir = tempfile.mkdtemp(prefix="smsgw-test-")
        self.modem = fake_hilink.Modem(capacity)
        self.ntfy = fake_hilink.Ntfy()
        self.servers = [fake_hilink.start(self.modem, MODEM_PORT),
                        fake_hilink.start_ntfy(self.ntfy, NTFY_PORT)]
        e = dict(os.environ, SMSGW_TOKEN=TOKEN, SMSGW_PORT=str(PORT),
                 SMSGW_DATA_DIR=self.dir, HILINK_URL="http://127.0.0.1:%d" % MODEM_PORT,
                 NTFY_URL="http://127.0.0.1:%d" % NTFY_PORT,
                 SMSGW_NTFY_STATE=os.path.join(self.dir, "ntfy.json"),
                 NTFY_POLL_INTERVAL="1", SMSGW_NTFY_SETTLE="1")
        e.update(env or {})
        self.env = e
        self.log = open(os.path.join(self.dir, "server.log"), "w")
        self.start()

    def start(self):
        self.proc = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(HERE), "server.py")], env=self.env,
                                     stdout=self.log, stderr=self.log)
        for _ in range(100):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/health" % PORT, timeout=1)
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("service did not start")

    def stop(self):
        self.proc.terminate()
        self.proc.wait(10)

    def close(self):
        self.stop()
        for s in self.servers:
            s.shutdown()
            s.server_close()
        self.log.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def logs(self):
        self.log.flush()
        return open(self.log.name, encoding="utf-8").read()

    def req(self, method, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=data, method=method)
        r.add_header("Authorization", "Bearer " + TOKEN)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r, timeout=40) as resp:
                return resp.status, json.loads(resp.read() or b"{}"), resp.headers
        except urllib.error.HTTPError as e:
            b = e.read()
            try:
                return e.code, json.loads(b or b"{}"), e.headers
            except ValueError:
                return e.code, {"raw": b[:200].decode("utf-8", "replace")}, e.headers


def wait(fn, timeout=15, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()
