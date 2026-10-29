"""Fixture tests for scripts/whatsapp_sidecar_snapshot.sh, driven through a fake `docker` on PATH.

The real in-container probe is also executed (with bash, over /dev/tcp) against a local stub that enforces the bearer token,
to prove the token never leaves the "container" side.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SNAP = REPO / "scripts" / "whatsapp_sidecar_snapshot.sh"
TOKEN = "TOKEN-" + "k" * 40

FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_ARGV_LOG"
case "$1" in
  exec)
    if [ -n "${FAKE_EXEC_REAL:-}" ]; then
      shift; shift   # exec <container>
      exec "$@"
    fi
    [ -f "${FAKE_EXEC_OUT:-/nonexistent}" ] && cat "$FAKE_EXEC_OUT"
    exit "${FAKE_EXEC_RC:-0}" ;;
  inspect) echo "${FAKE_RUNNING:-true}" ;;
  logs) [ -f "${FAKE_LOGS:-/nonexistent}" ] && cat "$FAKE_LOGS"; exit 0 ;;
esac
"""

OK_OUT = 'cur 93323264\nanon 71827456\nhttp 200 {"sessions":2,"max_sessions":3,"rss_mb":139.4,"rss_refuse_mb":216}\n'


@pytest.fixture
def rig(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    log = tmp_path / "logs" / "snap.csv"

    def run(
        *,
        exec_out: str | None = OK_OUT,
        exec_rc: int = 0,
        running: str = "true",
        logs: str = "",
        extra_env=None,
        args=(),
    ):
        env = {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "FAKE_ARGV_LOG": str(tmp_path / "argv.log"),
            "FAKE_EXEC_RC": str(exec_rc),
            "FAKE_RUNNING": running,
            "WA_SNAPSHOT_LOG": str(log),
            "WHATSAPP_SIDECAR_TOKEN": TOKEN,  # present in the caller's env on purpose: it must never be read or printed
        }
        if exec_out is not None:
            (tmp_path / "exec.out").write_text(exec_out)
            env["FAKE_EXEC_OUT"] = str(tmp_path / "exec.out")
        (tmp_path / "logs.txt").write_text(logs)
        env["FAKE_LOGS"] = str(tmp_path / "logs.txt")
        env.update(extra_env or {})
        res = subprocess.run(
            ["bash", str(SNAP), *args], capture_output=True, text=True, env=env, cwd=tmp_path
        )
        assert TOKEN not in res.stdout + res.stderr
        argv = (tmp_path / "argv.log").read_text() if (tmp_path / "argv.log").exists() else ""
        assert TOKEN not in argv
        return res

    return run, log


def rows(log: Path) -> list[list[str]]:
    return [line.split(",") for line in log.read_text().splitlines()]


HEADER = ["utc", "mem_current_b", "anon_b", "sessions", "rss_mb", "socket_closed_5m", "err"]


def test_healthy_line(rig):
    run, log = rig
    res = run(logs='{"msg":"socket closed"}\n{"msg":"other"}\n{"msg":"socket closed"}\n')
    assert res.returncode == 0 and res.stdout == "" and res.stderr == ""
    header, row = rows(log)
    assert header == HEADER
    assert row[1:] == ["93323264", "71827456", "2", "139.4", "2", ""]
    assert row[0].endswith("Z") and len(row[0]) == 20


def test_header_written_once_and_lines_append(rig):
    run, log = rig
    run()
    run()
    lines = log.read_text().splitlines()
    assert lines[0].split(",") == HEADER and len(lines) == 3


def test_container_down_still_writes_a_line_and_counts_logs(rig):
    run, log = rig
    res = run(exec_out=None, exec_rc=1, running="false", logs="socket closed\n")
    assert res.returncode == 0
    row = rows(log)[1]
    assert row[1:] == ["", "", "", "", "1", "container_down"]


def test_exec_failure_while_running(rig):
    run, log = rig
    run(exec_out=None, exec_rc=1, running="true")
    assert rows(log)[1][-1] == "exec_failed"


def test_health_refused_keeps_the_memory_columns(rig):
    run, log = rig
    run(exec_out='cur 100\nanon 50\nhttp 401 {"error":"unauthorized"}\n')
    assert rows(log)[1][1:] == ["100", "50", "", "", "0", "health_401"]


def test_health_body_with_spaces_still_parses(rig):
    run, log = rig
    run(exec_out='cur 1\nanon 1\nhttp 200 {"sessions": 1, "rss_mb": 120.5}\n')
    assert rows(log)[1][3:5] == ["1", "120.5"]


def test_unparsable_health_body(rig):
    run, log = rig
    run(exec_out="cur 100\nanon 50\nhttp 200 <html>\n")
    assert rows(log)[1][-1] == "health_unparsable"


def test_cgroup_unreadable(rig):
    run, log = rig
    run(exec_out='http 200 {"sessions":0,"max_sessions":3,"rss_mb":99.0,"rss_refuse_mb":216}\n')
    row = rows(log)[1]
    assert row[3:5] == ["0", "99.0"] and row[-1] == "cgroup_unreadable"


def test_no_http_line_at_all(rig):
    run, log = rig
    run(exec_out="cur 100\nanon 50\n")
    assert rows(log)[1][-1] == "exec_failed"


def test_rotation_by_size_keeps_two_and_bounds_disk(rig):
    run, log = rig
    env = {"WA_SNAPSHOT_MAX_BYTES": "300", "WA_SNAPSHOT_KEEP": "2"}
    for _ in range(40):
        assert run(extra_env=env).returncode == 0
    files = sorted(p.name for p in log.parent.iterdir())
    assert files == ["snap.csv", "snap.csv.1", "snap.csv.2"]
    assert sum(p.stat().st_size for p in log.parent.iterdir()) < 3 * 400
    for p in log.parent.iterdir():
        assert (
            p.read_text().splitlines()[0].split(",") == HEADER
        )  # every file starts with the header


def test_keep_zero_just_truncates(rig):
    run, log = rig
    env = {"WA_SNAPSHOT_MAX_BYTES": "200", "WA_SNAPSHOT_KEEP": "0"}
    for _ in range(10):
        run(extra_env=env)
    assert [p.name for p in log.parent.iterdir()] == ["snap.csv"]


def test_one_month_of_lines_is_under_one_megabyte(rig):
    run, log = rig
    run()
    line_bytes = len(log.read_text().splitlines()[1]) + 1
    assert line_bytes * 288 * 31 < 1_000_000  # 288 runs/day x 31 days at the realistic line length


def test_unwritable_log_dir_is_the_only_failure(rig):
    run, log = rig
    log.parent.mkdir()
    log.parent.chmod(0o500)
    try:
        if os.access(log.parent, os.W_OK):
            pytest.skip("running as a user that ignores directory permissions")
        res = run()
        assert res.returncode == 1 and "cannot write" in res.stderr
    finally:
        log.parent.chmod(0o700)


@pytest.mark.parametrize("bad", ["a b", "x;rm", "$(id)", "name\nother"])
def test_container_name_is_validated(rig, bad):
    run, _ = rig
    res = run(args=(bad,))
    assert res.returncode == 1 and "invalid container name" in res.stderr


def test_default_log_is_next_to_the_app_logs(tmp_path):
    # no WA_SNAPSHOT_LOG: resolves to <script dir>/../logs/whatsapp_sidecar_snapshot.csv
    src = SNAP.read_text()
    assert "${script_dir}/../logs/whatsapp_sidecar_snapshot.csv" in src


# ------------------------------------------------------------------ real probe against a token-enforcing stub


class Stub(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        ok = self.headers.get("Authorization") == f"Bearer {TOKEN}" and self.path == "/health"
        payload = (
            {"sessions": 2, "max_sessions": 3, "rss_mb": 138.2, "rss_refuse_mb": 216}
            if ok
            else {"error": "unauthorized"}
        )
        body = json.dumps(payload, separators=(",", ":")).encode()  # like JSON.stringify
        self.send_response(200 if ok else 401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


def test_real_probe_reads_health_inside_the_container_side_and_never_leaks_the_token(rig):
    run, log = rig
    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        # The fake `docker exec` runs the probe for real with the container's env (token + port), as the container would.
        env = {"FAKE_EXEC_REAL": "1", "PORT": str(server.server_address[1])}
        res = run(exec_out=None, extra_env=env)
        assert res.returncode == 0
    finally:
        server.shutdown()
    row = rows(log)[1]
    assert row[3:5] == ["2", "138.2"]
    # the argv recorded by the fake docker holds the probe TEXT but not the token value
    assert TOKEN not in (log.parent.parent / "argv.log").read_text()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
