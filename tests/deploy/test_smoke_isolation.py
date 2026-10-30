"""The smoke's isolation checks (scripts/smoke_whatsapp_sidecar.sh, opt-in via SMOKE_ISOLATION=1).

Two layers, neither needing a daemon:
  * the pure classifiers embedded in the script's node program are extracted and run under node with fake resolver /
    socket outcomes. Why: Docker Desktop answers a name the container cannot see with ENOTFOUND, a Linux host answers with
    EAI_AGAIN (first production run, 2026-10-30); both mean "not resolvable" and the check must not depend on which.
  * the host-side wrapper is driven through a fake `docker` on PATH to prove what reaches `docker exec`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SMOKE = REPO / "scripts" / "smoke_whatsapp_sidecar.sh"
SMOKE_TEXT = SMOKE.read_text()

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def classifiers_js() -> str:
    m = re.search(
        r"// BEGIN isolation-classifiers.*?\n(.*?)// END isolation-classifiers", SMOKE_TEXT, re.S
    )
    assert m, "classifier block markers missing from the smoke script"
    return m.group(1)


def run_node(calls: list[str]) -> list[dict]:
    program = classifiers_js() + "\nconsole.log(JSON.stringify([" + ",".join(calls) + "]))\n"
    res = subprocess.run(
        ["node", "--input-type=module", "-"], input=program, capture_output=True, text=True
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


# ----------------------------------------------------------------------------- DNS classification


@needs_node
@pytest.mark.parametrize("code", ["ENOTFOUND", "EAI_AGAIN", "EAI_FAIL", "EAI_NODATA", "EAI_NONAME"])
def test_every_not_resolvable_code_counts_as_isolated(code):
    (r,) = run_node([f"classifyLookup(Object.assign(new Error('x'), {{ code: '{code}' }}))"])
    assert r["ok"] is True and r["detail"] == f"dns={code}"


@needs_node
def test_a_name_that_resolves_is_the_failure():
    (r,) = run_node(["classifyLookup(null, { address: '172.22.0.3', family: 4 })"])
    assert r["ok"] is False and "resolves" in r["detail"]
    assert "172.22" not in r["detail"]  # the address is not echoed


@needs_node
@pytest.mark.parametrize("code", ["ECONNREFUSED", "ETIMEDOUT", "ENOENT", "undefined"])
def test_an_unexpected_error_is_not_mistaken_for_isolation(code):
    err = (
        "undefined"
        if code == "undefined"
        else f"Object.assign(new Error('x'), {{ code: '{code}' }})"
    )
    (r,) = run_node([f"classifyLookup({err})"])
    assert r["ok"] is False


# ----------------------------------------------------------------------------- TCP classification


@needs_node
@pytest.mark.parametrize("code", ["ETIMEDOUT", "EHOSTUNREACH", "ENETUNREACH"])
def test_no_route_outcomes_count_as_isolated(code):
    (r,) = run_node([f"classifyConnect('{code}')"])
    assert r["ok"] is True


@needs_node
@pytest.mark.parametrize("code", ["CONNECTED", "ECONNREFUSED", "ECONNRESET", "ERROR"])
def test_anything_that_answered_is_the_failure(code):
    """A refused connection means the packet got there and something replied: a route exists."""
    (r,) = run_node([f"classifyConnect('{code}')"])
    assert r["ok"] is False


# ----------------------------------------------------------------------------- host-side wrapper

FAKE_DOCKER = r"""#!/usr/bin/env bash
log="$STUB_LOG"
printf 'ARGV' >> "$log"; for a in "$@"; do printf ' [%s]' "$a" >> "$log"; done; printf '\n' >> "$log"
case "$1" in
  inspect)
    c="${@: -1}"
    case "$*" in
      *IPAddress*)
        [ -e "$STUB_DIR/ip_$c" ] || exit 1
        cat "$STUB_DIR/ip_$c" ;;
      *) echo "luran_wa_redis luran_wa_backend " ;;
    esac ;;
  exec)
    if [ "${@: -1}" = "-" ]; then cat >/dev/null; exit "${STUB_NODE_RC:-0}"; fi
    # `env` of a backend container: names only matter
    echo "PATH=/usr/bin" ;;
