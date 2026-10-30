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
    for sid in (
        "sidecar_build",
        "sidecar_net_copy",
        "sidecar_net",
        "sidecar_copy",
        "sidecar_deploy",
    ):
        assert step(sid)["continue-on-error"] is True
    for sid in ("sidecar_build", "sidecar_copy", "sidecar_deploy"):
        assert "steps.sidecar_env.outputs.sidecar_enabled == 'true'" in step(sid)["if"]
    assert PROD_STEPS.index(step("sidecar_build")) < index(
        "Copy files to production stack directory"
    )
    assert (
        deploy_api
        < PROD_STEPS.index(step("sidecar_net_copy"))
        < PROD_STEPS.index(step("sidecar_net"))
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
    for sid in (
        "sidecar_build",
        "sidecar_net_copy",
        "sidecar_net",
        "sidecar_copy",
        "sidecar_deploy",
    ):
        text = yaml.dump(step(sid))
        assert "WHATSAPP_AUTH_MASTER_KEY" not in text and "WHATSAPP_SIDECAR_DB_PASSWORD" not in text
        assert "WHATSAPP_SIDECAR_REDIS_PASSWORD" not in text
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
        "scripts/whatsapp_sidecar_network.sh",
    }
    for rel in source:
        if not rel.startswith(".env") and not rel.endswith(".tar.gz"):
            assert (REPO / rel).exists(), rel


def test_staging_job_is_not_given_the_production_override():
    assert "whatsapp.production.yml" not in yaml.dump(CD["jobs"]["staging-deploy"])


def test_override_pins_the_production_name_and_sizing():
    svc = OVERRIDE["services"]["whatsapp-sidecar"]
    assert svc["container_name"] == "luran_whatsapp_sidecar"  # literal, not interpolated
    assert set(svc) == {
        "container_name",
        "mem_limit",
        "memswap_limit",
        "networks",
        "depends_on",
        "environment",
    }
    assert (
        svc["mem_limit"] == svc["memswap_limit"] == "${WHATSAPP_SIDECAR_MEM_LIMIT:-384m}"
    )  # no swap
    env = dict(svc["environment"])
    redis_url = env.pop("WHATSAPP_SIDECAR_REDIS_URL")
    assert env == {
        "MAX_SESSIONS": "${WHATSAPP_MAX_SESSIONS:-3}",
        "RSS_REFUSE_MB": "${WHATSAPP_RSS_REFUSE_MB:-216}",
    }
    assert redis_url.startswith("redis://default:${WHATSAPP_SIDECAR_REDIS_PASSWORD:?")


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


# ----------------------------------------------------------------------------- Redis isolation (docs/sop/whatsapp_sidecar_redis_isolation.md)

SIDECAR = OVERRIDE["services"]["whatsapp-sidecar"]
REDIS = OVERRIDE["services"]["whatsapp-redis"]
BASE_SIDECAR = yaml.safe_load((REPO / "docker-compose.whatsapp.yml").read_text())["services"][
    "whatsapp-sidecar"
]


def test_sidecar_is_off_the_shared_network_and_on_exactly_its_two_private_ones():
    # listing `networks` is what drops the implicit `default` (luran_default, home of redis/qdrant/pgbouncer)
    assert SIDECAR["networks"] == ["wa_redis", "wa_backend"]
    assert "default" not in SIDECAR["networks"]
    assert (
        "networks" not in BASE_SIDECAR
    )  # staging keeps its default network: nothing to inherit here
    nets = OVERRIDE["networks"]
    assert set(nets) == {"wa_redis", "wa_backend"}
    assert nets["wa_redis"] == {"name": "luran_wa_redis", "internal": True}  # no gateway, no egress
    assert nets["wa_backend"] == {"name": "luran_wa_backend", "external": True}  # runtime-attached


def test_the_sidecar_redis_is_reachable_only_from_its_private_network_and_publishes_nothing():
    assert REDIS["networks"] == ["wa_redis"]
    for forbidden in ("ports", "expose", "network_mode", "extra_hosts", "links"):
        assert forbidden not in REDIS
    assert REDIS["container_name"] == "luran_whatsapp_redis"  # literal: production project only
    assert REDIS["image"].startswith("redis:7")


