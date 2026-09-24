#!/usr/bin/env python3
"""Tell the operator when someone other than them starts using the gateway.

Run hourly by steledger-watch-users.timer on the droplet. Three signals:

  - writers: GitHub ids that own `ai:gh:<id>...` records, read straight from the
    chain (`name_filter` on the node), so it does not depend on our own logs.
    These records are public and carry the id and login, so the message names them.
  - sign-ins: how many GitHub ids have called an MCP tool while signed in (the
    `mcp:callers` set the edge keeps). Only the count is reported — the privacy
    page promises that set is a number, not a list — so a sign-in without a
    write stays anonymous here.
  - X: replies to @steledger's posts and mentions elsewhere, as at most one
    digest per run — up to five comments with links, then a count — so a burst
    of replies is still a single message an hour. Read with the app's bearer
    token, which never rotates; the posting token lives on a laptop and does.

  - errors: internal errors (bugs on our side) as soon as the hourly run sees
    them, and once a day, after UTC midnight, a digest of the day before — calls,
    refusals by tool and code, internal errors, and how sign-ins went. Read from the counters the edge
    keeps (see edge/app/stats.py). They carry no identity, so your own calls are
    counted along with everyone else's.

Your own ids (KNOWN_GITHUB_IDS) are left out of writers and sign-ins. State lives in
/var/lib/steledger-watch/state.json; a failed send leaves it untouched, so the
next run tries again.

Config, root-only, never in this repo: /etc/steledger/notify.env with
TG_BOT_TOKEN, TG_CHAT_ID, KNOWN_GITHUB_IDS (comma-separated), and for X
X_BEARER_TOKEN and X_USER_ID (leave them out to skip the X digest).

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
import time
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG = Path("/etc/steledger/notify.env")
STATE = Path("/var/lib/steledger-watch/state.json")
NODE = ["docker", "exec", "emc", "emercoin-cli",
        "-datadir=/srv/emercoind", "-conf=/srv/emercoind/emercoin.conf"]
REDIS = ["docker", "exec", "emer-redis", "redis-cli", "--raw"]
API = "https://api.steledger.com"
X_API = "https://api.x.com/2"
DIGEST_ITEMS = 5
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


def redis_hash(key: str) -> dict:
    """HGETALL through redis-cli --raw: field and value on alternate lines."""
    lines = run(REDIS + ["HGETALL", key]).splitlines()
    return {lines[i]: int(lines[i + 1]) for i in range(0, len(lines) - 1, 2)}


def internal_errors() -> int:
    return int(run(REDIS + ["GET", "mcp:errors:internal"]).strip() or 0)


def internal_digest(new: int) -> str:
    recent = [json.loads(x) for x in run(REDIS + ["LRANGE", "mcp:errors:recent", "0", "199"]).splitlines() if x]
    bugs = [e for e in recent if e.get("code") == "internal_error"][:min(new, DIGEST_ITEMS)]
    lines = [f"Steledger: {new} new internal error(s) — a bug on our side. "
             "Tracebacks are in the edge log (docker logs emer-edge)."]
    for e in bugs:
        when = time.strftime("%H:%M UTC", time.gmtime(e["ts"]))
        lines.append(f"• {when} {e['where']} ({e.get('client') or 'no client'})")
    return "\n".join(lines)


def daily_digest(day: str) -> str:
    calls = redis_hash("mcp:daily").get(day, 0)
    errors = redis_hash(f"mcp:errors:day:{day}")
    total = sum(errors.values())
    lines = [f"Steledger, {day}: {calls} MCP tool call(s); {total} refusal(s) and error(s) over MCP and REST."]
    for key, n in sorted(errors.items(), key=lambda kv: -kv[1])[:8]:
        lines.append(f"• {key} × {n}")
    bugs = sum(n for key, n in errors.items() if key.endswith(":internal_error"))
    if bugs:
        lines.append(f"{bugs} of them internal errors — bugs on our side.")
    signin = redis_hash(f"oauth:funnel:day:{day}")
    if signin:
        lines.append(
            f"Sign-in: {signin.get('authorize_started', 0)} sent to GitHub, "
            f"{signin.get('github_denied', 0)} cancelled, {signin.get('state_expired', 0)} too late, "
            f"{signin.get('github_failed', 0)} failed, {signin.get('token_issued', 0)} signed in."
        )
    lines.append(f"{API}/stats")
    return "\n".join(lines)


def x_mentions(conf: dict, since_id: str | None) -> list:
    """Posts mentioning the account since `since_id`, newest first (at most 100)."""
    params = {"max_results": 100, "tweet.fields": "in_reply_to_user_id,author_id,created_at"}
    if since_id:
        params["since_id"] = since_id
    url = f"{X_API}/users/{conf['X_USER_ID']}/mentions?{urllib.parse.urlencode(params)}"
    token = urllib.parse.unquote(conf["X_BEARER_TOKEN"])
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp).get("data", [])


def x_digest(conf: dict, posts: list) -> str | None:
    me = conf["X_USER_ID"]
    posts = [p for p in posts if p.get("author_id") != me]
    comments = [p for p in posts if p.get("in_reply_to_user_id") == me]
    elsewhere = len(posts) - len(comments)
    if not posts:
        return None
    lines = []
    if comments:
        lines.append(f"Steledger on X: {len(comments)} new comment(s) on our posts.")
        for p in comments[:DIGEST_ITEMS]:
            text = " ".join(p["text"].split())
            lines.append(f"• {text[:140]}{'…' if len(text) > 140 else ''}\n  https://x.com/i/status/{p['id']}")
        if len(comments) > DIGEST_ITEMS:
            lines.append(f"…and {len(comments) - DIGEST_ITEMS} more.")
    if elsewhere:
        lines.append(f"{'Also m' if comments else 'Steledger on X: m'}entioned in {elsewhere} "
                     f"other post(s): https://x.com/notifications/mentions")
    return "\n".join(lines)


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
    # "x" is absent until the X digest has run once; then {"since_id": newest seen or None}.
    # "internal" and "digest_day" likewise start quietly on their first run.
    today = time.strftime("%Y-%m-%d", time.gmtime())
    now_internal = internal_errors()
    new_state = {"writers": sorted(now_writers), "signed_in": now_signed_in, "x": state.get("x"),
                 "internal": now_internal, "digest_day": today}

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

    if "internal" in state and now_internal > state["internal"]:
        messages.append(internal_digest(now_internal - state["internal"]))
    if "digest_day" in state and state["digest_day"] != today:
        messages.append(daily_digest(state["digest_day"]))

    if conf.get("X_BEARER_TOKEN") and conf.get("X_USER_ID"):
        seen = state.get("x")
        try:
            posts = x_mentions(conf, seen["since_id"] if seen else None)
        except (OSError, ValueError) as exc:  # X down or refusing: the rest still runs
            print(f"X mentions unavailable: {exc}", file=sys.stderr)
        else:
            newest = posts[0]["id"] if posts else (seen or {}).get("since_id")
            new_state["x"] = {"since_id": newest}
            # First X run: record where we are and say nothing, like the baseline above.
            if seen is not None and (digest := x_digest(conf, posts)):
                messages.append(digest)

    for text in messages:
        send(conf, text)  # raises before the state is saved, so a failure is retried
    STATE.write_text(json.dumps(new_state))


if __name__ == "__main__":
    main()
