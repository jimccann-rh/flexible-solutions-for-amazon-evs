#!/usr/bin/env python3
"""
Update vCenter Distributed Port Group notes with NSX segment subnet data.

Queries NSX Manager for segments matching ``--segmentname``, extracts each
segment's subnet metadata (network, gateway, DHCP server, DHCP range, DNS),
and writes it into the matching vCenter Distributed Port Group's "Notes"
(description) field.

Connection defaults are resolved in order: CLI flags > environment variables >
config files (--config / --edge-spec) > AWS Secrets Manager.

Requirements:
  pip install boto3 pyvmomi requests

Usage:
  # List matching segments with subnet data:
  python update_portgroup_notes.py \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \\
      --profile ci \\
      --segmentname "logical network segment" \\
      --show-segments

  # Dry run — show what notes would be written:
  python update_portgroup_notes.py \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \\
      --profile ci \\
      --segmentname "logical network segment" \\
      --dry-run

  # Apply notes to port groups:
  python update_portgroup_notes.py \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \\
      --profile ci \\
      --segmentname "logical network segment"
"""

import argparse
import json
import logging
import os
import ssl
import sys

import boto3
import requests
import urllib3

from pyVim import connect
from pyVmomi import vim

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LOG = logging.getLogger("update_portgroup_notes")

# Defaults — env-var overrides
NSX_HOST = os.environ.get("NSX_HOST")
NSX_USER = os.environ.get("NSX_USER", "admin")
NSX_PASSWORD = os.environ.get("NSX_PASSWORD")
HTTPS_PROXY = os.environ.get("HTTPS_PROXY", "http://127.0.0.1:9999")


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


# ---------------------------------------------------------------------------
# AWS helpers
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


# ---------------------------------------------------------------------------
# vCenter helpers
# ---------------------------------------------------------------------------

def connect_to_vcenter(host, username, password, port=443):
    ctx = ssl._create_unverified_context()
    return connect.SmartConnect(
        host=host, user=username, pwd=password,
        port=port, sslContext=ctx,
    )


def find_all_dv_portgroups(si):
    """Return a dict mapping port group name -> port group managed object."""
    content = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(
        content.rootFolder,
        [vim.dvs.DistributedVirtualPortgroup],
        True,
    )
    try:
        pg_map = {}
        for pg in container.view:
            pg_map[pg.name] = pg
        return pg_map
    finally:
        container.Destroy()


def ensure_custom_attribute(cfm, name):
    """Create a custom attribute for DistributedVirtualPortgroup if it doesn't exist.

    Returns the field key (integer).
    """
    for field in cfm.field:
        if field.name == name and field.managedObjectType == vim.dvs.DistributedVirtualPortgroup:
            LOG.info("Custom attribute '%s' already exists (key=%d)", name, field.key)
            return field.key

    field_def = cfm.AddCustomFieldDef(
        name=name,
        moType=vim.dvs.DistributedVirtualPortgroup,
    )
    LOG.info("Created custom attribute '%s' (key=%d)", name, field_def.key)
    return field_def.key


def set_custom_attributes(cfm, pg, attributes):
    """Set custom attribute key/value pairs on a port group.

    *attributes* is a list of (field_key, value) tuples.
    """
    for field_key, value in attributes:
        cfm.SetField(entity=pg, key=field_key, value=value)
    LOG.info("Set %d custom attributes on '%s'", len(attributes), pg.name)


# ---------------------------------------------------------------------------
# NSX segment helpers
# ---------------------------------------------------------------------------

def find_matching_segments(client, pattern):
    """Query NSX for segments whose display_name contains *pattern* (case-insensitive)."""
    resp = client.get("/infra/segments")
    if resp.status_code >= 400:
        LOG.error("Failed to list NSX segments: %s", resp.status_code)
        try:
            LOG.error("%s", json.dumps(resp.json(), indent=2))
        except Exception:
            LOG.error("%s", resp.text)
        sys.exit(1)

    pattern_lower = pattern.lower()
    matches = []
    for seg in resp.json().get("results", []):
        if pattern_lower in seg.get("display_name", "").lower():
            matches.append(seg)

    return matches


def extract_subnet_info(segment):
    """Extract subnet metadata from an NSX segment dict."""
    subnets = segment.get("subnets", [])
    if not subnets:
        return None

    s = subnets[0]
    dhcp_cfg = s.get("dhcp_config") or {}
    dhcp_ranges = s.get("dhcp_ranges", [])

    return {
        "network": s.get("network", ""),
        "gateway": s.get("gateway_address", ""),
        "dhcp_server": dhcp_cfg.get("server_address", ""),
        "dhcp_range": ", ".join(dhcp_ranges) if dhcp_ranges else "",
        "dns": ", ".join(dhcp_cfg.get("dns_servers", [])),
    }