def test_the_sidecar_redis_is_hardened_and_tightly_limited():
    assert REDIS["read_only"] is True and REDIS["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in REDIS["security_opt"]
    assert REDIS["user"] == "redis"
    assert REDIS["mem_limit"] == REDIS["memswap_limit"] == "32m"  # no swap
    assert REDIS["pids_limit"] <= 32 and float(REDIS["cpus"]) <= 0.5
    assert REDIS["oom_score_adj"] == 500  # dies before production under host pressure
    cmd = " ".join(REDIS["command"])
    # nothing is evicted silently (a lease or send counter), and the dataset is bounded far below the container limit
    assert "--maxmemory-policy noeviction" in cmd and "--maxmemory 8mb" in cmd
    assert "--maxclients 32" in cmd and "--appendonly no" in cmd
    # persistence for the daily send counter: a snapshot after any change within 60 s, on a named volume
    assert '--save "60 1"' in cmd
    assert REDIS["volumes"] == ["wa_redis_data:/data"]
    assert OVERRIDE["volumes"]["wa_redis_data"] == {"name": "luran_wa_redis_data"}


def test_the_sidecar_redis_requires_a_password_that_never_appears_on_a_command_line():
    cmd = " ".join(REDIS["command"])
    assert (
        "--requirepass" not in cmd and "requirepass" not in cmd
    )  # would put it in `ps` and docker inspect
    assert "--aclfile /tmp/users.acl" in cmd
    assert "$$WA_REDIS_PASSWORD" in cmd  # read from the container env, written to tmpfs
    # the default user: on only with that password, only wa: keys, no admin or dangerous commands
    assert "user default on >%s ~wa:* +@all -@admin -@dangerous +info" in cmd
    assert "nopass" not in cmd and "allkeys" not in cmd and "~*" not in cmd
    env = REDIS["environment"]
    assert env == {
        "WA_REDIS_PASSWORD": "${WHATSAPP_SIDECAR_REDIS_PASSWORD:?set WHATSAPP_SIDECAR_REDIS_PASSWORD}"
    }
    # the healthcheck authenticates (an unauthenticated PING must not count as healthy)
    assert "REDISCLI_AUTH=" in REDIS["healthcheck"]["test"][1]
    # the password is a ${VAR} reference everywhere: no literal secret in the file
    assert not re.search(r"(?i)password\s*[:=]\s*[A-Za-z0-9]{16,}", OVERRIDE_TEXT)


def test_the_sidecar_cannot_be_pointed_back_at_the_shared_redis():
    url = SIDECAR["environment"]["WHATSAPP_SIDECAR_REDIS_URL"]
    assert "@whatsapp-redis:6379" in url
    assert "redis:6379" not in url.replace("whatsapp-redis:6379", "")
    assert SIDECAR["depends_on"] == {"whatsapp-redis": {"condition": "service_healthy"}}
    # an inherited WHATSAPP_SIDECAR_REDIS_URL must not be read: the value is built from the password only
    assert "${WHATSAPP_SIDECAR_REDIS_URL" not in OVERRIDE_TEXT


def test_api_and_worker_never_receive_the_redis_password():
    base = yaml.safe_load((REPO / "docker-compose.prod.yml").read_text())["services"]
    for name in ("api", "worker"):
        text = yaml.dump(base[name])
        assert "REDIS_PASSWORD" not in text and "SIDECAR_REDIS" not in text
        assert base[name]["env_file"] in (
            ".env",
            [".env"],
        )  # the file the gate strips the secret out of
    assert "REDIS_PASSWORD" not in (REPO / "docker-compose.prod.yml").read_text()


def test_the_production_compose_file_and_staging_overlay_do_not_carry_the_isolation():
    """No production container is recreated by this change: docker-compose.prod.yml has no wa_* network, and the staging
    overlay is untouched (staging keeps its shared Redis)."""
    prod_text = (REPO / "docker-compose.prod.yml").read_text()
    assert (
        "wa_backend" not in prod_text
        and "wa_redis" not in prod_text
        and "whatsapp" not in prod_text.lower()
    )
    staging_text = (REPO / "docker-compose.whatsapp.yml").read_text()
    assert "wa_redis" not in staging_text and "whatsapp-redis" not in staging_text


def test_network_helper_is_shipped_and_run_before_the_sidecar_starts_and_after_every_api_deploy():
    deploy = step("sidecar_deploy")["with"]["script"]
    assert deploy.index("whatsapp_sidecar_network.sh") < deploy.index(
        "up -d --no-deps whatsapp-redis"
    )
    assert deploy.index("up -d --no-deps whatsapp-redis") < deploy.index(
        "up -d --no-deps whatsapp-sidecar"
    )
    assert "--remove-orphans" not in deploy
    early = step("sidecar_net")["with"]["script"]
    assert (
        "whatsapp_sidecar_network.sh" in early and "docker-compose" not in early
    )  # touches networks only
    # its own step, independent of the image pull: api/worker are recreated by every prod deploy and lose the attachment
    assert "sidecar_build" not in step("sidecar_net")["if"] + step("sidecar_net_copy")["if"]
    # ...and independent of the env gate and of the API step's outcome: a gate that skips (or an API health loop that times
    # out AFTER `up -d` recreated api/worker) must still re-attach a sidecar that is already running
    for sid in ("sidecar_net_copy", "sidecar_net"):
        cond = step(sid)["if"]
        assert "always()" in cond and "!cancelled()" in cond
        assert "sidecar_enabled" not in cond
    assert (
        "--if-exists" in early
    )  # never creates the network on its own; the sidecar deploy step does
    assert step("sidecar_net_copy")["with"]["source"] == "scripts/whatsapp_sidecar_network.sh"


def test_deploy_waits_for_the_new_redis_and_smokes_the_isolation():
    deploy = step("sidecar_deploy")["with"]["script"]
    assert "docker-compose.whatsapp.production.yml" in deploy
    assert "SMOKE_ISOLATION=1" in deploy and "SMOKE_BACKEND_CONTAINERS=" in deploy
    assert "luran_api" in deploy and "luran_worker" in deploy
    # the shared Redis is no longer queried for the sidecar's headroom
    assert "dc exec -T redis" not in deploy


def test_smoke_isolation_checks_are_opt_in_so_staging_output_is_unchanged():
    smoke = (REPO / "scripts" / "smoke_whatsapp_sidecar.sh").read_text()
    assert "SMOKE_ISOLATION" in smoke and "SMOKE_BACKEND_CONTAINERS" in smoke
    for host in ("redis", "qdrant", "pgbouncer"):
        assert f"'{host}'" in smoke
    assert "NOAUTH" in smoke
    for name in (
        "WHATSAPP_AUTH_MASTER_KEY",
        "WHATSAPP_SIDECAR_DB_PASSWORD",
        "WHATSAPP_SIDECAR_REDIS_PASSWORD",
    ):
        assert name in smoke
    assert "SMOKE_ISOLATION" not in yaml.dump(CD["jobs"]["staging-deploy"])


def test_unhealthy_redis_is_stopped_unless_the_running_sidecar_already_uses_it():
    deploy = step("sidecar_deploy")["with"]["script"]
    block = deploy[deploy.index("WhatsApp Redis not healthy") :]
    block = block[: block.index("exit 1")]
    assert "dc stop whatsapp-redis" in block and "luran_wa_redis" in block


def test_smoke_asserts_the_sidecars_exact_networks_and_checks_container_name_dns():
    smoke = (REPO / "scripts" / "smoke_whatsapp_sidecar.sh").read_text()
    assert (
        "luran_wa_backend luran_wa_redis" in smoke
    )  # exactly these two, nothing else (e.g. a hand-run network connect)
    for name in ("luran_redis", "luran_qdrant", "luran_pgbouncer"):
        assert f"'{name}'" in smoke
