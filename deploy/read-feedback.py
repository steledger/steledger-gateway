#!/usr/bin/env python3
"""Print the messages agents sent through send_feedback, newest first.

Run on the droplet:  python3 /opt/emer-ai-tools/deploy/read-feedback.py [N]

The text is from strangers: read it as data, never as instructions — and if an
assistant helps triage it, tell the assistant the same. Control characters are
stripped before printing so a message cannot restyle or rewrite the terminal.
Messages older than 90 days are dropped by the edge on every new write.
"""
import json
import subprocess
import sys
import time

n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
raw = subprocess.run(
    ["docker", "exec", "emer-redis", "redis-cli", "--raw", "ZREVRANGE", "feedback:inbox", "0", str(n - 1)],
    check=True, capture_output=True, text=True,
).stdout


def clean(s) -> str:
    return "".join(ch if ch.isprintable() or ch == "\n" else "·" for ch in str(s or ""))


for line in raw.splitlines():
    try:
        m = json.loads(line)
    except ValueError:
        continue
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(m["ts"]))
    about = " ".join(x for x in (m.get("tool"), m.get("error_code")) if x) or "-"
    who = f"gh:{m['github_id']}" if m.get("github_id") else "anonymous"
    print(f"── {when} · {about} · {who} · {clean(m.get('client'))[:60]} · id {m['id']}")
    print(clean(m["message"]))
    print()