def build_notes(info):
    """Format subnet info into a human-readable notes string."""
    lines = []
    lines.append(f"Network:      {info['network']}")
    lines.append(f"Gateway:      {info['gateway']}")
    lines.append(f"DHCP Server:  {info['dhcp_server']}")
    lines.append(f"DHCP Range:   {info['dhcp_range']}")
    lines.append(f"DNS:          {info['dns']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Update vCenter Distributed Port Group notes with NSX segment subnet data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List matching segments with subnet data:
  python update_portgroup_notes.py \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \\
      --profile ci --segmentname "logical network segment" --show-segments

  # Dry run:
  python update_portgroup_notes.py \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \\
      --profile ci --segmentname "logical network segment" --dry-run

  # Apply:
  python update_portgroup_notes.py \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \\
      --profile ci --segmentname "logical network segment"
""",
    )

    parser.add_argument(
        "--segmentname", required=True,
        help="Wildcard search pattern for NSX segment display_names "
             "(case-insensitive substring match)",
    )

    cfg = parser.add_argument_group("config files")
    cfg.add_argument("--config", help="Path to config.json")
    cfg.add_argument("--edge-spec", dest="edge_spec",
                     help="Path to edge_cluster_spec.json")
    cfg.add_argument("--profile", help="AWS CLI profile name")
    cfg.add_argument("--region", help="AWS region override")

    nsx = parser.add_argument_group("NSX connection")
    nsx.add_argument("--nsx-host", dest="nsx_host", default=NSX_HOST,
                     help="NSX Manager hostname (default: from config or $NSX_HOST)")
    nsx.add_argument("--nsx-user", dest="nsx_user", default=NSX_USER,
                     help="NSX username (default: admin)")
    nsx.add_argument("--nsx-password", dest="nsx_password", default=NSX_PASSWORD,
                     help="NSX password (default: from Secrets Manager or $NSX_PASSWORD)")
    nsx.add_argument("--proxy", default=HTTPS_PROXY,
                     help="HTTPS proxy for NSX (default: http://127.0.0.1:9999)")

    vc = parser.add_argument_group("vCenter connection")
    vc.add_argument("--vcenter-host", dest="vcenter_host",
                    help="vCenter hostname (default: from config vcfHostnames.vcenter + fqdn)")
    vc.add_argument("--vcenter-user", dest="vcenter_user",
                    default="administrator@vsphere.local",
                    help="vCenter username (default: administrator@vsphere.local)")

    modes = parser.add_argument_group("modes")
    modes.add_argument("--dry-run", dest="dry_run", action="store_true",
                       help="Show what notes would be written without updating vCenter")
    modes.add_argument("--show-segments", dest="show_segments", action="store_true",
                       help="List matching segments with subnet data and exit")
    modes.add_argument("--showpassword", action="store_true",
                       help="Display credentials on screen")
    modes.add_argument("--verbose", "-v", action="store_true",
                       help="Enable debug logging")

    return parser


# ---------------------------------------------------------------------------
# Connection resolution
# ---------------------------------------------------------------------------

def resolve_nsx_connection(args, config, edge_spec, sm):
    """Fill in NSX host and password from config files / Secrets Manager."""
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

        if env_id and sm:
            secret_id = f"evs-{env_id}_nsxAdmin"
            args.nsx_password = get_secret_password(sm, secret_id)
            LOG.info("NSX password retrieved from Secrets Manager")
        elif not args.nsx_password:
            LOG.error("NSX password not set. Provide --nsx-password, "
                      "$NSX_PASSWORD, or --config for Secrets Manager lookup.")
            sys.exit(1)


def resolve_vcenter_connection(args, config, sm):
    """Fill in vCenter host and password from config / Secrets Manager."""
    env_id = config.get("environmentId") if config else None

    if not args.vcenter_host and config:
        vc_short = config.get("vcfHostnames", {}).get("vcenter", "vc")
        fqdn = config.get("fqdn", "")
        args.vcenter_host = f"{vc_short}.{fqdn}"
        LOG.info("vCenter host (from config): %s", args.vcenter_host)

    if not args.vcenter_host:
        LOG.error("vCenter host not set. Provide --vcenter-host or --config.")
        sys.exit(1)

    if env_id and sm:
        secret_id = f"evs-{env_id}_vcenterSso"
        args._vcenter_password = get_secret_password(sm, secret_id)
        LOG.info("vCenter password retrieved from Secrets Manager")
    else:
        LOG.error("Cannot retrieve vCenter password: need --config with "
                  "environmentId for Secrets Manager lookup.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    # --- Load config files ---
    config = None
    edge_spec = None
    if args.config:
        with open(args.config) as f:
            config = json.load(f)
    if args.edge_spec:
        with open(args.edge_spec) as f:
            edge_spec = json.load(f)

    region = args.region
    if not region and config:
        region = config.get("region", "us-east-1")
    if not region:
        region = "us-east-1"

    # --- AWS session ---
    session_kwargs = {"region_name": region}
    if args.profile:
        session_kwargs["profile_name"] = args.profile
    session = boto3.Session(**session_kwargs)
    sm = session.client("secretsmanager")

    # --- Resolve NSX connection ---
    resolve_nsx_connection(args, config, edge_spec, sm)

    if not args.nsx_host:
        LOG.error("NSX host not set. Provide --nsx-host or --config.")
        sys.exit(1)

    # --- Connect to NSX & find matching segments ---
    LOG.info("Connecting to NSX Manager: %s", args.nsx_host)
    client = NSXClient(
        host=args.nsx_host,
        user=args.nsx_user,
        password=args.nsx_password,
        proxy=args.proxy or None,
    )

    segments = find_matching_segments(client, args.segmentname)

    if not segments:
        print(f"\nNo segments matching '{args.segmentname}'")
        sys.exit(1)

    # --- Extract subnet info from each segment ---
    segment_notes = []
    for seg in segments:
        info = extract_subnet_info(seg)
        if info:
            segment_notes.append({
                "id": seg["id"],
                "display_name": seg["display_name"],
                "info": info,
                "notes": build_notes(info),
            })
        else:
            LOG.warning("Segment '%s' has no subnets — skipping",
                        seg["display_name"])

    print(f"\nFound {len(segment_notes)} segment(s) with subnet data:")
    for sn in segment_notes:
        print(f"\n  Port Group: {sn['display_name']}")
        for line in sn["notes"].split("\n"):
            print(f"    {line}")

    if args.show_segments:
        sys.exit(0)

    if args.dry_run:
        print(f"\n[DRY RUN] Would set custom attributes on "
              f"{len(segment_notes)} port group(s).")
        sys.exit(0)

    # --- Resolve vCenter connection ---
    resolve_vcenter_connection(args, config, sm)

    if args.showpassword:
        print(f"\n  NSX Host     : {args.nsx_host}")
        print(f"  NSX User     : {args.nsx_user}")
        print(f"  NSX Pass     : {args.nsx_password}")
        print(f"  vCenter Host : {args.vcenter_host}")
        print(f"  vCenter User : {args.vcenter_user}")
        print(f"  vCenter Pass : {args._vcenter_password}\n")

    # --- Connect to vCenter ---
    LOG.info("Connecting to vCenter: %s", args.vcenter_host)
    si = connect_to_vcenter(
        args.vcenter_host, args.vcenter_user, args._vcenter_password,
    )

    try:
        content = si.RetrieveContent()
        cfm = content.customFieldsManager

        # --- Ensure custom attribute definitions exist ---
        ATTR_NAMES = ["Network", "Gateway", "DHCP Server", "DHCP Range", "DNS"]
        attr_keys = {}
        for name in ATTR_NAMES:
            attr_keys[name] = ensure_custom_attribute(cfm, name)

        pg_map = find_all_dv_portgroups(si)
        LOG.info("Found %d distributed port groups in vCenter", len(pg_map))

        updated = 0
        not_found = 0

        for sn in segment_notes:
            pg_name = sn["display_name"]
            pg = pg_map.get(pg_name)

            if pg is None:
                LOG.warning("Port group '%s' not found in vCenter — skipping",
                            pg_name)
                print(f"  NOT FOUND  {pg_name}")
                not_found += 1
                continue

            info = sn["info"]
            attributes = [
                (attr_keys["Network"], info["network"]),
                (attr_keys["Gateway"], info["gateway"]),
                (attr_keys["DHCP Server"], info["dhcp_server"]),
                (attr_keys["DHCP Range"], info["dhcp_range"]),
                (attr_keys["DNS"], info["dns"]),
            ]

            print(f"  Updating   {pg_name} ...")
            set_custom_attributes(cfm, pg, attributes)
            updated += 1

    finally:
        connect.Disconnect(si)

    print(f"\nDone: {updated} updated, {not_found} not found")


if __name__ == "__main__":
    main()
