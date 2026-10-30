"""Start the real arq worker for the smoke run.

Run from the repo root so `app` and `worker` import (same as `python worker.py`).

SMOKE_ALLOW_LOOPBACK=1 relaxes ONE rule of the outbound-webhook SSRF guard for this process:
127.0.0.1 is treated as routable, so the Slack/CRM jobs can POST to the local stand-in receiver.
Link-local (169.254.x), private ranges and everything else stay blocked, and the guard is
exercised unrelaxed in the run (see captures 70-72). Without that env var this is exactly
`python worker.py`.
"""

import os
import sys

sys.path.insert(0, os.getcwd())

if os.environ.get("SMOKE_ALLOW_LOOPBACK") == "1":
    from app.utils import network_security as ns

    _orig = ns._is_disallowed_ip
    ns._is_disallowed_ip = lambda ip: False if ip.split("%", 1)[0] == "127.0.0.1" else _orig(ip)
    print("[smoke] loopback relaxed for the worker's outbound guard (127.0.0.1 only)", flush=True)

from worker import main  # noqa: E402

main()