esac
"""


@pytest.fixture
def stub(tmp_path):
    (tmp_path / "bin").mkdir()
    docker = tmp_path / "bin" / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    (tmp_path / "state").mkdir()
    return tmp_path


def run_smoke(stub: Path, env_extra: dict[str, str]):
    log = stub / "log"
    log.write_text("")
    res = subprocess.run(
        ["bash", str(SMOKE), "luran_whatsapp_sidecar"],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{stub / 'bin'}:{os.environ['PATH']}",
            "STUB_LOG": str(log),
            "STUB_DIR": str(stub / "state"),
            **env_extra,
        },
    )
    return res, log.read_text().splitlines()


def exec_node_line(lines: list[str]) -> str:
    return next(line for line in lines if line.startswith("ARGV [exec] [-i]"))


def test_without_isolation_the_docker_exec_argv_is_exactly_what_staging_always_got(stub):
    res, lines = run_smoke(stub, {})
    assert res.returncode == 0, res.stderr
    assert exec_node_line(lines) == (
        "ARGV [exec] [-i] [-e] [SMOKE_BACKEND=enabled] [-e] [SMOKE_ISOLATION=] "
        "[luran_whatsapp_sidecar] [node] [--input-type=module] [-]"
    )
    assert not [
        line for line in lines if line.startswith("ARGV [inspect]")
    ]  # no extra docker calls
    assert "isolation" not in res.stdout


def test_isolation_passes_the_forbidden_targets_looked_up_from_the_host(stub):
    (stub / "state" / "ip_luran_redis").write_text("172.22.0.3 ")
    (stub / "state" / "ip_luran_qdrant").write_text("172.22.0.4 10.9.0.4 ")
    (stub / "state" / "ip_luran_pgbouncer").write_text("172.22.0.7 ")
    res, lines = run_smoke(stub, {"SMOKE_ISOLATION": "1"})
    assert res.returncode == 0, res.stderr
    line = exec_node_line(lines)
    assert (
        "[SMOKE_FORBIDDEN_TARGETS=luran_redis=172.22.0.3:6379,luran_qdrant=172.22.0.4:6333,"
        "luran_qdrant=10.9.0.4:6333,luran_pgbouncer=172.22.0.7:5432]"
    ) in line
    assert "SKIP" not in res.stdout


def test_a_container_that_is_not_running_is_skipped_not_failed(stub):
    (stub / "state" / "ip_luran_redis").write_text("172.22.0.3 ")
    res, lines = run_smoke(stub, {"SMOKE_ISOLATION": "1"})
    assert res.returncode == 0
    assert "SKIP isolation: luran_qdrant is not running here" in res.stdout
    assert "SKIP isolation: luran_pgbouncer is not running here" in res.stdout
    assert "SMOKE_FORBIDDEN_TARGETS=luran_redis=172.22.0.3:6379]" in exec_node_line(lines)


def test_the_in_container_failure_status_still_propagates(stub):
    res, _ = run_smoke(stub, {"SMOKE_ISOLATION": "1", "STUB_NODE_RC": "1"})
    assert res.returncode == 1


def test_the_program_uses_the_classifiers_for_both_dns_and_tcp():
    assert "classifyLookup(" in SMOKE_TEXT.split("// END isolation-classifiers")[1]
    assert "classifyConnect(" in SMOKE_TEXT.split("// END isolation-classifiers")[1]
    # the old strict comparison is gone: it was the bug
    assert "e?.code === 'ENOTFOUND'" not in SMOKE_TEXT


def test_the_node_program_still_parses(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    body = SMOKE_TEXT.split("<<'JS'", 1)[1].split("\n", 1)[1].split("\nJS\n", 1)[0]
    program = tmp_path / "smoke.mjs"
    program.write_text(body)
    res = subprocess.run(["node", "--check", str(program)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
