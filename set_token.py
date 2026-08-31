"""
Grab the gateway token from the clipboard and put it in .env.

    python token.py            # read clipboard -> .env, report time left

Saves the slowest part of the loop: opening .env, pasting, saving. Copy the
value in DevTools and run this; the token goes straight in.

The token is never printed. Only its remaining lifetime is shown.

Why this is needed at all: gateway tokens live 15 minutes and the SPA only
renews while it is actually making requests. A tab left idle holds a stale
token, so ALWAYS reload the portal page before copying.
"""

import subprocess
import sys
from pathlib import Path

from src import portal

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"


def clipboard() -> str:
    """Windows clipboard via PowerShell; falls back to a typed value."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
            capture_output=True, text=True, timeout=15)
        return out.stdout.strip()
    except Exception:
        return ""


def write_env(token: str) -> None:
    lines, seen = [], False
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8-sig").splitlines():
            if line.strip().startswith("ACCESS_TOKEN"):
                lines.append(f"ACCESS_TOKEN={token}")
                seen = True
            else:
                lines.append(line)
    if not seen:
        lines.append(f"ACCESS_TOKEN={token}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    raw = " ".join(sys.argv[1:]).strip() or clipboard()
    raw = raw.strip().strip('"').strip("'")
    while raw.lower().startswith("bearer "):
        raw = raw[7:].strip()

    if raw.count(".") != 2:
        sys.exit("FAIL: clipboard does not hold a JWT.\n"
                 "  Reload the portal page first, then in DevTools Console run:\n"
                 "    sessionStorage.token\n"
                 "  Right-click the result -> Copy string contents, then re-run this.")

    left = portal.token_seconds_left(f"Bearer {raw}")
    if left is None:
        sys.exit("FAIL: could not read the token's expiry.")
    if left <= 0:
        sys.exit(f"FAIL: that token expired {abs(left) // 60}m {abs(left) % 60}s ago.\n"
                 "  RELOAD the portal page (F5), then copy again - the SPA only\n"
                 "  renews the token while it is making requests.")

    write_env(raw)
    print(f"OK: .env updated. {left // 60}m {left % 60}s left.")
    if left < 300:
        print("  WARN: under 5 minutes. Run reconcile.py right now.")


if __name__ == "__main__":
    main()
