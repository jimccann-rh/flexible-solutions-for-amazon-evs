#!/usr/bin/env python3
"""
Manage the NSX Nested-MAC Discovery Profile and bind it to segments.

Creates a MAC Discovery Profile with MAC learning, MAC change, and unknown
unicast flooding enabled — required for nested VM environments. Can also
bind/unbind the profile to segments matching a wildcard pattern.

Connection defaults are resolved in order: CLI flags > environment variables >
config files (--config / --edge-spec) > AWS Secrets Manager.

Requirements:
  pip install boto3 requests

Usage:
  # Create the Nested-MAC profile:
  python nsx_mac_profile.py profile create \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \\
      --profile ci

  # Show the profile:
  python nsx_mac_profile.py profile show \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \\
      --profile ci

  # Bind to all matching segments:
  python nsx_mac_profile.py bind create \\
      --segmentname "logical network segment" \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \\
      --profile ci

  # Show bindings on matching segments:
  python nsx_mac_profile.py bind show \\
      --segmentname "logical network segment" \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \\
      --profile ci

  # Unbind from matching segments:
  python nsx_mac_profile.py bind delete \\
      --segmentname "logical network segment" \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \\
      --profile ci
"""

import argparse
import json
import logging
import os
import sys

import boto3
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LOG = logging.getLogger("nsx_mac_profile")

# Defaults — env-var overrides
NSX_HOST = os.environ.get("NSX_HOST")
NSX_USER = os.environ.get("NSX_USER", "admin")
NSX_PASSWORD = os.environ.get("NSX_PASSWORD")
HTTPS_PROXY = os.environ.get("HTTPS_PROXY", "http://127.0.0.1:9999")

DEFAULT_PROFILE_ID = "Nested-MAC"
DEFAULT_PROFILE_NAME = "Nested-MAC"
DEFAULT_BINDING_MAP_ID = "nested-mac-binding"


# ---------------------------------------------------------------------------
# NSX client
# ---------------------------------------------------------------------------

class NSXClient:
    def __init__(self, host, user, password, proxy):
        self.base = f"https://{host}"
        self.session = requests.Session()
        self.session.auth = (user, password)
        self.session.verify = False
        self.session.headers["Content-Type"] = "application/json"
        if proxy:
            self.session.proxies = {"https": proxy, "http": proxy}

    def _url(self, path):
        return f"{self.base}/policy/api/v1{path}"

    def get(self, path):
        return self.session.get(self._url(path))

    def put(self, path, payload):
        return self.session.put(self._url(path), json=payload)

    def patch(self, path, payload):
        return self.session.patch(self._url(path), json=payload)

    def delete(self, path):
        return self.session.delete(self._url(path))


def pp(data):
    print(json.dumps(data, indent=2))


