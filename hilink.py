#!/usr/bin/env python3
"""Huawei HiLink client for legacy firmware (stdlib only)."""
import os, time, html, threading, http.client
import urllib.error, urllib.request
import xml.etree.ElementTree as ET

BASE = os.environ.get("HILINK_URL", "http://192.168.8.1")
TIMEOUT = 10
_lock = threading.Lock()

ERRORS = {
    "100002": "عملیات پشتیبانی نمی‌شود",
    "108006": "رمز اشتباه",
    "111019": "پاسخی از شبکه آماده نیست",
    "113004": "سرویس پیامک شبکه پاسخ نداد",
    "113053": "حافظه‌ی پیامک مودم پر است",
    "113055": "این پیام روی مودم پیدا نشد",
    "113114": "این پیام روی مودم پیدا نشد",
    "113018": "ماژول SMS مشغول است",
    "113020": "شماره نامعتبر",
    "125001": "توکن نامعتبر",
    "125002": "سشن نامعتبر",
    "125003": "توکن منقضی شده",
}

# توکن یک‌بارمصرف گاهی رد می‌شود؛ درخواست اصلاً اجرا نشده و تکرارش امن است.
TOKEN_ERRORS = {"125001", "125002", "125003"}

class HiLinkError(Exception):
    """خطای مودم. `code` کد خام مودم است (مثل `113018`) تا فراخواننده
    بتواند مطمئن تصمیم بگیرد — نه با تطبیق رشته‌ی پیام."""

    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code

def _req(path, body=None, token=None):
    r = urllib.request.Request(BASE + path)
    if token:
        r.add_header("__RequestVerificationToken", token)
    if body is not None:
        r.data = body.encode("utf-8")
        r.add_header("Content-Type", "text/xml; charset=UTF-8")
    try:
        with urllib.request.urlopen(r, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        raise HiLinkError("مودم در دسترس نیست (%s)" % type(e).__name__,
                          code="unreachable") from e

def _xml(raw):
    try:
        return ET.fromstring(raw)
    except ET.ParseError as e:
        raise HiLinkError("پاسخ نامعتبر از مودم", code="badxml") from e

def _token():
    t = (_xml(_req("/api/webserver/token")).findtext("token") or "").strip()
    if not t:
        raise HiLinkError("توکن دریافت نشد")
    return t

def _call(path, body=None):
    for attempt in (1, 2):
        with _lock:
            raw = _req(path, body, _token())
        root = _xml(raw)
        if root.tag != "error":
            return root
        c = root.findtext("code") or "?"
        if c in TOKEN_ERRORS and attempt == 1:
            time.sleep(0.2)
            continue
        raise HiLinkError(f"{c}: {ERRORS.get(c, 'خطای ناشناخته مودم')}", code=c)

def _flat(root):
    return {c.tag: (c.text or "") for c in root}

def device_info(): return _flat(_call("/api/device/information"))
def signal():      return _flat(_call("/api/device/signal"))
def status():      return _flat(_call("/api/monitoring/status"))
def sms_count():   return _flat(_call("/api/sms/sms-count"))
def traffic():     return _flat(_call("/api/monitoring/traffic-statistics"))
def current_plmn(): return _flat(_call("/api/net/current-plmn"))
def basic_info():  return _flat(_call("/api/device/basic_information"))

def sms_page(box=1, page=1, count=50):
    """یک صفحه از صندوق: 1=ورودی، 2=ارسالی، 3=پیش‌نویس."""
    body = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><request>"
            "<PageIndex>%d</PageIndex><ReadCount>%d</ReadCount>"
            "<BoxType>%d</BoxType><SortType>0</SortType>"
            "<Ascending>0</Ascending><UnreadPreferred>0</UnreadPreferred>"
            "</request>" % (page, count, box))
    out = []
    for m in _call("/api/sms/sms-list", body).iter("Message"):
        out.append({
            "index": int(m.findtext("Index") or 0),
            "phone": m.findtext("Phone") or "",
            "content": m.findtext("Content") or "",
            "date": m.findtext("Date") or "",
            "unread": (m.findtext("Smstat") or "1") == "0",
        })
    return out

def inbox(count=20, box=1):
    return sms_page(box, 1, count)

def list_all(box, call=None, page_size=50):
    """کل یک صندوق، صفحه‌به‌صفحه. `call` همان `sender.modem_call` است تا قفل
    مودم بین صفحه‌ها آزاد شود و پیامک هشدار پشت خواندن طولانی نماند."""
    call = call or (lambda fn, *a: fn(*a))
    seen = {}
    for page in range(1, 1001):
        rows = call(sms_page, box, page, page_size)
        fresh = [r for r in rows if r["index"] not in seen]
        for r in fresh:
            seen[r["index"]] = r
        if len(rows) < page_size or not fresh:
            break
    return sorted(seen.values(), key=lambda r: r["index"])

def send(phone, content, reserved=1):
    body = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><request>"
            "<Index>-1</Index><Phones><Phone>%s</Phone></Phones><Sca></Sca>"
            "<Content>%s</Content><Length>%d</Length>"
            "<Reserved>%d</Reserved><Date>%s</Date></request>"
            % (html.escape(phone), html.escape(content), len(content),
               reserved, time.strftime("%Y-%m-%d %H:%M:%S")))
    return (_call("/api/sms/send-sms", body).text or "").strip()

def delete(index):
    body = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><request>"
            "<Index>%d</Index></request>" % int(index))
    return (_call("/api/sms/delete-sms", body).text or "").strip()

def ussd(content, timeout=15):
    """Send a USSD code (or a reply within an active session) and wait for
    the carrier's response. The response only becomes available for a brief
    window (~1-1.5s observed) after send, and /api/ussd/get errors with
    111019 outside that window - so we must poll fast and treat that error
    as "not ready yet" rather than fatal."""
    body = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><request>"
            "<content>%s</content><codeType>1</codeType><timeout></timeout></request>"
            % html.escape(content))
    try:
        _call("/api/ussd/send", body)
    except HiLinkError:
        ussd_release()      # جلسه‌ی گیرکرده نباید درخواست بعدی را هم خراب کند
        raise
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.3)
        try:
            c = _call("/api/ussd/get").findtext("content")
        except HiLinkError as e:
            if e.code in ("unreachable", "badxml"):
                raise
            continue
        if c:
            return c
    ussd_release()
    raise HiLinkError("پاسخی از اپراتور نرسید؛ مهلت %d ثانیه‌ای تمام شد" % timeout,
                      code="ussd_timeout")

def ussd_release():
    """بستن جلسه‌ی USSD. بهترین‌تلاش: مودم ممکن است جلسه‌ی بازی نداشته باشد."""
    try:
        _call("/api/ussd/release")
        return True
    except HiLinkError:
        return False

if __name__ == "__main__":
    import sys, json
    a = sys.argv[1:] or ["info"]
    if a[0] == "send":
        print(send(a[1], a[2], int(a[3]) if len(a) > 3 else 1))
    elif a[0] == "inbox":
        print(json.dumps(inbox(), ensure_ascii=False, indent=2))
    elif a[0] == "delete":
        print(delete(a[1]))
    elif a[0] == "count":
        print(json.dumps(sms_count(), ensure_ascii=False, indent=2))
    elif a[0] == "ussd":
        print(ussd(a[1]))
    else:
        d = device_info(); d.update(signal()); d.update(status())
        print(json.dumps(d, ensure_ascii=False, indent=2))
