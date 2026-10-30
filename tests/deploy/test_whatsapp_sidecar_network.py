"""Fixture tests for scripts/whatsapp_sidecar_network.sh, driven through a fake `docker` on PATH.

The fake keeps its state in files so a second run sees the first run's network and attachments, which is how idempotency
is proven without a daemon.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "whatsapp_sidecar_network.sh"
NET = "luran_wa_backend"

FAKE_DOCKER = r"""#!/usr/bin/env bash
state="$STUB_STATE"
echo "$*" >> "$state/calls"
case "$1" in
  inspect)
    c="${@: -1}"
    [ -e "$state/containers/$c" ] || { echo "Error: No such object: $c" >&2; exit 1; }
    case "$*" in
      *com.docker.compose.project*) cat "$state/containers/$c" ;;
      *) cat "$state/attached/$c" 2>/dev/null || true ;;
    esac
    ;;
  network)
    case "$2" in
      inspect)
        [ -e "$state/net" ] || exit 1
        case "$*" in
          *.Containers*) cat "$state/members" 2>/dev/null || true ;;
          *) cat "$state/net" ;;
        esac ;;
      create) echo "false bridge" > "$state/net"; echo created-id ;;
      connect)
        net="${@: -2:1}"; c="${@: -1}"
        mkdir -p "$state/attached"; echo -n "$net " >> "$state/attached/$c"
        ;;
    esac
    ;;
esac
"""


@pytest.fixture
def stub(tmp_path):
    (tmp_path / "bin").mkdir()
    docker = tmp_path / "bin" / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    state = tmp_path / "state"
    (state / "containers").mkdir(parents=True)
    (state / "attached").mkdir()
    for name in ("luran_api", "luran_worker", "luran_db"):
        (state / "containers" / name).write_text("luran")
        (state / "attached" / name).write_text("luran_default ")
    return state


def run(stub: Path, *args: str):
    res = subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env={"PATH": f"{stub.parent / 'bin'}:{os.environ['PATH']}", "STUB_STATE": str(stub)},
    )
    calls = (stub / "calls").read_text().splitlines() if (stub / "calls").exists() else []
    return res, calls


def mutating(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith(("network create", "network connect"))]


def test_creates_the_network_and_attaches_api_worker_db_with_the_right_aliases(stub):
    res, calls = run(stub)
    assert res.returncode == 0, res.stderr
    assert mutating(calls) == [
        f"network create --driver bridge --label luran.purpose=whatsapp-sidecar {NET}",
        f"network connect --alias api {NET} luran_api",
        f"network connect {NET} luran_worker",
        f"network connect --alias db {NET} luran_db",
    ]
    assert f"network {NET}: created" in res.stdout


def test_a_second_run_changes_nothing(stub):
    run(stub)
    (stub / "calls").unlink()
    res, calls = run(stub)
    assert res.returncode == 0
    assert mutating(calls) == []
    assert res.stdout.count("already on") == 3


def test_a_recreated_api_is_re_attached_and_the_others_are_left_alone(stub):
    run(stub)
    (stub / "attached" / "luran_api").write_text(
        "luran_default "
    )  # compose recreated it: attachment lost
    (stub / "calls").unlink()
    res, calls = run(stub)
    assert res.returncode == 0
    assert mutating(calls) == [f"network connect --alias api {NET} luran_api"]


def test_no_sidecar_or_redis_container_is_ever_touched(stub):
    _, calls = run(stub)
    text = "\n".join(calls)
    for forbidden in (
        "luran_redis",
        "luran_qdrant",
        "luran_pgbouncer",
        "whatsapp",
        "restart",
        "stop",
        " rm ",
    ):
        assert forbidden not in text.replace(
            "whatsapp-sidecar", ""
        )  # the label value is the only 'whatsapp'
    assert "disconnect" not in text


@pytest.mark.parametrize("project", ["luran_staging", "", "other"])
def test_refuses_when_luran_api_is_not_the_production_stack(stub, project):
    (stub / "containers" / "luran_api").write_text(project)
    res, calls = run(stub)
    assert res.returncode == 1 and "refusing" in res.stderr
    assert mutating(calls) == []


def test_refuses_when_luran_api_is_missing(stub):
    (stub / "containers" / "luran_api").unlink()
    res, calls = run(stub)
    assert res.returncode == 1 and mutating(calls) == []


def test_missing_member_container_fails_loudly_naming_it(stub):
    (stub / "containers" / "luran_db").unlink()
    res, _ = run(stub)
    assert res.returncode == 1 and "luran_db" in res.stderr


@pytest.mark.parametrize("shape", ["true bridge", "false host", "false overlay"])
def test_refuses_an_existing_network_that_is_internal_or_not_a_bridge(stub, shape):
    (stub / "net").write_text(shape)
    res, calls = run(stub)
    assert res.returncode == 1 and "refusing" in res.stderr
    assert mutating(calls) == []


@pytest.mark.parametrize(
    "intruder", ["luran_redis", "luran_qdrant", "luran_pgbouncer", "some_other"]
)
def test_refuses_a_network_that_holds_a_container_outside_the_allowed_set(stub, intruder):
    """A hand-run `docker network connect luran_wa_backend luran_redis` would undo the isolation without changing any
    service-name DNS result, so the helper itself must fail on foreign members."""
    (stub / "net").write_text("false bridge")
    (stub / "members").write_text(f"luran_api luran_whatsapp_sidecar {intruder} ")
    res, calls = run(stub)
    assert res.returncode == 1 and intruder in res.stderr
    assert mutating(calls) == []


def test_the_expected_members_are_accepted(stub):
    (stub / "net").write_text("false bridge")
    (stub / "members").write_text("luran_api luran_worker luran_db luran_whatsapp_sidecar ")
    res, _ = run(stub)
    assert res.returncode == 0, res.stderr


def test_if_exists_mode_never_creates_the_network(stub):
    """Used after every prod deploy, also when the sidecar gate skipped: no network yet means nothing to repair."""
    res, calls = run(stub, "--if-exists")
    assert res.returncode == 0 and "nothing to attach" in res.stdout
    assert mutating(calls) == []


def test_if_exists_mode_re_attaches_when_the_network_exists(stub):
    run(stub)
    (stub / "attached" / "luran_worker").write_text("luran_default ")
    (stub / "calls").unlink()
    res, calls = run(stub, "--if-exists")
    assert res.returncode == 0
    assert mutating(calls) == [f"network connect {NET} luran_worker"]


def test_unknown_argument_is_rejected(stub):
    res, calls = run(stub, "--bogus")
    assert res.returncode == 2 and mutating(calls) == []
