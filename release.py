#!/usr/bin/env python3
"""ساخت بسته‌ی نصب. allowlist عمدی است: config.env و data هرگز وارد بسته نمی‌شوند.

    python3 release.py     → dist/smsgw-<نسخه>.tar.gz
"""
import io, os, tarfile

BASE = os.path.dirname(os.path.abspath(__file__))
FILES = ("VERSION", "README.md", "LICENSE", "install.sh", "auth.py", "db.py", "hilink.py", "release.py",
         "selftest.py", "sender.py", "server.py", "store.py", "ui.html")
DIRS = ("static", "tests", "docs")


def version():
    with open(os.path.join(BASE, "VERSION"), encoding="utf-8") as f:
        return f.read().strip()


def _members():
    for name in FILES:
        yield name
    for top in DIRS:
        for root, dirs, names in os.walk(os.path.join(BASE, top)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for n in sorted(names):
                if not n.endswith(".pyc"):
                    yield os.path.relpath(os.path.join(root, n), BASE)


def build():
    """بایت‌های tar.gz را برمی‌گرداند."""
    prefix = "smsgw-%s" % version()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in _members():
            info = tar.gettarinfo(os.path.join(BASE, rel), arcname=prefix + "/" + rel)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            with open(os.path.join(BASE, rel), "rb") as f:
                tar.addfile(info, f)
    return buf.getvalue()


if __name__ == "__main__":
    os.makedirs(os.path.join(BASE, "dist"), exist_ok=True)
    out = os.path.join(BASE, "dist", "smsgw-%s.tar.gz" % version())
    with open(out, "wb") as f:
        f.write(build())
    print(out)
