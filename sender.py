#!/usr/bin/env python3
"""صف ارسال و **تنها نقطه‌ی تماس با مودم** — فقط stdlib.

⚠️ ثابت معماری (نشکن): مودم هم‌زمان بیش از یک عملیات SMS قبول نمی‌کند و
خطای `113018` می‌دهد. برای همین:

  ۱. **یک حلقه‌ی پس‌زمینه بیشتر وجود ندارد** — `modem_loop`. همان حلقه هم
     صف را خالی می‌کند، هم صندوق ورودی را می‌خواند (آینه‌سازی + ntfy).
     هیچ‌وقت دو thread موازی به مودم نزن.
  ۲. مسیر همگام `/send` (که زبیکس به آن وابسته است) هنوز مستقیم می‌فرستد،
     ولی از همان دروازه‌ی `GATE` رد می‌شود؛ پس با حلقه هم‌پوشانی ندارد و
     فاصله‌ی حداقلی بین ارسال‌ها همیشه رعایت می‌شود.

هر کد جدیدی که می‌خواهد پیامک بفرستد باید `send_now()` را صدا بزند،
نه `hilink.send()` را.
"""
import json, logging, os, threading, time
import db, hilink, store

log = logging.getLogger("smsgw.sender")

DEFAULT_GAP = 3.0          # فاصله‌ی حداقل بین دو ارسال (ثانیه) — بخش ۴ مشخصات
MAX_ATTEMPTS = 3
BACKOFF_BASE = 15          # ۱۵s → ۳۰s → ۶۰s
POLL_STARVE_LIMIT = 3      # اگر poll این‌قدر عقب افتاد، بین ارسال‌ها هم بخوان

# حافظه‌ی مودم: اگر پر شود **دریافت پیامک بی‌صدا متوقف می‌شود**. با رسیدن به
# سقف بالا، قدیمی‌ترین‌ها تا سقف پایین حذف می‌شوند.
STORAGE_HIGH = 0.80
STORAGE_LOW = 0.60
CLEAN_BATCH = 25           # هر دور حداکثر این‌قدر حذف، تا صف عقب نیفتد
PURGE_BATCH = 50
LOOP_STALE = 180           # اگر حلقه این‌قدر تیک نزند، /health خراب گزارش می‌کند

# کدهایی که تکرار تلاش بی‌فایده است — شکست فوری
FATAL_CODES = {"113020"}   # شماره‌ی نامعتبر


class Gate:
    """دروازه‌ی انحصاری مودم: قفل + تضمین فاصله‌ی زمانی بین ارسال‌ها."""

    def __init__(self, gap=DEFAULT_GAP):
        self.lock = threading.RLock()
        self.gap = gap
        self.last_send = 0.0

    def wait_turn(self):
        """قبل از ارسال صدا زده می‌شود؛ باید داخل `with gate.lock` باشد."""
        delta = time.time() - self.last_send
        if delta < self.gap:
            time.sleep(self.gap - delta)

    def mark_sent(self):
        self.last_send = time.time()


GATE = Gate()


def configured_gap():
    try:
        return max(0.5, float(db.setting("send_gap", DEFAULT_GAP)))
    except (TypeError, ValueError):
        return DEFAULT_GAP


def is_paused():
    return db.setting("queue_paused", "0") == "1"


def set_paused(v):
    db.set_setting("queue_paused", "1" if v else "0")


# ----------------------------------------------------------- ارسال هم‌زمان
def modem_call(fn, *a, **kw):
    """هر تماس *خواندنی* با مودم (status/inbox/info/USSD) از اینجا رد شود.

    فاصله‌ی زمانی ندارد — فقط انحصار متقابل، تا خواندنِ برخاسته از یک
    درخواست HTTP با ارسالِ در جریانِ حلقه هم‌پوشانی نکند (`113018`).
    """
    with GATE.lock:
        return fn(*a, **kw)


def send_now(phone, text):
    """ارسال فوری با رعایت دروازه. خروجی: رشته‌ی نتیجه‌ی مودم («OK»)."""
    with GATE.lock:
        GATE.gap = configured_gap()
        GATE.wait_turn()
        try:
            return (hilink.send(phone, text) or "").strip()
        finally:
            GATE.mark_sent()


