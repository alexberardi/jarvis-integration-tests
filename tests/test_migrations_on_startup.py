"""Migrate-on-startup contract — every migrate-set service container in the CI
stack must boot with its database at alembic HEAD.

WHY THIS EXISTS (the 2026-06 fleet-wide outage):
  jarvis-config-service shipped alembic migration 005 (services.external_host)
  but its image/compose never ran `alembic upgrade head` on startup — it relied
  on `Base.metadata.create_all()`, which CREATEs missing tables but never ALTERs
  an existing one. So a previously-initialised prod DB stayed at 004, the new
  column was absent, and EVERY `GET /services` query 500'd across the whole
  fleet. The per-service unit suites were green (their own ephemeral DBs start
  empty, so create_all() is enough). The existing install-e2e was green too: it
  only probed `/health` (a shallow `{"status":"ok"}` that never touches the DB)
  and only stood up the installer's EXPORT compose (which happened to migrate
  config-service) — it never asserted migrations actually RAN, and never hit the
  symptom endpoint.

WHAT THIS LOCKS DOWN (generator-agnostic — catches the bug for ANY service):
  1. For every migrate-set service this CI stack stands up, assert the running
     container's DB is at alembic head:
        current = `python -m alembic current`   (the applied revision)
        heads   = `python -m alembic heads`      (the latest revision in code)
     and assert current == heads. A service that skipped its migrations reports
     an EMPTY or BEHIND `current` while `heads` advances → the sets differ → FAIL.
     This is exactly the 005-vs-004 drift the incident produced.
  2. PLUS the exact symptom endpoint: `GET /services` on config-service must
     return HTTP 200 — NOT just `/health`, and asserted as `== 200` (it's an
     open-read endpoint; a stale schema turns it into a 500, which is the whole
     fleet-visible failure mode). `/health` alone would have stayed green through
     the outage; that is the gap this case closes.

PROVEN TO CATCH THE BUG: bring up config-service, `alembic downgrade -1` (the DB
now sits at 004 while heads is 005), and BOTH legs fire — `current != heads` AND
`GET /services` returns 500. `alembic upgrade head` restores both to green.

SCOPE — which services this asserts:
  The migrate-set (flagged `migrate: true` in both compose generators) is
  config-service, auth, command-center, whisper-api, llm-proxy-api, notifications.
  This CI stack's `core` profile only stands up THREE of them — auth,
  config-service, command-center — so this file asserts those three and SKIPS the
  rest (a service the stack doesn't run can't be checked here; the from-source /
  cross-repo lanes cover whisper/llm-proxy when they build). Each service is
  independently skipped if its container isn't up, so the file degrades cleanly:
  it no-ops entirely in the fakes-only lanes / local runs that don't bring up the
  stack (gated on CC_URL, exactly like test_cc_real_smoke.py).

DOCKER ACCESS:
  The alembic-head checks shell out to `docker compose ... exec` against the same
  project the runner brought the stack up under — pytest runs ON the runner host
  (same host as `docker compose`), so the socket is reachable. Project name +
  compose-file flags default to the runner's invocation and are overridable via
  env (COMPOSE_PROJECT_NAME / CI_COMPOSE_FILES) so the same test runs against any
  compose layout. If docker compose isn't reachable from the test host, the
  alembic-head checks skip (the /services HTTP leg still runs — it needs only the
  mapped port).
"""

from __future__ import annotations

import os
import shlex
import subprocess

import httpx
import pytest

CC_URL = os.environ.get("CC_URL")
CONFIG_URL = os.environ.get("CONFIG_URL", "http://localhost:7700")

# docker compose invocation. Defaults match the integration-runner's bring-up
# (`docker compose -f docker-compose.ci.yaml ...`, project name derived from the
# compose `name:` key → "jarvis-ci"). Overridable so the same assertions run
# against any compose layout (e.g. a from-source overlay chain).
COMPOSE_PROJECT_NAME = os.environ.get("COMPOSE_PROJECT_NAME", "jarvis-ci")
# Space-separated compose-file paths; each becomes a `-f <path>` flag. Defaults to
# the single CI compose file the runner uses.
CI_COMPOSE_FILES = os.environ.get("CI_COMPOSE_FILES", "docker-compose.ci.yaml")

SKIP_REASON = "CC_URL unset — skipping migrate-on-startup checks (no service stack)"

# The migrate-set services THIS CI stack (`core` profile) stands up, mapped to the
# compose service name `docker compose exec` targets. A service not in the stack
# is intentionally absent here — it can't be asserted from this harness.
MIGRATE_SET_SERVICES = (
    "jarvis-auth",
    "jarvis-config-service",
    "jarvis-command-center",
)


def _compose_base_cmd() -> list[str]:
    """`docker compose -p <project> -f <file> [-f <file> ...]` as an argv list."""
    cmd = ["docker", "compose", "-p", COMPOSE_PROJECT_NAME]
    for f in shlex.split(CI_COMPOSE_FILES):
        cmd += ["-f", f]
    return cmd


