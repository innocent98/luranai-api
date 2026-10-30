"""Fixture tests for scripts/prepare_production_whatsapp_env.sh and the production secrets generator.

Every secret below is a dummy built in the test. The assertions include "no value ever appears in the output".
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PREP = REPO / "scripts" / "prepare_production_whatsapp_env.sh"
GEN = REPO / "scripts" / "generate_whatsapp_production_secrets.sh"

TOKEN = "t" * 40
SECRET = "s" * 40
MASTER = "ab" * 32
DBPW = "Z" * 28
PEPPER = "p" * 40
REDISPW = "R" * 40
VALUES = [TOKEN, SECRET, MASTER, DBPW, PEPPER, REDISPW]

BASE = [
    "COMPOSE_PROJECT_NAME=luran",
    "ENVIRONMENT=production",
    "API_MEM_LIMIT=1200m",
    "POSTGRES_USER=app",
]
FULL = [
    f"WHATSAPP_SIDECAR_TOKEN={TOKEN}",
    f"WHATSAPP_WEBHOOK_SECRET={SECRET}",
    f"WHATSAPP_AUTH_MASTER_KEY={MASTER}",
    f"WHATSAPP_SIDECAR_DB_PASSWORD={DBPW}",
    f"WHATSAPP_SIDECAR_REDIS_PASSWORD={REDISPW}",
    f"WHATSAPP_CONTACT_PEPPER={PEPPER}",
    "WHATSAPP_SIDECAR_URL=http://whatsapp-sidecar:8787",
    "WHATSAPP_ENABLED=false",
    "WHATSAPP_MAX_CONNECTIONS=3",
]


def run(tmp_path: Path, lines: list[str] | None, *, eol: str = "\n"):
    env_file = tmp_path / ".env"
    sidecar_file = tmp_path / ".env.sidecar"
    out = tmp_path / "gh_output"
    if lines is not None:
        env_file.write_bytes((eol.join(lines) + eol).encode())
        env_file.chmod(0o600)
    res = subprocess.run(
        ["bash", str(PREP), str(env_file), str(sidecar_file)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "GITHUB_OUTPUT": str(out),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        },
    )
    outputs = (
        dict(line.split("=", 1) for line in out.read_text().splitlines()) if out.exists() else {}
    )
    shown = (
        res.stdout + res.stderr + (tmp_path / "summary").read_text()
        if (tmp_path / "summary").exists()
        else res.stdout + res.stderr
    )
    for value in VALUES:
        assert value not in shown, "a secret value was printed"
    return res, outputs, env_file, sidecar_file


def test_no_sidecar_names_is_a_pure_noop(tmp_path):
    lines = [*BASE, "WHATSAPP_ENABLED=false"]
    res, outputs, env_file, sidecar_file = run(tmp_path, lines)
    before = ("\n".join(lines) + "\n").encode()
    assert res.returncode == 0
    assert outputs == {"sidecar_enabled": "false"}
    assert env_file.read_bytes() == before
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600  # not even chmod'ed
    assert not sidecar_file.exists()
    assert "SKIPPED" in res.stdout


def test_all_names_enable_and_secrets_leave_the_api_env(tmp_path):
    res, outputs, env_file, sidecar_file = run(tmp_path, [*BASE, *FULL])
    assert res.returncode == 0
    assert outputs == {"sidecar_enabled": "true", "backend_mode": "disabled"}
    api_env = env_file.read_text()
    assert (
        "WHATSAPP_AUTH_MASTER_KEY" not in api_env
        and "WHATSAPP_SIDECAR_DB_PASSWORD" not in api_env
        and "WHATSAPP_SIDECAR_REDIS_PASSWORD" not in api_env
    )
    for kept in (
        "WHATSAPP_SIDECAR_TOKEN",
        "WHATSAPP_WEBHOOK_SECRET",
        "WHATSAPP_CONTACT_PEPPER",
        "API_MEM_LIMIT=1200m",
    ):
        assert kept in api_env
    assert sorted(sidecar_file.read_text().split()) == sorted(
        [
            f"WHATSAPP_AUTH_MASTER_KEY={MASTER}",
            f"WHATSAPP_SIDECAR_DB_PASSWORD={DBPW}",
            f"WHATSAPP_SIDECAR_REDIS_PASSWORD={REDISPW}",
        ]
    )
    # the scp container reads both as another user (luranai-api #165)
    assert (
        stat.S_IMODE(env_file.stat().st_mode) == 0o644
        and stat.S_IMODE(sidecar_file.stat().st_mode) == 0o644
    )


def test_script_never_changes_whatsapp_enabled_or_the_api_limit(tmp_path):
    _, _, env_file, _ = run(tmp_path, [*BASE, *FULL])
    kept = env_file.read_text().splitlines()
    assert "WHATSAPP_ENABLED=false" in kept
    assert "API_MEM_LIMIT=1200m" in kept
    assert [line for line in kept if line.startswith("WHATSAPP_ENABLED")] == [
        "WHATSAPP_ENABLED=false"
    ]


@pytest.mark.parametrize("flag", ["true", "TRUE", "1", "yes", "on"])
def test_backend_mode_follows_the_flag(tmp_path, flag):
    lines = [
        *BASE,
        *[line for line in FULL if not line.startswith("WHATSAPP_ENABLED")],
        f"WHATSAPP_ENABLED={flag}",
    ]
    _, outputs, _, _ = run(tmp_path, lines)
    assert outputs["backend_mode"] == "enabled"


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        (["COMPOSE_PROJECT_NAME=luran_staging"], "COMPOSE_PROJECT_NAME is not luran"),
        (["COMPOSE_PROJECT_NAME=luran-staging"], "COMPOSE_PROJECT_NAME is not luran"),
        (["ENVIRONMENT=staging"], "ENVIRONMENT is not production"),
    ],
)
def test_staging_env_is_refused_but_secrets_still_stripped(tmp_path, extra, needle):
    key = extra[0].split("=")[0]
    lines = [line for line in BASE if not line.startswith(key)] + FULL + extra
    res, outputs, env_file, sidecar_file = run(tmp_path, lines)
    assert res.returncode == 0
    assert outputs == {"sidecar_enabled": "false"}
    assert needle in res.stdout
    assert "WHATSAPP_AUTH_MASTER_KEY" not in env_file.read_text()
    assert "WHATSAPP_SIDECAR_REDIS_PASSWORD" not in env_file.read_text()
    assert not sidecar_file.exists()


def test_unset_project_name_is_accepted(tmp_path):
    lines = [line for line in BASE if not line.startswith("COMPOSE_PROJECT_NAME")] + FULL
    _, outputs, _, _ = run(tmp_path, lines)
    assert outputs["sidecar_enabled"] == "true"


def test_partial_names_skip_with_names_only(tmp_path):
    lines = [*BASE, *[line for line in FULL if "MASTER_KEY" not in line]]
    res, outputs, env_file, sidecar_file = run(tmp_path, lines)
    assert outputs == {"sidecar_enabled": "false"}
    assert "missing or empty: WHATSAPP_AUTH_MASTER_KEY" in res.stdout
    assert "WHATSAPP_SIDECAR_DB_PASSWORD" not in env_file.read_text()
    assert not sidecar_file.exists()


def test_existing_names_without_the_redis_password_fail_closed_naming_it_only(tmp_path):
    """The pre-isolation env (every old name, no Redis password) must NOT deploy the old shared-Redis layout."""
    lines = [*BASE, *[line for line in FULL if "REDIS_PASSWORD" not in line]]
    res, outputs, env_file, sidecar_file = run(tmp_path, lines)
    assert res.returncode == 0 and outputs == {"sidecar_enabled": "false"}
    assert "missing or empty: WHATSAPP_SIDECAR_REDIS_PASSWORD" in res.stdout
    # loud, not a quiet green run: an already-running pre-isolation sidecar keeps the shared-Redis layout until this is fixed
    assert "::warning::" in res.stdout and "NOT deployed" in res.stdout
    api_env = env_file.read_text()
    assert (
        "WHATSAPP_AUTH_MASTER_KEY" not in api_env and "WHATSAPP_SIDECAR_DB_PASSWORD" not in api_env
    )
    assert not sidecar_file.exists()


def test_only_the_redis_password_still_counts_as_a_sidecar_name_and_is_stripped(tmp_path):
    res, outputs, env_file, sidecar_file = run(
        tmp_path, [*BASE, f"WHATSAPP_SIDECAR_REDIS_PASSWORD={REDISPW}"]
    )
    assert outputs == {"sidecar_enabled": "false"}
    assert "missing or empty" in res.stdout and "SKIPPED" in res.stdout
    assert "WHATSAPP_SIDECAR_REDIS_PASSWORD" not in env_file.read_text()
    assert not sidecar_file.exists()


@pytest.mark.parametrize(
    ("replace", "name"),
    [
        (("WHATSAPP_SIDECAR_TOKEN", "short"), "WHATSAPP_SIDECAR_TOKEN"),
        (("WHATSAPP_AUTH_MASTER_KEY", "zz" * 32), "WHATSAPP_AUTH_MASTER_KEY"),
        (("WHATSAPP_AUTH_MASTER_KEY", "ab" * 31), "WHATSAPP_AUTH_MASTER_KEY"),
        (
            ("WHATSAPP_SIDECAR_DB_PASSWORD", "-" * 24),
            "WHATSAPP_SIDECAR_DB_PASSWORD",
        ),  # right length, not alphanumeric
        (("WHATSAPP_SIDECAR_REDIS_PASSWORD", "R" * 31), "WHATSAPP_SIDECAR_REDIS_PASSWORD"),
        (("WHATSAPP_SIDECAR_REDIS_PASSWORD", "R" * 31 + "@"), "WHATSAPP_SIDECAR_REDIS_PASSWORD"),
        (("WHATSAPP_SIDECAR_REDIS_PASSWORD", "R" * 31 + "/"), "WHATSAPP_SIDECAR_REDIS_PASSWORD"),
    ],
)
def test_malformed_values_skip_naming_the_key_only(tmp_path, replace, name):
    key, value = replace
    lines = [*BASE, *[f"{key}={value}" if line.startswith(key + "=") else line for line in FULL]]
    res, outputs, _, _ = run(tmp_path, lines)
    assert outputs == {"sidecar_enabled": "false"}
    assert "malformed" in res.stdout and name in res.stdout
    assert value not in res.stdout


def test_dotenv_dialects_crlf_export_spaces_quotes(tmp_path):
    lines = [
        "COMPOSE_PROJECT_NAME=luran",
        f"export WHATSAPP_SIDECAR_TOKEN='{TOKEN}'",
        f'  WHATSAPP_WEBHOOK_SECRET = "{SECRET}"',
        f"WHATSAPP_AUTH_MASTER_KEY={MASTER}",
        f"WHATSAPP_SIDECAR_DB_PASSWORD={DBPW}",
        f"export WHATSAPP_SIDECAR_REDIS_PASSWORD = '{REDISPW}'",
        "WHATSAPP_ENABLED=false",
    ]
    res, outputs, env_file, sidecar_file = run(tmp_path, lines, eol="\r\n")
    assert res.returncode == 0 and outputs["sidecar_enabled"] == "true"
    assert "WHATSAPP_AUTH_MASTER_KEY" not in env_file.read_text()
    assert MASTER in sidecar_file.read_text() and REDISPW in sidecar_file.read_text()
    assert "REDIS_PASSWORD" not in env_file.read_text()


def test_prefix_sharing_names_are_not_confused(tmp_path):
    lines = [
        *BASE,
        *FULL,
        "WHATSAPP_SIDECAR_TOKEN_OLD=" + "o" * 40,
        "WHATSAPP_AUTH_MASTER_KEY_BACKUP=" + "c" * 64,
        "WHATSAPP_SIDECAR_REDIS_PASSWORD_OLD=" + "d" * 40,
    ]
    _, _, env_file, sidecar_file = run(tmp_path, lines)
    api_env = env_file.read_text()
    assert "WHATSAPP_SIDECAR_TOKEN_OLD" in api_env and "WHATSAPP_AUTH_MASTER_KEY_BACKUP" in api_env
    assert "WHATSAPP_AUTH_MASTER_KEY_BACKUP" not in sidecar_file.read_text()
    assert "WHATSAPP_SIDECAR_REDIS_PASSWORD_OLD" in api_env
    assert "WHATSAPP_SIDECAR_REDIS_PASSWORD_OLD" not in sidecar_file.read_text()


def test_missing_env_file_skips_cleanly(tmp_path):
    res, outputs, _, sidecar_file = run(tmp_path, None)
    assert res.returncode == 0 and outputs == {"sidecar_enabled": "false"}
    assert not sidecar_file.exists()


def test_backend_cap_above_sidecar_cap_warns(tmp_path):
    lines = [
        *BASE,
        *[line for line in FULL if "MAX_CONNECTIONS" not in line],
        "WHATSAPP_MAX_CONNECTIONS=10",
    ]
    res, outputs, _, _ = run(tmp_path, lines)
    assert outputs["sidecar_enabled"] == "true"
    assert "WHATSAPP_MAX_CONNECTIONS (backend) is higher" in res.stdout


def test_enabled_flag_without_sidecar_names_warns_only(tmp_path):
    res, outputs, env_file, _ = run(tmp_path, [*BASE, "WHATSAPP_ENABLED=true"])
    assert res.returncode == 0 and outputs == {"sidecar_enabled": "false"}
    assert "::warning::WHATSAPP_ENABLED is on" in res.stdout
    assert "WHATSAPP_ENABLED=true" in env_file.read_text()  # reported, never altered


# ----------------------------------------------------------------------------- generator


def test_generator_writes_a_private_file_prints_nothing_and_ships_disabled(tmp_path):
    out = tmp_path / "secrets.env"
    res = subprocess.run(["bash", str(GEN), str(out)], capture_output=True, text=True)
    assert res.returncode == 0 and res.stdout == "" and res.stderr == ""
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    values = dict(
        line.split("=", 1)
        for line in out.read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert values["WHATSAPP_ENABLED"] == "false"
    assert values["WHATSAPP_MAX_CONNECTIONS"] == "3"
    assert (
        len(values["WHATSAPP_AUTH_MASTER_KEY"]) == 64
        and len(values["WHATSAPP_SIDECAR_DB_PASSWORD"]) == 48
        and re.fullmatch(r"[0-9a-f]{48}", values["WHATSAPP_SIDECAR_REDIS_PASSWORD"])
    )
    assert (
        len(
            {
                values[k]
                for k in (
                    "WHATSAPP_SIDECAR_TOKEN",
                    "WHATSAPP_WEBHOOK_SECRET",
                    "WHATSAPP_CONTACT_PEPPER",
                    "WHATSAPP_AUTH_MASTER_KEY",
                    "WHATSAPP_SIDECAR_DB_PASSWORD",
                    "WHATSAPP_SIDECAR_REDIS_PASSWORD",
                )
            }
        )
        == 6
    )
    # what it generates must pass the gate
    env_file = tmp_path / ".env"
    env_file.write_text("COMPOSE_PROJECT_NAME=luran\n" + out.read_text())
    gate = subprocess.run(
        ["bash", str(PREP), str(env_file), str(tmp_path / ".env.sidecar")],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert (
        "ENABLED for deploy" in gate.stdout
        and "backend WHATSAPP_ENABLED is disabled" in gate.stdout
    )


def test_generator_refuses_to_overwrite(tmp_path):
    out = tmp_path / "secrets.env"
    out.write_text("keep")
    res = subprocess.run(["bash", str(GEN), str(out)], capture_output=True, text=True)
    assert res.returncode == 1 and out.read_text() == "keep"


def test_generator_redis_only_writes_exactly_the_one_new_name(tmp_path):
    """For an owner whose env already has the other WhatsApp secrets: must not regenerate (and so change) the master key."""
    out = tmp_path / "redis.env"
    res = subprocess.run(
        ["bash", str(GEN), "--redis-only", str(out)], capture_output=True, text=True
    )
    assert res.returncode == 0 and res.stdout == "" and res.stderr == ""
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    lines = [line for line in out.read_text().splitlines() if line and not line.startswith("#")]
    assert len(lines) == 1
    name, value = lines[0].split("=", 1)
    assert name == "WHATSAPP_SIDECAR_REDIS_PASSWORD" and re.fullmatch(r"[0-9a-f]{48}", value)
    again = subprocess.run(
        ["bash", str(GEN), "--redis-only", str(out)], capture_output=True, text=True
    )
    assert again.returncode == 1 and out.read_text().count(value) == 1  # refuses to overwrite


def test_generator_rejects_unknown_flags(tmp_path):
    res = subprocess.run(
        ["bash", str(GEN), "--bogus", str(tmp_path / "x.env")], capture_output=True, text=True
    )
    assert res.returncode == 2 and not (tmp_path / "x.env").exists()
