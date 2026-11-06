#!/usr/bin/env python3
"""منطق دامنه: پیام‌ها، مکالمات، مخاطبین، قالب‌ها — روی db.py.

`db.py` نازک می‌ماند (اتصال + migration)؛ هرچه معنای دامنه دارد اینجاست.
"""
import csv, io, re, time
import db

# ------------------------------------------------------------------ شماره‌ها
FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

class Invalid(ValueError):
    """ورودی نامعتبرِ کاربر — لایه‌ی HTTP آن را به ۴۰۰ تبدیل می‌کند.
    زیرکلاس ValueError است تا ValueErrorهای واقعیِ کد (باگ) با آن قاطی نشوند."""


MAX_SMS_CHARS = 1000          # ~۷ بخش UCS-2 — سقف عقلانی برای یک پیام
BULK_LIMIT = 50               # سقف سخت گیرنده در ارسال گروهی (بخش ۴ مشخصات)


def normalize_phone(p):
    """فقط شکل متعارف — **بدون اعتبارسنجی**.

    ⚠️ این تابع برای *جستجو و گروه‌بندی داده‌ی موجود* است، نه برای مقصد
    ارسال. ورودی نامعتبر را دست‌نخورده برمی‌گرداند، چون فرستنده‌ی پیام
    ورودی می‌تواند غیرعددی باشد (مثل «HAMRAHAVAL») و نباید رد شود.

    **برای هر مسیری که پیامک به آن شماره می‌رود از `clean_destination`
    استفاده کن.** تست `SinglePhoneEntryPoint` تضمین می‌کند هیچ ماژول
    دیگری این تابع را مستقیم صدا نزند.

    ارقام فارسی/عربی به لاتین، `+98`/`0098`/`98` به `0`.
    """
    if not p:
        return ""
    s = str(p).translate(FA_DIGITS).strip()
    s = re.sub(r"[\s\-()]", "", s)
    if s.startswith("00"):
        s = "+" + s[2:]
    if re.fullmatch(r"\+?98\d{10}", s):
        s = "0" + s.lstrip("+")[2:]
    elif re.fullmatch(r"9\d{9}", s):
        s = "0" + s
    return s


def is_mobile(p):
    return bool(re.fullmatch(r"09\d{9}", p or ""))


def clean_destination(p):
    """**تنها نقطه‌ی ورود برای شماره‌ی مقصد.** متعارف‌سازی + اعتبارسنجی با هم.

    خروجی: شماره‌ی متعارف‌شده. ورودی نامعتبر → `Invalid` (یعنی HTTP 400).

    هر مسیری که به ارسال ختم می‌شود — `/send`، صف، ارسال گروهی،
    زمان‌بندی، ایمپورت مخاطب — باید از همین‌جا رد شود. تابع‌های نوشتنِ
    `record_outgoing` و `enqueue` خودشان هم این را صدا می‌زنند تا حتی
    فراموشیِ یک صداکننده هم نتواند شماره‌ی نامعتبر را وارد دیتابیس کند.
    """
    s = normalize_phone(p)
    if not s:
        raise Invalid("شماره‌ی مقصد خالی است")
    if is_mobile(s):
        return s
    # کدهای کوتاه/خدماتی: فقط رقم، ۳ تا ۱۵ رقم
    if re.fullmatch(r"\+?\d{3,15}", s):
        return s
    raise Invalid("شماره‌ی نامعتبر: %s" % p)


def validate_text(t):
    t = "" if t is None else str(t)
    if not t.strip():
        raise Invalid("متن پیام خالی است")
    if len(t) > MAX_SMS_CHARS:
        raise Invalid("متن پیام بیش از %d کاراکتر است" % MAX_SMS_CHARS)
    return t