# --------------------------------------------------------------------- صف
def enqueue(phone, content, scheduled_at=None, user_id=None, batch_id=""):
    """یک پیام در صف می‌گذارد و رکورد آینه‌اش را در messages می‌سازد.

    شماره از `store.clean_destination` رد می‌شود — شماره‌ی نامعتبر هرگز
    وارد صف نمی‌شود.
    """
    p = store.clean_destination(phone)
    text = store.validate_text(content)
    when = int(scheduled_at or db.now())
    with db.tx() as c:
        mid = c.execute(
            "INSERT INTO messages(direction,phone,content,status,user_id,"
            "created_at) VALUES('out',?,?,'queued',?,?)",
            (p, text, user_id, db.now())).lastrowid
        qid = c.execute(
            "INSERT INTO queue(phone,content,scheduled_at,status,batch_id,"
            "message_id,user_id,created_at,next_try_at) "
            "VALUES(?,?,?,'queued',?,?,?,?,0)",
            (p, text, when, batch_id, mid, user_id, db.now())).lastrowid
    return qid, mid


def enqueue_many(phones, content, scheduled_at=None, user_id=None, batch_id=""):
    """ثبت اتمیک چند پیام؛ یا همه وارد صف می‌شوند یا هیچ‌کدام."""
    if not isinstance(phones, (list, tuple)):
        phones = [phones]
    if not phones or len(phones) > store.BULK_LIMIT:
        raise store.Invalid("تعداد گیرنده باید بین ۱ و %d باشد" % store.BULK_LIMIT)
    clean = [store.clean_destination(p) for p in phones]
    text = store.validate_text(content)
    when = int(scheduled_at or db.now())
    out = []
    with db.tx() as c:
        for phone in clean:
            mid = c.execute(
                "INSERT INTO messages(direction,phone,content,status,user_id,"
                "created_at) VALUES('out',?,?,'queued',?,?)",
                (phone, text, user_id, db.now())).lastrowid
            qid = c.execute(
                "INSERT INTO queue(phone,content,scheduled_at,status,batch_id,"
                "message_id,user_id,created_at,next_try_at) "
                "VALUES(?,?,?,'queued',?,?,?,?,0)",
                (phone, text, when, batch_id, mid, user_id, db.now())).lastrowid
            out.append({"queue_id": qid, "message_id": mid, "to": phone})
    return out


def claim_next():
    """یک کار آماده را اتمیک برمی‌دارد و به 'sending' می‌برد."""
    now = db.now()
    with db.tx() as c:
        row = c.execute(
            "SELECT * FROM queue WHERE status='queued' AND scheduled_at<=? "
            "AND next_try_at<=? ORDER BY scheduled_at, id LIMIT 1",
            (now, now)).fetchone()
        if not row:
            return None
        c.execute("UPDATE queue SET status='sending' WHERE id=?", (row["id"],))
        if row["message_id"]:
            c.execute("UPDATE messages SET status='sending' WHERE id=?",
                      (row["message_id"],))
    return row


def _finish(row, status, error=""):
    with db.tx() as c:
        c.execute("UPDATE queue SET status=?,error=? WHERE id=?",
                  (status, error[:200], row["id"]))
        if row["message_id"]:
            c.execute("UPDATE messages SET status=?,error=?,sent_at=? WHERE id=?",
                      (status, error[:200],
                       db.now() if status == "sent" else None, row["message_id"]))


def _retry_later(row, error):
    attempts = row["attempts"] + 1
    if attempts >= MAX_ATTEMPTS:
        log.warning("queue #%d: بعد از %d تلاش ناموفق — %s",
                    row["id"], attempts, error)
        with db.tx() as c:
            c.execute("UPDATE queue SET status='failed',attempts=?,error=? "
                      "WHERE id=?", (attempts, error[:200], row["id"]))
            if row["message_id"]:
                c.execute("UPDATE messages SET status='failed',error=? WHERE id=?",
                          (error[:200], row["message_id"]))
        return
    delay = BACKOFF_BASE * (2 ** (attempts - 1))       # ۱۵ → ۳۰ → ۶۰
    log.info("queue #%d: تلاش %d ناموفق (%s) — %ds دیگر دوباره",
             row["id"], attempts, error, delay)
    with db.tx() as c:
        c.execute("UPDATE queue SET status='queued',attempts=?,error=?,"
                  "next_try_at=? WHERE id=?",
                  (attempts, error[:200], db.now() + delay, row["id"]))
        if row["message_id"]:
            c.execute("UPDATE messages SET status='queued' WHERE id=?",
                      (row["message_id"],))


