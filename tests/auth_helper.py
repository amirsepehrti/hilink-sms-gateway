"""ساخت کاربر در دیتابیس موقتِ نمونه‌ی تست (نه دیتابیس واقعی)."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auth


def add_user(conn, username, password, role="viewer", must_change=0):
    h, salt = auth.hash_password(password)
    conn.execute("INSERT INTO users(username,password_hash,salt,role,display_name,enabled,"
                 "must_change_pw,created_at) VALUES(?,?,?,?,?,1,?,?)",
                 (username, h, salt, role, username, must_change, int(time.time())))
    conn.commit()
