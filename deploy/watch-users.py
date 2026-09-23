#!/usr/bin/env python3
"""Tell the operator when someone other than them starts using the gateway.

Run hourly by steledger-watch-users.timer on the droplet. Two signals:

  - writers: GitHub ids that own `ai:gh:<id>...` records, read straight from the
    chain (`name_filter` on the node), so it does not depend on our own logs.
    These records are public and carry the id and login, so the message names them.
  - sign-ins: how many GitHub ids have called an MCP tool while signed in (the
    `mcp:callers` set the edge keeps). Only the count is reported — the privacy
    page promises that set is a number, not a list — so a sign-in without a
    write stays anonymous here.

Your own ids (KNOWN_GITHUB_IDS) are left out of both. State lives in
/var/lib/steledger-watch/state.json; a failed send leaves it untouched, so the
next run tries again.

Config, root-only, never in this repo: /etc/steledger/notify.env with
TG_BOT_TOKEN, TG_CHAT_ID and KNOWN_GITHUB_IDS (comma-separated).

    python3 watch-users.py           # check and notify
    python3 watch-users.py --test    # send a test message and the current baseline

Install (once, on the droplet):
    cp /opt/emer-ai-tools/deploy/systemd/steledger-watch-users.* /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now steledger-watch-users.timer
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG = Path("/etc/steledger/notify.env")
STATE = Path("/var/lib/steledger-watch/state.json")
NODE = ["docker", "exec", "emc", "emercoin-cli",
        "-datadir=/srv/emercoind", "-conf=/srv/emercoind/emercoin.conf"]
REDIS = ["docker", "exec", "emer-redis", "redis-cli", "--raw"]
API = "https://api.steledger.com"
NAME = re.compile(r"^ai:gh:(\d+)(?::|$)")


def load_config() -> dict:
    conf = {}
    for line in CONFIG.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            conf[key.strip()] = value.strip()
    conf["known"] = {x.strip() for x in conf.get("KNOWN_GITHUB_IDS", "").split(",") if x.strip()}
    return conf


def run(cmd: list) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120).stdout


def writers(known: set) -> dict:
    """{github_id: {"records": n, "login": str | None}} for every non-known writer."""
    out: dict = {}
    for rec in json.loads(run(NODE + ["name_filter", "^ai:gh:", "0", "0"])):
        m = NAME.match(rec["name"])
        if not m or m.group(1) in known:
            continue
        entry = out.setdefault(m.group(1), {"records": 0, "login": None})
        entry["records"] += 1
        if rec["name"] == f"ai:gh:{m.group(1)}":  # the identity record carries the login
            try:
                entry["login"] = json.loads(rec["value"]).get("github_login")
            except (ValueError, AttributeError):
                pass
    return out


def signed_in(known: set) -> int:
    total = int(run(REDIS + ["SCARD", "mcp:callers"]).strip() or 0)
    mine = sum(int(run(REDIS + ["SISMEMBER", "mcp:callers", k]).strip() or 0) for k in known)
    return total - mine


def send(conf: dict, text: str) -> None:
    data = urllib.parse.urlencode({"chat_id": conf["TG_CHAT_ID"], "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    url = f"https://api.telegram.org/bot{conf['TG_BOT_TOKEN']}/sendMessage"
    with urllib.request.urlopen(url, data=data, timeout=20) as resp:
        if resp.status != 200:
            raise RuntimeError(f"telegram answered {resp.status}")


def main() -> None:
    conf = load_config()
    now_writers = writers(conf["known"])
    now_signed_in = signed_in(conf["known"])

    if "--test" in sys.argv[1:]:
        send(conf, f"Steledger watcher: test message. Right now: {len(now_writers)} "
                   f"outside writer(s), {now_signed_in} outside signed-in account(s).")
        return

    state = json.loads(STATE.read_text()) if STATE.exists() else None
    if state is None:
        # First run: take the baseline quietly, so a reinstall does not replay history.
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"writers": sorted(now_writers), "signed_in": now_signed_in}))
        return

    messages = []
    for gid in sorted(set(now_writers) - set(state["writers"])):
        w = now_writers[gid]
        who = f"{w['login']} (github id {gid})" if w["login"] else f"github id {gid}"
        messages.append(
            f"Steledger: a new account is writing — {who}, {w['records']} record(s) on-chain.\n"
            f"{API}/nvs/ai:gh:{gid}"
        )
    if now_signed_in > state["signed_in"]:
        messages.append(
            f"Steledger: {now_signed_in - state['signed_in']} new signed-in account(s) — "
            f"{now_signed_in} outside accounts so far. No identity shown: sign-ins are counted, not listed."
        )

    for text in messages:
        send(conf, text)  # raises before the state is saved, so a failure is retried
    STATE.write_text(json.dumps({"writers": sorted(now_writers), "signed_in": now_signed_in}))


if __name__ == "__main__":
    main()
