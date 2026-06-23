#!/usr/bin/env python3
"""Assert — VIA command-center — that the mobile e2e provisioning flow actually
registered a node, and (optionally) that it came online.

WHY: the mobile app's Success screen is NOT trustworthy — it reports success even
when the node errored. The authoritative signal that provisioning worked
end-to-end is the node appearing in command-center's node registry: the app
fetched a provisioning token from CC, handed it (+ CC's URL) to the fake node, and
the fake node registered itself back with CC (POST /api/v0/nodes/register). This
script polls CC's admin node list and asserts that chain completed.

  registered  (default)  : >=1 node is registered for the household. This proves
                           app -> fake-node -> CC registration succeeded. It is the
                           floor signal the mobile-e2e lane gates on.
  --require-online        : additionally require >=1 node whose CC `online` flag is
                           true (node.is_online() = last_seen within 15 min). The
                           provisioning SERVER does not heartbeat like the node
                           runtime, so online may lag registration — keep this OFF
                           until shakeout confirms the fake node sends a heartbeat
                           (see docs/mobile-e2e.md).

Stdlib only (mirrors tools/resolve_cross_repo.py / gen_case_catalog.py — the
harness pulls in no third-party client deps for its tooling).

Usage:
  python tools/assert_node_online.py \
      --cc-url http://localhost:7703 --jwt "$CC_USER_JWT" \
      --household "$CC_HOUSEHOLD_ID" [--require-online] [--timeout 60]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def _list_nodes(cc_url: str, jwt: str, household: str | None) -> list[dict]:
    """GET /api/v0/admin/nodes[?household_id=...] as the seeded (superuser) user."""
    path = "/api/v0/admin/nodes"
    if household:
        path += "?" + urllib.parse.urlencode({"household_id": household})
    req = urllib.request.Request(
        cc_url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {jwt}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (trusted CI URL)
        body = resp.read().decode("utf-8")
    data = json.loads(body)
    if not isinstance(data, list):
        raise ValueError(f"expected a JSON list from {path}, got {type(data).__name__}")
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description="Assert a node registered/online via command-center")
    ap.add_argument("--cc-url", required=True, help="command-center base URL (e.g. http://localhost:7703)")
    ap.add_argument("--jwt", required=True, help="superuser/member JWT (seed.sh exports CC_USER_JWT)")
    ap.add_argument("--household", default=None, help="household UUID to scope to (CC_HOUSEHOLD_ID)")
    ap.add_argument("--require-online", action="store_true",
                    help="also require >=1 node with CC online=true (off by default — see module docstring)")
    ap.add_argument("--timeout", type=float, default=60.0, help="seconds to poll before failing")
    ap.add_argument("--interval", type=float, default=3.0, help="seconds between polls")
    args = ap.parse_args()

    deadline = args.timeout
    waited = 0.0
    last_err = ""
    while True:
        try:
            nodes = _list_nodes(args.cc_url, args.jwt, args.household)
            registered = len(nodes)
            online = sum(1 for n in nodes if n.get("online") is True)
            ids = ", ".join(n.get("node_id", "?") for n in nodes) or "(none)"
            ok = registered >= 1 and (not args.require_online or online >= 1)
            if ok:
                print(f"PASS: {registered} node(s) registered via CC, {online} online "
                      f"[{ids}]")
                return 0
            last_err = (f"{registered} registered / {online} online "
                        f"(need >=1 registered{' + >=1 online' if args.require_online else ''})")
        except (urllib.error.URLError, ValueError, json.JSONDecodeError, OSError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"

        if waited >= deadline:
            print(f"FAIL: no node provisioned via CC within {args.timeout:.0f}s — {last_err}",
                  file=sys.stderr)
            return 1
        time.sleep(args.interval)
        waited += args.interval


if __name__ == "__main__":
    sys.exit(main())
