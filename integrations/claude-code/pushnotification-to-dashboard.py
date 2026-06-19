#!/usr/bin/env python3
"""Claude Code PostToolUse hook: mirror `PushNotification` calls to this dashboard.

Claude Code's built-in `PushNotification` tool pings the operator's terminal (and
their phone, when Remote Control is connected). This hook forwards that same
message to the e-paper message board via `POST /api/message`, so a single
`PushNotification` call lands on phone/terminal *and* the dashboard — no separate
`curl` needed for the loud, interrupt-worthy updates.

This is the "loud path". The plain `POST /api/message` REST call remains the
"quiet path" for frequent, glanceable status updates that should NOT buzz a phone.

Wiring: register as a `PostToolUse` hook matching `PushNotification` in
`~/.claude/settings.json` (see `settings.snippet.json` in this directory).

Fail-safe by design: this hook never blocks or fails a session. Any problem
(dashboard down, non-2xx, malformed input) is logged to stderr and it exits 0.

Configuration via environment variables:
  EPAPER_DASHBOARD_URL     base URL of the dashboard   (default http://192.168.50.73:8090)
  EPAPER_DASHBOARD_SENDER  sender label on the card     (default "Claude Code @ <hostname>")
"""
from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request

TIMEOUT_S = 4


def log(msg: str) -> None:
    print(f"[pushnotification-to-dashboard] {msg}", file=sys.stderr)


def main() -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        log(f"ignoring malformed hook payload: {exc}")
        return 0

    # The matcher should already scope this to PushNotification; double-check anyway.
    if event.get("tool_name") != "PushNotification":
        return 0

    message = ((event.get("tool_input") or {}).get("message") or "").strip()
    if not message:
        return 0

    base = os.environ.get("EPAPER_DASHBOARD_URL", "http://192.168.50.73:8090").rstrip("/")
    sender = os.environ.get("EPAPER_DASHBOARD_SENDER") or f"Claude Code @ {socket.gethostname()}"
    cwd = (event.get("cwd") or "").strip()
    body = f"_via PushNotification_  \n`{cwd}`" if cwd else "_via PushNotification_"

    payload = json.dumps({"sender": sender, "header": message, "body": body}).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/api/message",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            if response.status >= 300:
                log(f"dashboard returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        log(f"dashboard rejected message: HTTP {exc.code} {exc.reason}")
    except (urllib.error.URLError, OSError) as exc:
        log(f"dashboard unreachable ({base}): {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