def deliver(row):
    """یک کارِ برداشته‌شده را می‌فرستد و نتیجه را ثبت می‌کند."""
    try:
        result = send_now(row["phone"], row["content"])
    except hilink.HiLinkError as e:
        if e.code in FATAL_CODES:
            log.warning("queue #%d: شکست قطعی (%s) — بدون تکرار", row["id"], e.code)
            _finish(row, "failed", str(e))
        else:
            _retry_later(row, str(e))
        return False
    except Exception as e:
        _retry_later(row, repr(e))
        return False
    if result == "OK":
        _finish(row, "sent")
        return True
    _retry_later(row, "پاسخ غیرمنتظره‌ی مودم: %s" % result)
    return False


def requeue_stuck():
    """بعد از ری‌استارت، کارهای نیمه‌تمام 'sending' به صف برمی‌گردند."""
    with db.tx() as c:
        n = c.execute("UPDATE queue SET status='queued' WHERE status='sending'").rowcount
        c.execute("UPDATE messages SET status='queued' WHERE status='sending' "
                  "AND id IN (SELECT message_id FROM queue WHERE status='queued')")
        # /send همگام ردیف صف ندارد؛ اگر سرویس وسط ارسال مرده باشد نتیجه
        # نامعلوم است و نباید برای همیشه «در حال ارسال» بماند.
        c.execute("UPDATE messages SET status='failed',error=? "
                  "WHERE direction='out' AND status='sending' AND id NOT IN "
                  "(SELECT message_id FROM queue WHERE message_id IS NOT NULL)",
                  ("سرویس حین ارسال ری‌استارت شد؛ نتیجه نامعلوم است",))
    if n:
        log.info("%d کار نیمه‌تمام به صف برگشت", n)
    return n


def queue_stats():
    rows = db.q("SELECT status, COUNT(*) c FROM queue GROUP BY status")
    st = {r["status"]: r["c"] for r in rows}
    nxt = db.q1("SELECT MIN(scheduled_at) t FROM queue WHERE status='queued'")
    return {"queued": st.get("queued", 0), "sending": st.get("sending", 0),
            "sent": st.get("sent", 0), "failed": st.get("failed", 0),
            "canceled": st.get("canceled", 0),
            "paused": is_paused(), "gap": configured_gap(),
            "next_at": nxt["t"] if nxt else None}


def list_queue(limit=200, status=""):
    args = []
    where = ""
    if status:
        if status not in ("queued", "sending", "sent", "failed", "canceled"):
            raise store.Invalid("وضعیت صف نامعتبر است")
        where = "WHERE q.status=?"
        args.append(status)
    args.append(max(1, min(int(limit), 500)))
    rows = db.q("SELECT q.* FROM queue q %s ORDER BY q.id DESC LIMIT ?" % where,
                tuple(args))
    names = store.contact_names([r["phone"] for r in rows])
    return [{"id": r["id"], "phone": r["phone"],
             "name": names.get(r["phone"], ""), "content": r["content"],
             "scheduled_at": r["scheduled_at"], "attempts": r["attempts"],
             "status": r["status"], "error": r["error"],
             "batch_id": r["batch_id"], "message_id": r["message_id"],
             "created_at": r["created_at"], "next_try_at": r["next_try_at"]}
            for r in rows]


def queue_action(qid, action):
    row = db.q1("SELECT * FROM queue WHERE id=?", (int(qid),))
    if not row:
        raise store.Invalid("آیتم صف پیدا نشد")
    if action == "cancel":
        if row["status"] not in ("queued", "failed"):
            raise store.Invalid("فقط پیام منتظر یا ناموفق قابل لغو است")
        with db.tx() as c:
            # شرط وضعیت: اگر حلقه همین لحظه آن را برداشته باشد لغو نباید اثر کند
            done = c.execute("UPDATE queue SET status='canceled' WHERE id=? "
                             "AND status IN ('queued','failed')", (row["id"],)).rowcount
            if not done:
                raise store.Invalid("این پیام همین حالا در حال ارسال است")
            if row["message_id"]:
                c.execute("UPDATE messages SET status='canceled' WHERE id=?",
                          (row["message_id"],))
    elif action == "retry":
        if row["status"] not in ("failed", "canceled"):
            raise store.Invalid("فقط پیام ناموفق یا لغوشده قابل تلاش مجدد است")
        with db.tx() as c:
            c.execute("UPDATE queue SET status='queued',attempts=0,error='',"
                      "scheduled_at=?,next_try_at=0 WHERE id=?", (db.now(), row["id"]))
            if row["message_id"]:
                c.execute("UPDATE messages SET status='queued',error='' WHERE id=?",
                          (row["message_id"],))
    else:
        raise store.Invalid("عملیات صف نامعتبر است")
    return True