def check_response(resp, action):
    if resp.status_code >= 400:
        print(f"ERROR {action}: {resp.status_code}", file=sys.stderr)
        try:
            pp(resp.json())
        except Exception:
            print(resp.text, file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# AWS / config helpers
# ---------------------------------------------------------------------------

def get_secret_password(sm, secret_id):
    """Retrieve a password from Secrets Manager."""
    LOG.info("Fetching secret %s", secret_id)
    response = sm.get_secret_value(SecretId=secret_id)
    raw = response.get("SecretString", "")
    try:
        data = json.loads(raw)
        return data.get("password", raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def resolve_defaults_from_config(args):
    """Load config.json and edge_cluster_spec.json to fill in missing values."""
    config = None
    edge_spec = None

    if args.config:
        with open(args.config) as f:
            config = json.load(f)

    if args.edge_spec:
        with open(args.edge_spec) as f:
            edge_spec = json.load(f)

    if not args.nsx_host and config:
        nsx_short = config.get("vcfHostnames", {}).get("nsx", "nsx")
        fqdn = config.get("fqdn", "")
        args.nsx_host = f"{nsx_short}.{fqdn}"
        LOG.info("NSX host (from config): %s", args.nsx_host)

    if not args.nsx_password:
        env_id = None
        if config:
            env_id = config.get("environmentId")
        elif edge_spec:
            env_id = edge_spec.get("environmentId")

        if env_id:
            region = args.region or (config or {}).get("region", "us-east-1")
            session_kwargs = {"region_name": region}
            if args.profile:
                session_kwargs["profile_name"] = args.profile
            sm = boto3.Session(**session_kwargs).client("secretsmanager")
            secret_id = f"evs-{env_id}_nsxAdmin"
            args.nsx_password = get_secret_password(sm, secret_id)
            LOG.info("NSX password retrieved from Secrets Manager")
        else:
            print(
                "ERROR: NSX password not set. Provide --nsx-password, "
                "NSX_PASSWORD env var, or --config/--edge-spec for "
                "Secrets Manager lookup.",
                file=sys.stderr,
            )
            sys.exit(1)

    missing = []
    if not args.nsx_host:
        missing.append("--nsx-host (or --config)")
    if missing:
        print(f"ERROR: missing required values: {', '.join(missing)}",
              file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Segment helpers
# ---------------------------------------------------------------------------

def find_matching_segments(client, pattern):
    """Query NSX for segments whose display_name contains *pattern* (case-insensitive)."""
    resp = client.get("/infra/segments")
    if resp.status_code >= 400:
        print(f"ERROR listing segments: {resp.status_code}", file=sys.stderr)
        sys.exit(1)

    pattern_lower = pattern.lower()
    matches = []
    for seg in resp.json().get("results", []):
        if pattern_lower in seg.get("display_name", "").lower():
            matches.append(seg)

    return matches


# ---------------------------------------------------------------------------
# Profile operations
# ---------------------------------------------------------------------------

def profile_create(client, args):
    """Create the Nested-MAC Discovery Profile."""
    payload = {
        "resource_type": "MacDiscoveryProfile",
        "display_name": args.profile_name,
        "mac_change_enabled": True,
        "mac_learning_enabled": True,
        "mac_learning_aging_time": args.aging_time,
        "unknown_unicast_flooding_enabled": True,
        "mac_limit": args.mac_limit,
        "mac_limit_policy": "ALLOW",
        "remote_overlay_mac_limit": args.remote_mac_limit,
    }

    LOG.info("Creating MAC Discovery Profile: %s", args.profile_id)
    resp = client.put(
        f"/infra/mac-discovery-profiles/{args.profile_id}",
        payload,
    )
    check_response(resp, f"create profile '{args.profile_id}'")
    print(f"Profile '{args.profile_id}' created")
    pp(resp.json())


def profile_show(client, args):
    """Show the Nested-MAC Discovery Profile."""
    resp = client.get(
        f"/infra/mac-discovery-profiles/{args.profile_id}",
    )
    if resp.status_code == 404:
        print(f"Profile '{args.profile_id}' not found")
        return
    check_response(resp, f"show profile '{args.profile_id}'")
    pp(resp.json())


def profile_delete(client, args):
    """Delete the Nested-MAC Discovery Profile."""
    LOG.info("Deleting MAC Discovery Profile: %s", args.profile_id)
    resp = client.delete(
        f"/infra/mac-discovery-profiles/{args.profile_id}",
    )
    check_response(resp, f"delete profile '{args.profile_id}'")
    print(f"Profile '{args.profile_id}' deleted")


# ---------------------------------------------------------------------------
# Binding operations
# ---------------------------------------------------------------------------

def get_existing_binding_map(client, seg_id):
    """Find the existing binding map on a segment, if any.

    NSX allows only one binding map per segment. Returns (map_id, data) or
    (None, None) if no binding map exists.
    """
    resp = client.get(
        f"/infra/segments/{seg_id}/segment-discovery-profile-binding-maps"
    )
    if resp.status_code >= 400:
        return None, None

    results = resp.json().get("results", [])
    if results:
        bm = results[0]
        return bm["id"], bm
    return None, None


def bind_create(client, args):
    """Bind the Nested-MAC profile to matching segments."""
    segments = find_matching_segments(client, args.segmentname)
    if not segments:
        print(f"No segments matching '{args.segmentname}'")
        sys.exit(1)

    mac_profile_path = f"/infra/mac-discovery-profiles/{args.profile_id}"
    print(f"Binding profile '{args.profile_id}' to {len(segments)} segment(s):\n")

    for seg in segments:
        seg_id = seg["id"]
        seg_name = seg["display_name"]

        existing_id, existing_data = get_existing_binding_map(client, seg_id)

        if existing_id:
            if existing_data.get("mac_discovery_profile_path") == mac_profile_path:
                print(f"  {seg_name:<45} already bound — skipped")
                continue

            binding_path = (
                f"/infra/segments/{seg_id}"
                f"/segment-discovery-profile-binding-maps/{existing_id}"
            )
            existing_data["mac_discovery_profile_path"] = mac_profile_path
            LOG.info("Updating existing binding map '%s' on segment '%s'",
                     existing_id, seg_name)
            resp = client.put(binding_path, existing_data)
            check_response(resp, f"update binding on '{seg_name}'")
            print(f"  UPDATED  {seg_name}  (map: {existing_id})")
        else:
            binding_path = (
                f"/infra/segments/{seg_id}"
                f"/segment-discovery-profile-binding-maps/{args.binding_map_id}"
            )
            payload = {
                "mac_discovery_profile_path": mac_profile_path,
            }
            LOG.info("Creating new binding map on segment '%s'", seg_name)
            resp = client.put(binding_path, payload)
            check_response(resp, f"bind to '{seg_name}'")
            print(f"  BOUND    {seg_name}")

    print(f"\nDone: {len(segments)} segment(s) bound")


def bind_show(client, args):
    """Show binding status on matching segments."""
    segments = find_matching_segments(client, args.segmentname)
    if not segments:
        print(f"No segments matching '{args.segmentname}'")
        sys.exit(1)

    print(f"Binding status for {len(segments)} segment(s):\n")

    for seg in segments:
        seg_id = seg["id"]
        seg_name = seg["display_name"]

        existing_id, existing_data = get_existing_binding_map(client, seg_id)

        if existing_data:
            mac_profile = existing_data.get("mac_discovery_profile_path", "none")
            print(f"  {seg_name:<45} -> {mac_profile}  (map: {existing_id})")
        else:
            print(f"  {seg_name:<45} NOT BOUND")


def bind_delete(client, args):
    """Remove the MAC profile from the binding map on matching segments.

    If the segment has an existing binding map, the mac_discovery_profile_path
    is cleared (set to empty string) rather than deleting the entire binding
    map, which would also remove IP/segment discovery profile bindings.
    """
    segments = find_matching_segments(client, args.segmentname)
    if not segments:
        print(f"No segments matching '{args.segmentname}'")
        sys.exit(1)

    print(f"Unbinding MAC profile from {len(segments)} segment(s):\n")

    for seg in segments:
        seg_id = seg["id"]
        seg_name = seg["display_name"]

        existing_id, existing_data = get_existing_binding_map(client, seg_id)

        if not existing_data:
            print(f"  {seg_name:<45} no binding map — skipped")
            continue

        if not existing_data.get("mac_discovery_profile_path"):
            print(f"  {seg_name:<45} no MAC profile set — skipped")
            continue

        binding_path = (
            f"/infra/segments/{seg_id}"
            f"/segment-discovery-profile-binding-maps/{existing_id}"
        )
        existing_data.pop("mac_discovery_profile_path", None)
        resp = client.put(binding_path, existing_data)
        check_response(resp, f"unbind from '{seg_name}'")
        print(f"  UNBOUND  {seg_name}  (map: {existing_id})")

    print(f"\nDone: {len(segments)} segment(s) processed")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_connection_args(parser):
    conn = parser.add_argument_group("NSX connection")
    conn.add_argument("--nsx-host", dest="nsx_host", default=NSX_HOST)
    conn.add_argument("--nsx-user", dest="nsx_user", default=NSX_USER)
    conn.add_argument("--nsx-password", dest="nsx_password", default=NSX_PASSWORD)
    conn.add_argument("--proxy", default=HTTPS_PROXY)

    cfg = parser.add_argument_group("config files")
    cfg.add_argument("--config", help="Path to config.json")
    cfg.add_argument("--edge-spec", dest="edge_spec",
                     help="Path to edge_cluster_spec.json")
    cfg.add_argument("--profile", help="AWS CLI profile name")
    cfg.add_argument("--region", help="AWS region override")

    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Manage NSX Nested-MAC Discovery Profile and segment bindings",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create the profile:
  python nsx_mac_profile.py profile create \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json --profile ci

  # Bind to all matching segments:
  python nsx_mac_profile.py bind create \\
      --segmentname "logical network segment" \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json --profile ci

  # Show bindings:
  python nsx_mac_profile.py bind show \\
      --segmentname "logical network segment" \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json --profile ci

  # Unbind and delete:
  python nsx_mac_profile.py bind delete \\
      --segmentname "logical network segment" \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json --profile ci
  python nsx_mac_profile.py profile delete \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json --profile ci
""",
    )

    sub = parser.add_subparsers(dest="resource", required=True)

    # ---- profile ----
    prof = sub.add_parser("profile", help="Manage the MAC Discovery Profile")
    prof_sub = prof.add_subparsers(dest="action", required=True)

    p = prof_sub.add_parser("create", help="Create the Nested-MAC profile")
    add_connection_args(p)
    p.add_argument("--profile-id", dest="profile_id", default=DEFAULT_PROFILE_ID,
                   help=f"Profile ID (default: {DEFAULT_PROFILE_ID})")
    p.add_argument("--profile-name", dest="profile_name", default=DEFAULT_PROFILE_NAME,
                   help=f"Profile display name (default: {DEFAULT_PROFILE_NAME})")
    p.add_argument("--mac-limit", dest="mac_limit", type=int, default=4096,
                   help="MAC address table limit (default: 4096)")
    p.add_argument("--remote-mac-limit", dest="remote_mac_limit", type=int, default=2048,
                   help="Remote overlay MAC limit (default: 2048)")
    p.add_argument("--aging-time", dest="aging_time", type=int, default=600,
                   help="MAC learning aging time in seconds (default: 600)")
    p.set_defaults(func=profile_create)

    p = prof_sub.add_parser("show", help="Show the Nested-MAC profile")
    add_connection_args(p)
    p.add_argument("--profile-id", dest="profile_id", default=DEFAULT_PROFILE_ID)
    p.set_defaults(func=profile_show)

    p = prof_sub.add_parser("delete", help="Delete the Nested-MAC profile")
    add_connection_args(p)
    p.add_argument("--profile-id", dest="profile_id", default=DEFAULT_PROFILE_ID)
    p.set_defaults(func=profile_delete)

    # ---- bind ----
    bind = sub.add_parser("bind", help="Bind/unbind the profile to segments")
    bind_sub = bind.add_subparsers(dest="action", required=True)

    p = bind_sub.add_parser("create", help="Bind profile to matching segments")
    add_connection_args(p)
    p.add_argument("--segmentname", required=True,
                   help="Wildcard search for segment display_names")
    p.add_argument("--profile-id", dest="profile_id", default=DEFAULT_PROFILE_ID)
    p.add_argument("--binding-map-id", dest="binding_map_id",
                   default=DEFAULT_BINDING_MAP_ID,
                   help=f"Binding map ID (default: {DEFAULT_BINDING_MAP_ID})")
    p.set_defaults(func=bind_create)

    p = bind_sub.add_parser("show", help="Show binding status on matching segments")
    add_connection_args(p)
    p.add_argument("--segmentname", required=True,
                   help="Wildcard search for segment display_names")
    p.add_argument("--profile-id", dest="profile_id", default=DEFAULT_PROFILE_ID)
    p.add_argument("--binding-map-id", dest="binding_map_id",
                   default=DEFAULT_BINDING_MAP_ID)
    p.set_defaults(func=bind_show)

    p = bind_sub.add_parser("delete", help="Unbind profile from matching segments")
    add_connection_args(p)
    p.add_argument("--segmentname", required=True,
                   help="Wildcard search for segment display_names")
    p.add_argument("--profile-id", dest="profile_id", default=DEFAULT_PROFILE_ID)
    p.add_argument("--binding-map-id", dest="binding_map_id",
                   default=DEFAULT_BINDING_MAP_ID)
    p.set_defaults(func=bind_delete)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    resolve_defaults_from_config(args)

    client = NSXClient(
        host=args.nsx_host,
        user=args.nsx_user,
        password=args.nsx_password,
        proxy=args.proxy or None,
    )

    args.func(client, args)


if __name__ == "__main__":
    main()
