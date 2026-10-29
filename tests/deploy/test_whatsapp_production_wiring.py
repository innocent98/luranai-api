"""Static invariants of the production WhatsApp sidecar wiring (cd.yml steps + compose override).

These pin the properties the SOP promises: skip-path when names are missing, nothing turns WhatsApp on, production-only
guards, and sizing that stays internally consistent. They read files only; nothing is executed against a host.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
CD = yaml.safe_load((REPO / ".github" / "workflows" / "cd.yml").read_text())
PROD_STEPS = CD["jobs"]["production-deploy"]["steps"]
OVERRIDE_TEXT = (REPO / "docker-compose.whatsapp.production.yml").read_text()
OVERRIDE = yaml.safe_load(OVERRIDE_TEXT)


def step(step_id: str) -> dict:
    return next(s for s in PROD_STEPS if s.get("id") == step_id)


def index(name: str) -> int:
    return next(i for i, s in enumerate(PROD_STEPS) if s.get("name") == name)


def test_gate_step_runs_after_decrypt_and_before_the_env_is_shipped():
    gate = PROD_STEPS.index(step("sidecar_env"))
    assert (
        index("Decrypt production .env") < gate < index("Copy files to production stack directory")
    )
    assert "continue-on-error" not in step(
        "sidecar_env"
    )  # fail closed: never ship the secrets in the api env
    assert "if" not in step("sidecar_env")
    assert "prepare_production_whatsapp_env.sh" in step("sidecar_env")["run"]


def test_sidecar_steps_are_optional_and_follow_the_api_deploy():
    deploy_api = index("Deploy to production")
    notify = index("Notify deployment status")
    for sid in ("sidecar_build", "sidecar_copy", "sidecar_deploy"):
        s = step(sid)
        assert s["continue-on-error"] is True
        assert "steps.sidecar_env.outputs.sidecar_enabled == 'true'" in s["if"]
    assert PROD_STEPS.index(step("sidecar_build")) < index(
        "Copy files to production stack directory"
    )
    assert (
        deploy_api
        < PROD_STEPS.index(step("sidecar_copy"))
        < PROD_STEPS.index(step("sidecar_deploy"))
        < notify
    )
    status = next(s for s in PROD_STEPS if s.get("name") == "WhatsApp sidecar status")
    assert (
        "sidecar_enabled == 'true'" in status["if"]
        and deploy_api < PROD_STEPS.index(status) < notify
    )


def test_job_is_still_the_production_environment_with_no_staging_project_reference():
    job = CD["jobs"]["production-deploy"]
    assert job["environment"]["name"] == "production"
    assert "luran_staging" not in yaml.dump(job)


def test_nothing_in_the_production_job_turns_whatsapp_on():
    text = yaml.dump(CD["jobs"]["production-deploy"])
    assert not re.search(r"WHATSAPP_ENABLED\s*[=:]\s*['\"]?(true|1)", text, re.I)
    assert "WHATSAPP_ENABLED" not in yaml.dump(OVERRIDE)


def test_deploy_script_guards_the_project_and_never_removes_orphans():
    script = step("sidecar_deploy")["with"]["script"]
    assert "com.docker.compose.project" in script and "luran_api" in script
    assert "-p luran" in script and "export COMPOSE_PROJECT_NAME=luran" in script
    assert "--remove-orphans" not in script
    assert "up -d --no-deps whatsapp-sidecar" in script
    assert "chmod 600 .env .env.sidecar" in script
    assert "SMOKE_BACKEND=" in script and "smoke_whatsapp_sidecar.sh" in script


def test_sidecar_secrets_are_named_only_by_the_gate_step():
    for sid in ("sidecar_build", "sidecar_copy", "sidecar_deploy"):
        text = yaml.dump(step(sid))
        assert "WHATSAPP_AUTH_MASTER_KEY" not in text and "WHATSAPP_SIDECAR_DB_PASSWORD" not in text
        assert "secrets.WHATSAPP" not in text


def test_scp_ships_exactly_the_sidecar_files():
    source = step("sidecar_copy")["with"]["source"].split(",")
    assert set(source) == {
        "sidecar-image.tar.gz",
        "docker-compose.whatsapp.yml",
        "docker-compose.whatsapp.production.yml",
        ".env.sidecar",
        "scripts/whatsapp_sidecar_db_init.sql",
        "scripts/smoke_whatsapp_sidecar.sh",
        "scripts/whatsapp_sidecar_snapshot.sh",
    }
    for rel in source:
        if not rel.startswith(".env") and not rel.endswith(".tar.gz"):
            assert (REPO / rel).exists(), rel


def test_staging_job_is_not_given_the_production_override():
    assert "whatsapp.production.yml" not in yaml.dump(CD["jobs"]["staging-deploy"])


def test_override_pins_the_production_name_and_sizing():
    svc = OVERRIDE["services"]["whatsapp-sidecar"]
    assert svc["container_name"] == "luran_whatsapp_sidecar"  # literal, not interpolated
    assert set(svc) == {"container_name", "mem_limit", "memswap_limit", "environment"}
    assert (
        svc["mem_limit"] == svc["memswap_limit"] == "${WHATSAPP_SIDECAR_MEM_LIMIT:-384m}"
    )  # no swap
    assert svc["environment"] == {
        "MAX_SESSIONS": "${WHATSAPP_MAX_SESSIONS:-3}",
        "RSS_REFUSE_MB": "${WHATSAPP_RSS_REFUSE_MB:-216}",
    }


def test_sizing_is_coherent_with_the_measurements():
    """RSS guard must trip before the OOM killer: refuse (MiB; /health divides by 1048576) + 32 MiB tmpfs + ~48 MiB
    healthcheck process + ~50 MiB deploy-smoke process <= limit."""
    limit_mib = int(re.search(r"WHATSAPP_SIDECAR_MEM_LIMIT:-(\d+)m", OVERRIDE_TEXT).group(1))
    refuse_mb = int(re.search(r"WHATSAPP_RSS_REFUSE_MB:-(\d+)", OVERRIDE_TEXT).group(1))
    sessions = int(re.search(r"WHATSAPP_MAX_SESSIONS:-(\d+)", OVERRIDE_TEXT).group(1))
    measured_rss_peak_mb, measured_sessions = 144, 2
    assert refuse_mb == round(measured_rss_peak_mb * 1.5)
    assert sessions == round(measured_sessions * 1.5)
    assert refuse_mb + 32 + 48 + 50 <= limit_mib
    assert limit_mib % 64 == 0
    # the staging-measured RSS at the current cap must sit well below the refuse threshold, or normal load is refused
    assert measured_rss_peak_mb < refuse_mb


def test_production_image_has_its_own_repo_name_so_staging_pruning_cannot_touch_it():
    build = step("sidecar_build")["run"]
    deploy = step("sidecar_deploy")["with"]["script"]
    assert "luran-whatsapp-sidecar-prod:" in build and "luran-whatsapp-sidecar-prod:" in deploy
    assert "docker images luran-whatsapp-sidecar-prod " in deploy
    assert not re.search(r"luran-whatsapp-sidecar:", build + deploy)


def test_db_init_asserts_least_privilege_before_the_sidecar_starts():
    sql = (REPO / "scripts" / "whatsapp_sidecar_db_init.sql").read_text()
    assert (
        "RAISE EXCEPTION" in sql
        and "has_schema_privilege" in sql
        and "has_sequence_privilege" in sql
    )
    script = step("sidecar_deploy")["with"]["script"]
    assert script.index("whatsapp-db-init") < script.index("up -d --no-deps whatsapp-sidecar")