def eta_seconds(count, gap=None):
    """زمان تخمینی ارسال `count` پیام — برای نمایش «۵۰ پیام ≈ ۳ دقیقه»."""
    g = gap if gap is not None else configured_gap()
    return int(max(0, count - 1) * g)


def request_wipe():
    """پاک‌سازی کاربر باید مودم را هم خالی کند، وگرنه poll بعدی پیام‌ها را
    دوباره به آرشیو برمی‌گرداند. عمداً بدون backup: کاربر حذف خواسته است."""
    db.set_setting("modem_wipe", "1")
    db.set_setting("modem_purge", "all")


# ---------------------------------------------------------- حافظه‌ی مودم
def storage_usage(counts):
    """خلاصه‌ی ظرفیت از خروجی `sms_count`."""
    used = sum(int(counts.get(k) or 0)
               for k in ("LocalInbox", "LocalOutbox", "LocalDraft"))
    total = int(counts.get("LocalMax") or 0)
    return {"used": used, "max": total,
            "percent": round(100.0 * used / total, 1) if total else 0.0,
            "full": bool(total) and used >= total}


def backup_modem():
    """رونوشت JSON از هر سه صندوق؛ فقط وقتی برمی‌گردد که با شمارنده‌ی مودم
    بخواند و از دیسک قابل خواندن باشد — بدون این نباید چیزی حذف شود."""
    counts = modem_call(hilink.sms_count)
    boxes = {str(b): hilink.list_all(b, modem_call) for b in (1, 2, 3)}
    have = sum(len(v) for v in boxes.values())
    want = storage_usage(counts)["used"]
    if have != want:
        raise RuntimeError("backup ناقص: %d از %d رکورد" % (have, want))
    path = os.path.join(db.DATA_DIR, "modem-backup-%s.json"
                        % time.strftime("%Y%m%d-%H%M%S"))
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"created_at": db.now(), "counts": counts, "boxes": boxes},
                  f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)
    if sum(len(v) for v in saved["boxes"].values()) != want:
        raise RuntimeError("backup قابل تأیید نیست: %s" % path)
    log.info("modem backup: %d records in %s", want, path)
    return path, boxes


def purge_tick(merge):
    """پاک‌سازی دستی و یک‌باره، با تنظیم `modem_purge` = تعداد یا `all`.

    داخل همین حلقه اجرا می‌شود تا ثابت «یک مسیر به مودم» نشکند. اول backup،
    بعد حذف دسته‌ای؛ بین دسته‌ها صف و پیامک هشدار نوبت می‌گیرند.
    """
    want = db.setting("modem_purge", "")
    if not want:
        return False
    wiping = db.setting("modem_wipe", "") == "1"
    if not wiping and not db.setting("modem_purge_backup", ""):
        path, boxes = backup_modem()
        # متن‌های کوتاه‌شده‌ی آرشیو قبل از حذف نسخه‌ی کامل ترمیم می‌شوند
        store.mirror_incoming(merge(boxes["1"]), insert=False)
        db.set_setting("modem_purge_backup", path)
    budget = PURGE_BATCH if want == "all" else min(PURGE_BATCH, int(want))
    rows = []
    for box in (3, 2, 1):
        rows = modem_call(hilink.sms_page, box, 1, budget)
        if rows:
            break
    for r in rows:
        modem_call(hilink.delete, r["index"])
    left = want if want == "all" else str(int(want) - len(rows))
    if not rows or left == "0":
        db.set_setting("modem_purge", "")
        db.set_setting("modem_purge_backup", "")
        db.set_setting("modem_wipe", "")
        log.info("modem purge finished (%d deleted in last batch)", len(rows))
    else:
        db.set_setting("modem_purge", left)
        log.info("modem purge: %d deleted, remaining=%s", len(rows), left)
    return True