def sms_parts(text):
    ucs = any(ord(c) > 127 for c in text)
    per, mul = (70, 67) if ucs else (160, 153)
    n = len(text)
    return 1 if n <= per else -(-n // mul)


# -------------------------------------------------------------------- پیام‌ها
def modem_date_to_ts(s):
    """«2026-07-29 19:37:43» → epoch محلی. در خطا None."""
    try:
        return int(time.mktime(time.strptime((s or "").strip(),
                                             "%Y-%m-%d %H:%M:%S")))
    except Exception:
        return None


def mirror_incoming(merged, insert=True):
    """پیام‌های ورودیِ ادغام‌شده‌ی مودم را در جدول messages آینه می‌کند.

    کلید یکتا `(direction, modem_index)` است، پس فراخوانی مکرر رکورد
    تکراری نمی‌سازد. هدف: تاریخچه بعد از پاک شدن حافظه‌ی مودم باقی بماند.
    فقط جهت ورودی — دلیلش در docs/HARDWARE.md.

    دو حالت واقعی که باید درست مدیریت شوند:
      • پیام بلند تکه‌تکه می‌رسد. پس ردیف موجود با متن کامل‌تر **به‌روز**
        می‌شود، وگرنه فقط تکه‌ی اول برای همیشه می‌ماند.
      • اگر مودم بعد از خالی شدن حافظه ایندکس را از نو بدهد، همان ایندکس
        پیام دیگری است (فرستنده یا زمانش فرق دارد). ردیف قدیمی از ایندکس
        جدا می‌شود تا پیام تازه بی‌صدا دور ریخته نشود.

    خروجی: تعداد ردیف‌های تازه. با `insert=False` فقط متن‌های ناقص ترمیم
    می‌شوند و ردیف جدیدی ساخته نمی‌شود.
    """
    new = 0
    for m in merged:
        idx = min(m["indexes"])
        ts = modem_date_to_ts(m.get("date", "")) or db.now()
        phone = normalize_phone(m["phone"])
        qs = ",".join("?" * len(m["indexes"]))
        rows = db.q("SELECT id,phone,content,created_at,deleted_at FROM messages "
                    "WHERE direction='in' AND modem_index IN (%s)" % qs,
                    tuple(m["indexes"]))
        same = [r for r in rows if r["phone"] == phone and r["created_at"] == ts]
        if same:
            row = same[0]
            # پیامی که کاربر پاک کرده نباید با poll بعدی متنش برگردد
            if len(m["content"]) > len(row["content"]) and not row["deleted_at"]:
                db.ex("UPDATE messages SET content=? WHERE id=?",
                      (m["content"], row["id"]))
            continue
        if not insert:
            continue
        for r in rows:          # ایندکس بازیافت‌شده: همان عدد، پیام دیگر
            db.ex("UPDATE messages SET modem_index=NULL WHERE id=?", (r["id"],))
        cur = db.ex(
            "INSERT OR IGNORE INTO messages(direction,phone,content,status,"
            "modem_index,created_at) VALUES('in',?,?,'received',?,?)",
            (phone, m["content"], idx, ts))
        new += cur.rowcount
    return new


def is_mirrored(indexes):
    """آیا این پیام مودم در آرشیو محلی هست؟ (شرط حذف امن از مودم)"""
    if not indexes:
        return False
    qs = ",".join("?" * len(indexes))
    return bool(db.q1("SELECT 1 FROM messages WHERE direction='in' AND "
                      "modem_index IN (%s) LIMIT 1" % qs, tuple(indexes)))


def record_outgoing(phone, content, status, user_id=None, error="", sent_at=None):
    # اعتبارسنجی در مرز نوشتن: شماره‌ی نامعتبر حتی اگر صداکننده فراموش
    # کرده باشد هم وارد دیتابیس نمی‌شود.
    return db.insert(
        "INSERT INTO messages(direction,phone,content,status,error,user_id,"
        "created_at,sent_at) VALUES('out',?,?,?,?,?,?,?)",
        (clean_destination(phone), content, status, error or "", user_id,
         db.now(), sent_at))


def update_message(mid, **kw):
    if not mid or not kw:
        return
    cols = ",".join("%s=?" % k for k in kw)
    db.ex("UPDATE messages SET %s WHERE id=?" % cols, tuple(kw.values()) + (mid,))


def message_row(m, names=None):
    return {"id": m["id"], "direction": m["direction"], "phone": m["phone"],
            "name": (names or {}).get(m["phone"], ""),
            "content": m["content"], "status": m["status"],
            "error": "" if m["error"] == "read" else m["error"],
            "read": m["direction"] == "in" and m["error"] == "read",
            "created_at": m["created_at"], "sent_at": m["sent_at"],
            "deleted": bool(m["deleted_at"])}


def contact_names(phones):
    phones = [p for p in set(phones) if p]
    if not phones:
        return {}
    qs = ",".join("?" * len(phones))
    return {r["phone"]: r["name"] for r in
            db.q("SELECT phone,name FROM contacts WHERE phone IN (%s)" % qs,
                 tuple(phones))}


# ------------------------------------------------------------------- مکالمات
# «خوانده‌شده» با گذاشتن نشانه‌ی 'read' در ستون error پیام ورودی ثبت می‌شود:
# این ستون برای جهت ورودی بلااستفاده است و اینطور به مهاجرت جدید نیاز نیست.
_UNREAD = "direction='in' AND deleted_at IS NULL AND COALESCE(error,'')<>'read'"


def conversations(limit=200, search=""):
    """فهرست گفتگوها؛ هر شماره یک ردیف، با آخرین پیام و شمار خوانده‌نشده."""
    where, args = "", []
    if search:
        like = "%%%s%%" % search
        where = ("WHERE deleted_at IS NULL AND (phone LIKE ? OR content LIKE ? OR phone IN "
                 "(SELECT phone FROM contacts WHERE name LIKE ?))")
        args = ["%%%s%%" % normalize_phone(search), like, like]
    else:
        where = "WHERE deleted_at IS NULL"
    rows = db.q(
        "SELECT phone, MAX(created_at) AS last_at, COUNT(*) AS total, "
        "  SUM(CASE WHEN %s THEN 1 ELSE 0 END) AS unread "
        "FROM messages %s GROUP BY phone ORDER BY last_at DESC LIMIT ?"
        % (_UNREAD, where), tuple(args) + (limit,))
    names = contact_names([r["phone"] for r in rows])
    out = []
    for r in rows:
        last = db.q1("SELECT direction,content FROM messages WHERE phone=? AND deleted_at IS NULL "
                     "ORDER BY created_at DESC, id DESC LIMIT 1", (r["phone"],))
        out.append({"phone": r["phone"], "name": names.get(r["phone"], ""),
                    "last_at": r["last_at"], "total": r["total"],
                    "unread": r["unread"] or 0,
                    "last_direction": last["direction"] if last else "",
                    "preview": (last["content"] or "")[:80] if last else ""})
    return out


def conversation(phone, limit=300, before=None):
    """پیام‌های یک مکالمه به ترتیب زمان.

    همیشه **جدیدترین** `limit` پیام برمی‌گردد؛ قدیمی‌ترها با `before`
    (شناسه‌ی قدیمی‌ترین پیامِ نمایش‌داده‌شده) صفحه‌به‌صفحه می‌آیند.
    """
    p = normalize_phone(phone)
    where, args = "phone=? AND deleted_at IS NULL", [p]
    if before:
        ref = db.q1("SELECT created_at,id FROM messages WHERE id=?", (before,))
        if not ref:
            raise Invalid("پیام مرجع پیدا نشد")
        where += " AND (created_at<? OR (created_at=? AND id<?))"
        args += [ref["created_at"], ref["created_at"], ref["id"]]
    rows = db.q("SELECT * FROM messages WHERE %s ORDER BY created_at DESC, id DESC "
                "LIMIT ?" % where, tuple(args) + (limit + 1,))
    has_more = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    names = contact_names([p])
    return {"phone": p, "name": names.get(p, ""), "has_more": has_more,
            "messages": [message_row(m, names) for m in rows]}


def mark_read(phone):
    return db.ex("UPDATE messages SET error='read' WHERE phone=? AND " + _UNREAD,
                 (normalize_phone(phone),)).rowcount


def unread_total():
    return db.q1("SELECT COUNT(*) c FROM messages WHERE " + _UNREAD)["c"]


def search_messages(term, limit=200):
    like = "%%%s%%" % term
    rows = db.q("SELECT * FROM messages WHERE deleted_at IS NULL AND (content LIKE ? OR phone LIKE ?) "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (like, "%%%s%%" % normalize_phone(term), limit))
    names = contact_names([r["phone"] for r in rows])
    return [message_row(m, names) for m in rows]


def list_messages(limit=100, phone="", direction="", since=0):
    where, args = ["deleted_at IS NULL"], []
    if phone:
        where.append("phone=?")
        args.append(normalize_phone(phone))
    if direction:
        if direction not in ("in", "out"):
            raise Invalid("direction باید in یا out باشد")
        where.append("direction=?")
        args.append(direction)
    if since:
        where.append("created_at>=?")
        args.append(int(since))
    args.append(max(1, min(int(limit), 500)))
    rows = db.q("SELECT * FROM messages %s ORDER BY created_at DESC,id DESC LIMIT ?"
                % (("WHERE " + " AND ".join(where)) if where else ""), tuple(args))
    names = contact_names([r["phone"] for r in rows])
    return [message_row(r, names) for r in rows]


def soft_delete_message(message_id, user_id=None):
    """پیام را از مکالمه پنهان می‌کند؛ ردیف برای آمار و حسابرسی باقی می‌ماند."""
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        raise Invalid("شناسه پیام نامعتبر است")
    row = db.q1("SELECT id,phone,direction,modem_index,deleted_at FROM messages WHERE id=?",
                (mid,))
    if not row:
        raise Invalid("پیام پیدا نشد")
    if row["deleted_at"]:
        return {"id": mid, "already_deleted": True,
                "phone": row["phone"], "direction": row["direction"],
                "modem_index": row["modem_index"]}
    db.ex("UPDATE messages SET deleted_at=?,deleted_by=? WHERE id=? AND deleted_at IS NULL",
          (db.now(), user_id, mid))
    return {"id": mid, "already_deleted": False,
            "phone": row["phone"], "direction": row["direction"],
            "modem_index": row["modem_index"]}


def soft_delete_conversation(phone, user_id=None):
    """همه‌ی پیام‌های قابل‌نمایش یک مکالمه را پنهان می‌کند؛ آمار حفظ می‌شود."""
    p = normalize_phone(phone)
    if not p:
        raise Invalid("شماره‌ی مکالمه لازم است")
    with db.tx() as c:
        count = c.execute("SELECT COUNT(*) c FROM messages WHERE phone=? "
                          "AND deleted_at IS NULL", (p,)).fetchone()["c"]
        if not count:
            raise Invalid("مکالمه‌ای برای حذف پیدا نشد")
        c.execute("UPDATE messages SET deleted_at=?,deleted_by=? WHERE phone=? "
                  "AND deleted_at IS NULL", (db.now(), user_id, p))
    return {"phone": p, "deleted": count}


# ------------------------------------------------------------------ مخاطبین
def list_contacts(search="", group_id=None):
    where, args, join = [], [], ""
    if group_id:
        join = "JOIN contact_group_members gm ON gm.contact_id=c.id AND gm.group_id=?"
        args.append(int(group_id))
    if search:
        where.append("(c.name LIKE ? OR c.phone LIKE ? OR c.note LIKE ?)")
        args += ["%%%s%%" % search] * 3
    rows = db.q("SELECT c.* FROM contacts c %s %s ORDER BY c.name"
                % (join, ("WHERE " + " AND ".join(where)) if where else ""),
                tuple(args))
    gmap = {}
    for r in db.q("SELECT gm.contact_id, g.id, g.name, g.color FROM "
                  "contact_group_members gm JOIN contact_groups g ON g.id=gm.group_id"):
        gmap.setdefault(r["contact_id"], []).append(
            {"id": r["id"], "name": r["name"], "color": r["color"]})
    return [{"id": c["id"], "name": c["name"], "phone": c["phone"],
             "note": c["note"], "created_at": c["created_at"],
             "groups": gmap.get(c["id"], [])} for c in rows]


def upsert_contact(cid, name, phone, note, group_ids):
    name = (name or "").strip()
    if not name:
        raise Invalid("نام مخاطب لازم است")
    if len(name) > 64:
        raise Invalid("نام مخاطب بیش از حد بلند است")
    p = clean_destination(phone)
    note = (note or "").strip()[:200]
    clash = db.q1("SELECT id FROM contacts WHERE phone=?", (p,))
    if clash and clash["id"] != (cid or 0):
        raise Invalid("این شماره قبلاً ثبت شده است")
    with db.tx() as c:
        if cid:
            if not c.execute("SELECT 1 FROM contacts WHERE id=?", (cid,)).fetchone():
                raise Invalid("مخاطب پیدا نشد")
            c.execute("UPDATE contacts SET name=?,phone=?,note=?,updated_at=? "
                      "WHERE id=?", (name, p, note, db.now(), cid))
        else:
            cid = c.execute("INSERT INTO contacts(name,phone,note,created_at,"
                            "updated_at) VALUES(?,?,?,?,?)",
                            (name, p, note, db.now(), db.now())).lastrowid
        c.execute("DELETE FROM contact_group_members WHERE contact_id=?", (cid,))
        for g in group_ids or []:
            c.execute("INSERT OR IGNORE INTO contact_group_members(contact_id,"
                      "group_id) VALUES(?,?)", (cid, int(g)))
    return cid


def delete_contact(contact_id):
    try:
        cid = int(contact_id)
    except (TypeError, ValueError):
        raise Invalid("شناسه مخاطب نامعتبر است")
    if not db.ex("DELETE FROM contacts WHERE id=?", (cid,)).rowcount:
        raise Invalid("مخاطب پیدا نشد")
    return cid


def import_contacts(rows):
    """ایمپورت اتمیک دفترچه تلفن؛ شماره‌ی تکراری به‌روزرسانی می‌شود."""
    if not isinstance(rows, list) or not rows:
        raise Invalid("فایل مخاطبین خالی است")
    if len(rows) > 5000:
        raise Invalid("در هر بار حداکثر ۵۰۰۰ شماره قابل ایمپورت است")
    clean = []
    for pos, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise Invalid("ردیف %d ساختار معتبری ندارد" % pos)
        name = str(row.get("name", "")).strip()
        if not name or len(name) > 64:
            raise Invalid("نام ردیف %d خالی یا بیش از حد بلند است" % pos)
        try:
            phone = clean_destination(row.get("phone"))
        except Invalid as e:
            raise Invalid("ردیف %d: %s" % (pos, e))
        clean.append((name, phone, str(row.get("note", "")).strip()[:200]))
    created = updated = 0
    with db.tx() as c:
        for name, phone, note in clean:
            old = c.execute("SELECT id FROM contacts WHERE phone=?", (phone,)).fetchone()
            if old:
                c.execute("UPDATE contacts SET name=?,note=?,updated_at=? WHERE id=?",
                          (name, note, db.now(), old["id"]))
                updated += 1
            else:
                c.execute("INSERT INTO contacts(name,phone,note,created_at,updated_at) "
                          "VALUES(?,?,?,?,?)", (name, phone, note, db.now(), db.now()))
                created += 1
    return {"created": created, "updated": updated, "total": len(clean)}


def parse_contacts_csv(text):
    """تبدیل CSV استاندارد و Google Contacts به ورودی دفترچه تلفن.

    Google Contacts شماره را در ستون‌های ``Phone N - Value`` نگه می‌دارد؛
    هر شماره یک ردیف مستقل می‌شود تا هیچ شماره‌ی دوم/سومی جا نماند.
    """
    if not isinstance(text, str) or not text.strip():
        raise Invalid("فایل CSV خالی است")
    if len(text.encode("utf-8")) > 1500 * 1024:
        raise Invalid("حجم فایل CSV بیش از حد مجاز است")
    text = text.lstrip("\ufeff")
    try:
        sample = text[:8192]
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    try:
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        raw_headers = reader.fieldnames or []
        headers = {str(h).strip().lower(): h for h in raw_headers if h is not None}
        if not headers:
            raise Invalid("سطر عنوان CSV پیدا نشد")

        def header(*names):
            for name in names:
                if name in headers:
                    return headers[name]
            return None

        google_phones = []
        for normalized, original in headers.items():
            match = re.fullmatch(r"phone\s+(\d+)\s*-\s*value", normalized)
            if match:
                google_phones.append((int(match.group(1)), original))
        google_phones.sort()
        simple_phone = header("phone", "mobile", "number", "شماره", "موبایل")
        phone_headers = [h for _, h in google_phones] or ([simple_phone] if simple_phone else [])
        if not phone_headers:
            raise Invalid("ستون شماره پیدا نشد؛ فایل Google Contacts یا ستون phone لازم است")

        full_name = header("name", "fullname", "full name", "نام")
        given = header("given name", "first name", "نام کوچک")
        middle = header("additional name", "middle name", "نام میانی")
        family = header("family name", "last name", "surname", "نام خانوادگی")
        nickname = header("nickname", "نام مستعار")
        organization = header("organization 1 - name", "organization", "company")
        note_header = header("notes", "note", "یادداشت")
        if not any((full_name, given, family, nickname, organization)):
            raise Invalid("ستون نام مخاطب پیدا نشد")

        contacts, skipped = [], 0
        for row in reader:
            if not row or not any(str(v or "").strip() for v in row.values()):
                continue
            name = str(row.get(full_name, "") or "").strip() if full_name else ""
            if not name:
                name = " ".join(str(row.get(h, "") or "").strip()
                                for h in (given, middle, family) if h).strip()
            if not name:
                for h in (nickname, organization):
                    if h and str(row.get(h, "") or "").strip():
                        name = str(row.get(h, "")).strip()
                        break
            note = str(row.get(note_header, "") or "").strip() if note_header else ""
            phones = []
            for h in phone_headers:
                value = str(row.get(h, "") or "").strip()
                # Google Contacts گاهی چند مقدار یک ستون را با ::: جدا
                # می‌کند (مثلاً دو شماره‌ی Phone 1 - Value). هر کدام باید
                # یک شماره‌ی مستقل برای همان مخاطب شوند.
                for phone in re.split(r"\s*:::\s*", value):
                    phone = phone.strip()
                    if phone and phone not in phones:
                        phones.append(phone)
            if not phones:
                skipped += 1
                continue
            if not name:
                name = phones[0]
            contacts.extend({"name": name, "phone": phone, "note": note}
                            for phone in phones)
        if not contacts:
            raise Invalid("هیچ شماره‌ای در فایل CSV پیدا نشد")
        if len(contacts) > 5000:
            raise Invalid("فایل بیش از ۵۰۰۰ شماره دارد")
        kind = "google" if google_phones else "standard"
        return {"contacts": contacts, "skipped": skipped, "format": kind}
    except (csv.Error, UnicodeError) as e:
        raise Invalid("ساختار CSV نامعتبر است: %s" % e)


def list_groups():
    return [{"id": g["id"], "name": g["name"], "color": g["color"],
             "count": db.q1("SELECT COUNT(*) c FROM contact_group_members "
                            "WHERE group_id=?", (g["id"],))["c"]}
            for g in db.q("SELECT * FROM contact_groups ORDER BY name")]


def group_phones(group_id):
    return [r["phone"] for r in db.q(
        "SELECT c.phone FROM contacts c JOIN contact_group_members gm "
        "ON gm.contact_id=c.id WHERE gm.group_id=? ORDER BY c.name", (group_id,))]


# ------------------------------------------------------------------- قالب‌ها
def list_templates():
    return [{"id": t["id"], "name": t["name"], "content": t["content"],
             "created_at": t["created_at"]}
            for t in db.q("SELECT * FROM templates ORDER BY name")]


def upsert_template(tid, name, content, user_id=None):
    name = (name or "").strip()
    if not name or len(name) > 64:
        raise Invalid("نام قالب لازم است (حداکثر ۶۴ کاراکتر)")
    content = validate_text(content)
    clash = db.q1("SELECT id FROM templates WHERE name=?", (name,))
    if clash and clash["id"] != (tid or 0):
        raise Invalid("قالبی با این نام وجود دارد")
    if tid:
        if not db.q1("SELECT 1 FROM templates WHERE id=?", (tid,)):
            raise Invalid("قالب پیدا نشد")
        db.ex("UPDATE templates SET name=?,content=? WHERE id=?",
              (name, content, tid))
        return tid
    return db.insert("INSERT INTO templates(name,content,created_at,user_id) "
                     "VALUES(?,?,?,?)", (name, content, db.now(), user_id))


# ------------------------------------------------------------- پاک‌سازی داده
WIPE_SCOPES = ("messages", "logs", "all")


def wipe(scope, user_id=None):
    """پاک‌سازی انبوه؛ سه سطح کاملاً جدا:

      • `messages`: متن همه‌ی پیام‌ها و تاریخچه‌ی صف پاک می‌شود، ولی ردیف‌ها
        می‌مانند تا اعداد آمار، نمودار و تقویم دست‌نخورده باشند.
      • `logs`: فقط گزارش فعالیت‌ها.
      • `all`: همه‌چیز، از جمله آمار. مخاطبین و کاربران می‌مانند.

    پیام‌های منتظر ارسال در `messages` حفظ می‌شوند؛ هنوز فرستاده نشده‌اند.
    خروجی: تعداد ردیف‌های متأثر برای ثبت در گزارش.
    """
    if scope not in WIPE_SCOPES:
        raise Invalid("نوع پاک‌سازی نامعتبر است")
    out = {"messages": 0, "queue": 0, "logs": 0}
    with db.tx() as c:
        if scope == "messages":
            out["messages"] = c.execute(
                "UPDATE messages SET content='',deleted_at=COALESCE(deleted_at,?),"
                "deleted_by=COALESCE(deleted_by,?) WHERE status NOT IN ('queued','sending') "
                "AND content<>''", (db.now(), user_id)).rowcount
            out["queue"] = c.execute("DELETE FROM queue WHERE status NOT IN "
                                     "('queued','sending')").rowcount
        if scope == "logs":
            out["logs"] = c.execute("DELETE FROM audit_log").rowcount
        if scope == "all":
            out["queue"] = c.execute("DELETE FROM queue").rowcount
            out["messages"] = c.execute("DELETE FROM messages").rowcount
            out["logs"] = c.execute("DELETE FROM audit_log").rowcount
    try:
        # بدون این، متن پاک‌شده تا مدتی در فایل دیتابیس قابل بازیابی است
        db.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        db.conn().execute("VACUUM")
    except Exception:
        pass        # پاک‌سازی انجام شده؛ فشرده‌سازی در اجرای بعد هم ممکن است
    return out


def export_all():
    """خروجی کامل برای نگهداری قبل از پاک‌سازی."""
    def rows(sql):
        return [dict(r) for r in db.q(sql)]
    return {"exported_at": db.now(),
            "messages": rows("SELECT id,direction,phone,content,status,error,created_at,"
                             "sent_at,deleted_at FROM messages ORDER BY id"),
            "contacts": rows("SELECT id,name,phone,note,created_at FROM contacts ORDER BY id"),
            "audit_log": rows("SELECT id,actor,action,detail,ip,created_at FROM audit_log "
                              "ORDER BY id")}


# ------------------------------------------------------- داشبورد و گزارش
def dashboard_stats():
    """آمار از داده‌ی محلی — بدون تماس با مودم."""
    day = db.now() - 86400
    week = db.now() - 7 * 86400

    def n(sql, args=()):
        return db.q1("SELECT COUNT(*) c FROM messages WHERE " + sql, args)["c"]

    return {
        "total_in": n("direction='in'"),
        "total_out": n("direction='out'"),
        "in_24h": n("direction='in' AND created_at>=?", (day,)),
        "out_24h": n("direction='out' AND created_at>=?", (day,)),
        "out_7d": n("direction='out' AND created_at>=?", (week,)),
        "failed_7d": n("direction='out' AND status='failed' AND created_at>=?", (week,)),
        "unread": unread_total(),
        "conversations": db.q1("SELECT COUNT(DISTINCT phone) c FROM messages "
                               "WHERE deleted_at IS NULL")["c"],
        "contacts": db.q1("SELECT COUNT(*) c FROM contacts")["c"],
        "templates": db.q1("SELECT COUNT(*) c FROM templates")["c"],
        "latest_message_id": db.q1(
            "SELECT COALESCE(MAX(id),0) n FROM messages")["n"],
    }


def live_state(cursor=0, previous_state=""):
    """نشانگر رخدادمحور برای long-poll؛ timeout به‌تنهایی UI را رندر نمی‌کند."""
    latest = db.q1("SELECT COALESCE(MAX(id),0) n FROM messages")["n"]
    deleted = db.q1("SELECT COUNT(*) c,COALESCE(MAX(deleted_at),0) m FROM messages "
                    "WHERE deleted_at IS NOT NULL")
    queue = db.q1(
        "SELECT COUNT(*) c," 
        "SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) queued," 
        "SUM(CASE WHEN status='sending' THEN 1 ELSE 0 END) sending," 
        "SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) sent," 
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed," 
        "SUM(CASE WHEN status='canceled' THEN 1 ELSE 0 END) canceled," 
        "COALESCE(SUM(attempts),0) attempts FROM queue")
    contacts = db.q1("SELECT COUNT(*) c,COALESCE(MAX(updated_at),0) m FROM contacts")
    audit_id = db.q1("SELECT COALESCE(MAX(id),0) n FROM audit_log")["n"]
    message_sig = "%d-%d-%d" % (latest, deleted["c"], deleted["m"])
    queue_sig = "-".join(str(queue[k] or 0) for k in
                         ("c", "queued", "sending", "sent", "failed", "canceled", "attempts"))
    contact_sig = "%d-%d" % (contacts["c"], contacts["m"])
    parts = previous_state.split("|") if previous_state else []
    old = parts + [""] * (4 - len(parts))
    state = "|".join((message_sig, queue_sig, contact_sig, str(audit_id)))
    message_changed = message_sig != old[0]
    queue_changed = queue_sig != old[1]
    contacts_changed = contact_sig != old[2]
    audit_changed = str(audit_id) != old[3]
    incoming = None
    if latest > cursor:
        row = db.q1("SELECT * FROM messages WHERE direction='in' AND deleted_at IS NULL AND id>? "
                    "ORDER BY id DESC LIMIT 1", (cursor,))
        if row:
            incoming = message_row(row, contact_names([row["phone"]]))
    return {"cursor": latest, "state": state,
            "changed": any((message_changed, queue_changed,
                            contacts_changed, audit_changed)),
            "message_changed": message_changed,
            "queue_changed": queue_changed,
            "contacts_changed": contacts_changed,
            "audit_changed": audit_changed,
            "unread": unread_total(), "incoming": incoming}


def report(days=14):
    """گزارش ساده: تفکیک روزانه + پرترافیک‌ترین شماره‌ها + نرخ موفقیت."""
    days = max(1, min(int(days), 366))
    since = db.now() - days * 86400
    daily = db.q(
        "SELECT date(created_at,'unixepoch','localtime') AS d, direction, "
        "  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
        "  COUNT(*) AS n FROM messages WHERE created_at>=? "
        "GROUP BY d, direction ORDER BY d", (since,))
    by_day = {}
    for r in daily:
        e = by_day.setdefault(r["d"], {"date": r["d"], "in": 0, "out": 0, "failed": 0})
        e[r["direction"]] = r["n"]
        e["failed"] += r["failed"]
    top = db.q(
        "SELECT phone, COUNT(*) n, "
        "  SUM(CASE WHEN direction='out' THEN 1 ELSE 0 END) AS out_n "
        "FROM messages WHERE created_at>=? GROUP BY phone "
        "ORDER BY n DESC LIMIT 10", (since,))
    names = contact_names([r["phone"] for r in top])
    sent = db.q1("SELECT COUNT(*) c FROM messages WHERE direction='out' "
                 "AND status='sent' AND created_at>=?", (since,))["c"]
    failed = db.q1("SELECT COUNT(*) c FROM messages WHERE direction='out' "
                   "AND status='failed' AND created_at>=?", (since,))["c"]
    # روزهای بدون ترافیک هم برگردند تا تقویم و نمودار محور زمانی واقعی و
    # پیوسته داشته باشند (نه فقط «آخرین ۱۴ روزی که پیام داشته‌اند»).
    today = time.localtime()
    midnight = int(time.mktime((today.tm_year, today.tm_mon, today.tm_mday,
                                0, 0, 0, 0, 0, -1)))
    complete = []
    for offset in range(days - 1, -1, -1):
        key = time.strftime("%Y-%m-%d", time.localtime(midnight - offset * 86400))
        complete.append(by_day.get(key, {"date": key, "in": 0, "out": 0,
                                         "failed": 0}))
    return {"days": days,
            "daily": complete,
            "top": [{"phone": r["phone"], "name": names.get(r["phone"], ""),
                     "total": r["n"], "out": r["out_n"]} for r in top],
            "sent": sent, "failed": failed,
            "success_rate": round(100.0 * sent / (sent + failed), 1)
                            if (sent + failed) else None}
