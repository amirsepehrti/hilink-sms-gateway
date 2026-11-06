#!/usr/bin/env python3
"""مودم HiLink و سرور ntfy ساختگی برای تست آفلاین — هیچ پیامک واقعی فرستاده نمی‌شود."""
import json, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape


class Modem:
    def __init__(self, capacity=500):
        self.capacity = capacity
        self.boxes = {1: [], 2: [], 3: []}
        self.next_index = 40000
        self.sent = []
        self.tokens = set()
        self.fail_tokens = 0     # چند توکن بعدی رد شوند (125001)
        self.down = False
        self.ussd_reply = {}     # کد → پاسخ
        self.ussd_pending = None
        self.ussd_at = 0.0
        self.ussd_delay = 0.2
        self.ussd_log = []
        self.released = 0
        self.lock = threading.Lock()

    def add(self, box, phone, content, date=None, index=None):
        with self.lock:
            if index is None:
                index = self.next_index
                self.next_index += 1
            self.boxes[box].append({"Index": index, "Phone": phone, "Content": content,
                                    "Date": date or time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "Smstat": "0" if box == 1 else "3"})
            return index

    def used(self):
        return sum(len(v) for v in self.boxes.values())

    def fill(self, inbox=0, outbox=0, draft=0, date="2026-01-01 00:00:00"):
        for box, n in ((1, inbox), (2, outbox), (3, draft)):
            for i in range(n):
                self.add(box, "0912000%04d" % i, "old %d" % i, date)


def start(modem, port):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def xml(self, body):
            b = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>" + body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def err(self, code):
            self.xml("<error><code>%s</code><message></message></error>" % code)

        def ok(self):
            self.xml("<response>OK</response>")

        def flat(self, d):
            self.xml("<response>%s</response>" % "".join(
                "<%s>%s</%s>" % (k, escape(str(v)), k) for k, v in d.items()))

        def route(self, body):
            p = self.path
            if modem.down:
                self.close_connection = True
                self.connection.close()
                return
            if p == "/api/webserver/token":
                t = "%010d" % (time.time_ns() % 10 ** 10)
                modem.tokens.add(t)
                return self.xml("<response><token>%s</token></response>" % t)
            tok = self.headers.get("__RequestVerificationToken")
            if tok not in modem.tokens:
                return self.err("125001")
            modem.tokens.discard(tok)
            if modem.fail_tokens:
                modem.fail_tokens -= 1
                return self.err("125001")
            if p == "/api/sms/sms-list":
                page = int(re.search(r"<PageIndex>(\d+)", body).group(1))
                n = int(re.search(r"<ReadCount>(\d+)", body).group(1))
                box = int(re.search(r"<BoxType>(\d+)", body).group(1))
                rows = sorted(modem.boxes[box], key=lambda m: -m["Index"])
                rows = rows[(page - 1) * n:page * n]
                x = "".join("<Message>%s</Message>" % "".join(
                    "<%s>%s</%s>" % (k, escape(str(v)), k) for k, v in m.items())
                    for m in rows)
                return self.xml("<response><Count>%d</Count><Messages>%s</Messages>"
                                "</response>" % (len(rows), x))
            if p == "/api/sms/sms-count":
                return self.flat({"LocalUnread": len(modem.boxes[1]),
                                  "LocalInbox": len(modem.boxes[1]),
                                  "LocalOutbox": len(modem.boxes[2]),
                                  "LocalDraft": len(modem.boxes[3]),
                                  "LocalDeleted": 0, "LocalMax": modem.capacity,
                                  "SimMax": 50, "NewMsg": 0})
            if p == "/api/sms/delete-sms":
                i = int(re.search(r"<Index>(\d+)", body).group(1))
                with modem.lock:
                    for b in modem.boxes.values():
                        b[:] = [m for m in b if m["Index"] != i]
                return self.ok()
            if p == "/api/sms/send-sms":
                modem.sent.append(body)
                if modem.used() < modem.capacity:
                    modem.add(2, "x", re.search(r"<Content>(.*)</Content>", body, re.S).group(1))
                return self.ok()
            if p == "/api/ussd/send":
                code = re.search(r"<content>(.*)</content>", body).group(1)
                modem.ussd_log.append(code)
                modem.ussd_pending = modem.ussd_reply.get(code)
                modem.ussd_at = time.time()
                return self.ok()
            if p == "/api/ussd/get":
                age = time.time() - modem.ussd_at
                if modem.ussd_pending and modem.ussd_delay <= age:
                    return self.xml("<response><content>%s</content></response>"
                                    % escape(modem.ussd_pending))
                return self.err("111019")
            if p == "/api/ussd/release":
                modem.released += 1
                modem.ussd_pending = None
                return self.ok()
            if p == "/api/device/signal":
                return self.flat({"rssi": "-70dBm", "rscp": "-80dBm"})
            if p == "/api/monitoring/status":
                return self.flat({"ConnectionStatus": "901", "SignalIcon": "4",
                                  "CurrentNetworkType": "41"})
            if p == "/api/device/information":
                return self.flat({"DeviceName": "E303", "SoftwareVersion": "22.318"})
            return self.flat({"x": "1"})

        def do_GET(self):
            self.route("")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.route(self.rfile.read(n).decode())

    s = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


class Ntfy:
    def __init__(self):
        self.posts = []
        self.fail = False


def start_ntfy(state, port):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            b = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if state.fail:
                self.send_response(500)
                self.end_headers()
                return
            state.posts.append(json.loads(b))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    s = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s
