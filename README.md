<div align="center">

# 📨 Khaneh-Payam — HiLink SMS Gateway

**A lightweight, self-hosted SMS gateway for Huawei HiLink USB modems.**
REST API · send queue · interactive USSD · contacts · bilingual installable web panel

[![Release](https://img.shields.io/github/v/release/amirsepehrti/hilink-sms-gateway?style=flat-square)](https://github.com/amirsepehrti/hilink-sms-gateway/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white)](#requirements)
[![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen?style=flat-square)](#why-stdlib-only)
[![Tested on E303](https://img.shields.io/badge/tested-Huawei%20E303-red?style=flat-square)](#compatible-modems)

**English** · [فارسی](README.fa.md)

</div>

---

Turn a cheap Huawei USB stick and a SIM card into a reliable SMS endpoint for your homelab or
office: send alerts when the internet is down, receive OTPs and operator messages, and run USSD
codes from a browser. Pure Python standard library, so there is no `pip`, no Docker, and nothing
to break on the next upgrade.

## ✨ Features

| | |
|---|---|
| 📤 **Send** | REST API with a persistent queue, rate spacing, and `Idempotency-Key` support |
| 📥 **Receive** | Inbox polling with multipart (long) SMS reassembly, plus optional [ntfy](https://ntfy.sh) push notifications |
| 📟 **USSD** | Step-by-step interactive menus with *continue* and *end* controls |
| 👥 **Contacts** | Address book with conversation view and a paginated archive |
| 🧹 **Modem storage care** | Auto-cleanup from 80% down to 60%, warning at 90%, capacity shown in `/status` |
| 🔐 **Security** | Roles (`admin` / `operator` / `viewer`), scoped API tokens, scrypt passwords, CSRF, security headers |
| 🌐 **Web panel** | Installable PWA, Persian (RTL) and English (LTR), light and dark themes, mobile-friendly |
| 📖 **API docs** | OpenAPI spec at `/api/v1/openapi.json`, unauthenticated `/health` for monitoring |

## 📡 Compatible modems

| Status | Modem | Notes |
|:---:|---|---|
| ✅ | **Huawei E303** in HiLink mode, firmware `22.318.27.00.00` | Send, receive, multipart SMS and USSD tested on real hardware |
| ⚠️ | Older Huawei HiLink modems | May work if both checks below pass. Untested |
| ❌ | Modems that only expose `/api/webserver/SesTokInfo` or require a login | Includes many newer firmwares, such as some E3372. The client does not support cookie sessions yet |
| ❌ | Stick mode (serial port + AT commands) | This project talks to the HiLink web API only |

> [!NOTE]
> The model printed on the stick is not enough: **the firmware decides**. `lsusb` can also report
> the wrong model.

### Check your modem before installing

```bash
MODEM=http://192.168.8.1
T=$(curl -s $MODEM/api/webserver/token | sed -n 's:.*<token>\(.*\)</token>.*:\1:p')
echo "token: $T"
curl -s -H "__RequestVerificationToken: $T" $MODEM/api/sms/sms-count
```

Your modem is compatible if the first command prints a numeric token and the second output
contains `<LocalMax>`. If the token is empty or the response contains `<error>`, this version
will not work with your modem.

## 🧰 Requirements

- Linux with systemd (tested on Debian with Python 3.13; minimum Python 3.9 and SQLite 3.24)
- A HiLink modem with an active, SMS-capable SIM card
- **SIM PIN lock disabled**
- The modem visible as a network interface, with the host holding an address on it (usually `192.168.8.100/24`)

> [!TIP]
> Running inside a container or VM? Pass the modem's USB network interface through to it. If the
> address disappears after a reboot, make it persistent on that interface.

## 🚀 Installation

Download the latest `smsgw-*.tar.gz` from [Releases](https://github.com/amirsepehrti/hilink-sms-gateway/releases), then:

```bash
tar xzf smsgw-*.tar.gz && cd smsgw-*/
sudo ./install.sh --modem http://192.168.8.1 --port 8080
journalctl -u smsgw | grep 'کاربر اولیه'      # initial admin password
```

Open `http://<IP>:8080`, sign in as `admin`, and change the password.

| Option | Purpose |
|---|---|
| `--modem` | Modem address |
| `--port` | Service port |
| `--proxies` | Reverse-proxy IPs, comma-separated |
| `--ntfy` | ntfy server URL for new-message notifications (optional) |
| `--prefix` | Install path, default `/opt/smsgw` |
| `--no-systemd` | Copy files and create the config only |

The package contains no messages, users or tokens; fresh secrets are generated on every install.
Settings live in `config.env`. To upgrade, run the new version's `install.sh`; `config.env` and
`data/` are left untouched.

## 📤 Sending SMS via the API

Create a token with the least privileges you need in the panel's **API** section, then:

```bash
curl -X POST http://<IP>:8080/api/v1/messages \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Idempotency-Key: alert-001" \
  -H "Content-Type: application/json" \
  -d '{"to":"09120000000","text":"Hello"}'
```

Anything that can make an HTTP POST can use it, for example a Zabbix webhook media type for
alerts that still arrive when your internet link is down.

## ⚠️ Things to know

- **Modem storage is small.** The E303 holds 500 messages, and when it is full, incoming SMS
  **stop silently**. The service cleans up automatically from 80% to 60% and warns at 90%.
- **There are no delivery reports.** `OK` means the modem accepted the message, not that it reached the recipient.
- **USSD codes are operator-specific.** Some menu options make purchases; be sure before you pick one.
- **Sends are spaced out.** The default is 3 seconds, so the modem doesn't return busy errors.

## 🔒 Security

- The modem API has **no authentication**. Never route the modem network or expose it to the internet.
- Put the service behind HTTPS and declare your proxy IPs with `--proxies`.
- Firewall the service port so only your proxy and monitoring hosts can reach it.

## 🩺 Troubleshooting

| Symptom | What to do |
|---|---|
| "Modem unreachable" | Check `ip addr`; the host needs an address on the modem network and `curl $MODEM/api/webserver/token` must answer |
| No incoming SMS | Check capacity in the dashboard or `/status`; full modem storage is the most common cause |
| Error `113018` | The modem is busy; make sure no other program talks to it at the same time |
| `/health` returns `503` | The modem loop has stopped; check `journalctl -u smsgw` |
| USSD timeout | The code isn't valid for your operator or SIM, or the network didn't answer |

## 🛠️ Development & tests

```bash
cd tests && python3 offline_test.py      # no real modem needed: uses a fake modem and fake ntfy
```

> [!CAUTION]
> Never run send tests against a real SIM card.

Verified modem behaviour is documented in [docs/HARDWARE.md](docs/HARDWARE.md).

### Why stdlib-only?

No third-party packages means no supply-chain surprises, no virtualenv, and code that keeps
running on a stock Python years from now.

## 🤝 Contributing

Compatibility reports are the most valuable contribution. If you try another modem, open an issue
with the **model and firmware version** and whether the check above passed.

## 📄 License

[MIT](LICENSE). The Vazirmatn font is licensed under the [SIL OFL](static/fonts/OFL.txt).
