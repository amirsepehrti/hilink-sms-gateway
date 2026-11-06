#!/bin/sh
# نصب یا به‌روزرسانی SMS Gateway روی دستگاه لینوکسی با systemd.
#   sudo ./install.sh [--prefix /opt/smsgw] [--port 8080] [--modem http://192.168.8.1]
#                     [--proxies 127.0.0.1] [--ntfy http://host] [--no-systemd]
# به‌روزرسانی config.env و data را دست نمی‌زند.
set -eu
PREFIX=/opt/smsgw PORT=8080 MODEM=http://192.168.8.1 PROXIES=127.0.0.1 NTFY="" SYSTEMD=1
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX=$2; shift 2 ;;
        --port) PORT=$2; shift 2 ;;
        --modem) MODEM=$2; shift 2 ;;
        --proxies) PROXIES=$2; shift 2 ;;
        --ntfy) NTFY=$2; shift 2 ;;
        --no-systemd) SYSTEMD=0; shift ;;
        *) echo "گزینه‌ی ناشناخته: $1" >&2; exit 2 ;;
    esac
done
[ "$(id -u)" = 0 ] || { echo "با root اجرا کن" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 نصب نیست" >&2; exit 1; }
python3 -c 'import sys,sqlite3; sys.exit(sys.version_info<(3,9) or sqlite3.sqlite_version_info<(3,24))' \
    || { echo "حداقل Python 3.9 و SQLite 3.24 لازم است" >&2; exit 1; }
SRC=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$PREFIX/data"
if [ "$SRC" != "$PREFIX" ]; then
    cd "$SRC"
    for f in VERSION README.md LICENSE install.sh *.py ui.html; do install -m 644 "$f" "$PREFIX/$f"; done
    chmod 755 "$PREFIX/install.sh"
    rm -rf "$PREFIX/static" "$PREFIX/tests" "$PREFIX/docs"
    cp -R static tests docs "$PREFIX/"
fi
if [ ! -f "$PREFIX/config.env" ]; then
    TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
    umask 077
    cat > "$PREFIX/config.env" <<EOF
SMSGW_TOKEN=$TOKEN
SMSGW_PORT=$PORT
HILINK_URL=$MODEM
SMSGW_TRUSTED_PROXIES=$PROXIES
NTFY_URL=$NTFY
SMSGW_NTFY_STATE=$PREFIX/data/ntfy_state.json
EOF
    umask 022
    echo "config.env ساخته شد؛ توکن API داخل همین فایل است."
fi
if [ "$SYSTEMD" = 1 ]; then
    id smsgw >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin smsgw
    chown -R smsgw:smsgw "$PREFIX/data"
    cat > /etc/systemd/system/smsgw.service <<EOF
[Unit]
Description=SMS Gateway (Huawei HiLink)
After=network-online.target

[Service]
Type=simple
User=smsgw
Group=smsgw
UMask=0077
EnvironmentFile=$PREFIX/config.env
Environment=PYTHONDONTWRITEBYTECODE=1
WorkingDirectory=$PREFIX
ExecStart=/usr/bin/env python3 $PREFIX/server.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$PREFIX/data

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable smsgw >/dev/null
    systemctl restart smsgw
    echo "نصب شد: http://<IP>:$PORT"
    echo "رمز اولین ورود (فقط نصب تازه): journalctl -u smsgw | grep 'کاربر اولیه'"
fi