_cleaning = False


def clean_storage(merge, keep_after=0):
    """پاک‌سازی خودکار ظرفیت. خروجی: خلاصه‌ی ظرفیت بعد از این دور.

    اول قدیمی‌ترین صندوق ارسالی (نسخه‌اش در آرشیو هست). بعد ورودی‌هایی که
    هم آینه شده‌اند و هم فورواردشان تمام است (`keep_after`). پیش‌نویس
    دست‌نخورده می‌ماند. اگر چیزی امن برای حذف نباشد، حذف نمی‌کند — همین
    حالت است که هشدار ظرفیت را فعال می‌کند.
    """
    global _cleaning
    usage = storage_usage(modem_call(hilink.sms_count))
    low = int(usage["max"] * STORAGE_LOW)
    # آستانه‌ی دوگانه: با ۸۰٪ شروع و تا ۶۰٪ ادامه، نه این‌که هر دور فقط کمی
    # زیر سقف برگردد.
    if usage["max"] and usage["used"] >= usage["max"] * STORAGE_HIGH:
        _cleaning = True
    if not _cleaning or not usage["max"]:
        return usage
    if usage["used"] <= low:
        _cleaning = False
        return usage
    need = min(CLEAN_BATCH, usage["used"] - low)
    victims = [r["index"] for r in hilink.list_all(2, modem_call)[:need]]
    if len(victims) < need:
        for g in sorted(merge(hilink.list_all(1, modem_call)),
                        key=lambda g: min(g["indexes"])):
            if len(victims) >= need:
                break
            if max(g["indexes"]) <= keep_after and store.is_mirrored(g["indexes"]):
                victims.extend(sorted(g["indexes"]))
    for index in victims[:CLEAN_BATCH]:
        modem_call(hilink.delete, index)
    if victims:
        log.info("modem storage %d/%d: %d old records deleted",
                 usage["used"], usage["max"], len(victims[:CLEAN_BATCH]))
        usage = storage_usage(modem_call(hilink.sms_count))
    return usage


# ------------------------------------------------------- تنها حلقه‌ی مودم
LAST_TICK = 0.0


def loop_alive():
    return time.time() - LAST_TICK < LOOP_STALE


def modem_loop(inbox_handler, poll_interval=30, stop=None, maintenance=None):
    """یگانه حلقه‌ای که با مودم حرف می‌زند.

    اولویت با صف است؛ وقتی صف خالی است صندوق ورودی خوانده می‌شود. اگر یک
    ارسال گروهی طولانی خواندن را بیش از حد عقب بیندازد، بین ارسال‌ها هم
    یک‌بار می‌خوانَد — چون همه در همین یک thread است، هیچ‌وقت هم‌پوشانی
    نمی‌شود.

    ⚠️ هیچ خطایی نباید این thread را بکشد: مرگ بی‌صدای آن یعنی صف و دریافت
    برای همیشه می‌ایستند، در حالی که API سالم به نظر می‌رسد.
    """
    global LAST_TICK
    LAST_TICK = time.time()
    next_poll = 0.0
    requeued = False

    def do_poll():
        nonlocal next_poll
        try:
            with GATE.lock:              # خواندن هم نباید با ارسال هم‌پوشانی کند
                inbox_handler()
            if maintenance:
                maintenance()
        except hilink.HiLinkError as e:
            log.error("inbox poll: %s", e)
        except Exception:
            log.exception("inbox poll failed")
        next_poll = time.time() + poll_interval

    while not (stop and stop.is_set()):
        LAST_TICK = time.time()
        worked = False
        try:
            if not requeued:
                requeue_stuck()
                requeued = True
            if not is_paused():
                row = claim_next()
                if row is not None:
                    deliver(row)
                    worked = True
                    # اگر خواندن خیلی عقب افتاده، همین‌جا یک‌بار بخوان
                    if time.time() - next_poll > poll_interval * POLL_STARVE_LIMIT:
                        do_poll()
            if time.time() >= next_poll:
                do_poll()
        except Exception:
            log.exception("modem loop iteration failed")
            worked = False
        if not worked:
            time.sleep(1)