def _docker_compose_available() -> bool:
    """True iff `docker compose` is reachable from the test host (the runner is;
    a remote/CI-less invocation may not be — then the exec-based checks skip)."""
    try:
        proc = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=20,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _service_is_up(service: str) -> bool:
    """True iff the compose project has a RUNNING container for `service`.

    `docker compose ps -q <service>` prints the container id(s) when one is up and
    nothing when it isn't, so an empty result means the stack didn't bring this
    service up in the current lane (→ the per-service check skips, it doesn't fail)."""
    proc = subprocess.run(
        _compose_base_cmd() + ["ps", "-q", "--status", "running", service],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode == 0 and proc.stdout.strip() != ""


def _alembic_revisions(service: str, subcommand: str) -> set[str]:
    """Run `python -m alembic <subcommand>` inside the service container and return
    the set of revision ids it reports.

    `alembic current` / `alembic heads` print one revision per line, optionally
    suffixed ` (head)`, interleaved with INFO log lines (alembic logs the
    migration context to stdout). We keep only lines whose FIRST token looks like a
    revision id (hex/alnum, not an `INFO`/`WARNING` banner or an empty line) and
    strip the `(head)` annotation. An EMPTY set from `current` is the un-migrated
    signal: a DB that never ran `alembic upgrade` has no `alembic_version` row, so
    `current` prints only the INFO banner and no revision — which can never equal a
    non-empty `heads`."""
    proc = subprocess.run(
        _compose_base_cmd() + ["exec", "-T", service, "python", "-m", "alembic", subcommand],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"`alembic {subcommand}` failed inside {service} (exit {proc.returncode}). "
        f"stdout={proc.stdout[-500:]!r} stderr={proc.stderr[-500:]!r} — the image "
        f"may not ship alembic, or its alembic.ini/env.py can't reach the DB."
    )
    revisions: set[str] = set()
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        token = line.split()[0]
        # Skip alembic's INFO/WARNING/ERROR log banners; keep only revision tokens.
        if token.isupper() and token.isalpha():
            continue
        # A revision id is alphanumeric (alembic uses hex hashes or short labels
        # like "005"); anything with other punctuation is a log line, not a rev.
        if token.replace("_", "").isalnum():
            revisions.add(token)
    return revisions


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-230")
def test_migrate_set_services_are_at_alembic_head():
    """Every migrate-set service the CI stack runs has its DB at alembic HEAD.

    This is the keystone assertion against the 2026-06 config-service outage: a
    service whose image/compose forgot to `alembic upgrade head` on startup boots
    with a DB that is EMPTY or BEHIND while its code's `heads` advances. For each
    running migrate-set container we compare `alembic current` to `alembic heads`
    and require them equal — a generator-agnostic check that fails for ANY service
    that skipped its migrations, regardless of which compose generator produced it.

    Services not in this stack's `core` profile are skipped (can't be asserted
    here); each in-stack service is independently skipped if its container isn't up
    in the current lane, so the case degrades cleanly rather than failing on an
    absent service.
    """
    if not _docker_compose_available():
        pytest.skip("docker compose not reachable from the test host — exec checks skipped")

    checked: list[str] = []
    for service in MIGRATE_SET_SERVICES:
        if not _service_is_up(service):
            # Not part of this lane's bring-up; nothing to assert for it here.
            continue
        current = _alembic_revisions(service, "current")
        heads = _alembic_revisions(service, "heads")
        assert heads, (
            f"{service}: `alembic heads` returned no revision — the image ships no "
            f"migrations, so this service should not be in the migrate-set / this "
            f"assertion. stdout parsing found nothing."
        )
        assert current == heads, (
            f"{service}: DB is NOT at alembic head — current={sorted(current)} vs "
            f"heads={sorted(heads)}. This is the 2026-06 config-service failure mode: "
            f"the container booted without running `alembic upgrade head`, so the "
            f"schema is stale (an empty `current` means migrations never ran at all). "
            f"Every query that touches a migrated column will 500. Ensure this "
            f"service runs `alembic upgrade head` on startup (registry `migrate: true` "
            f"→ entrypoint wrapper, or its image CMD)."
        )
        checked.append(service)

    assert checked, (
        "no migrate-set service container was found running in the stack — expected "
        f"at least one of {MIGRATE_SET_SERVICES} to be up when CC_URL is set. "
        f"Check COMPOSE_PROJECT_NAME={COMPOSE_PROJECT_NAME!r} / "
        f"CI_COMPOSE_FILES={CI_COMPOSE_FILES!r} match the lane's bring-up."
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-231")
def test_config_service_services_endpoint_returns_200():
    """`GET /services` on config-service returns HTTP 200 — the exact symptom
    endpoint of the 2026-06 outage.

    `/services` is config-service's open-read registry query — the one CC and every
    other service hits at startup for discovery. When migration 005
    (services.external_host) didn't run, this endpoint 500'd fleet-wide while
    `/health` (which never touches the DB) stayed a cheerful 200. So this case
    asserts the STRICT contract `== 200`, deliberately NOT a loose "200/401/403":
    `/services` requires no auth, so anything other than 200 here is the schema-
    stale failure (or the service being down), which is precisely what `/health`
    would mask. The companion CASE-230 proves the DB is at head via alembic; this
    proves the user-visible read actually works against that schema.
    """
    resp = httpx.get(f"{CONFIG_URL}/services", timeout=15.0)
    assert resp.status_code == 200, (
        f"expected GET /services == 200, got {resp.status_code} "
        f"body={resp.text[:400]} — a 500 here is the 2026-06 fleet-wide outage "
        f"signature: config-service's schema is stale because `alembic upgrade head` "
        f"didn't run on startup, so the registry query fails on a missing column. "
        f"(/health would still be 200 — that's exactly why this case probes /services.)"
    )
    # Shape sanity: the registry read returns a JSON list (or a paginated object
    # wrapping one). We don't pin specific rows — only that a stale schema can't
    # masquerade as a 200 with an error body.
    body = resp.json()
    assert isinstance(body, (list, dict)), (
        f"expected /services to return a JSON list/object, got {type(body).__name__}: "
        f"{str(body)[:200]}"
    )
