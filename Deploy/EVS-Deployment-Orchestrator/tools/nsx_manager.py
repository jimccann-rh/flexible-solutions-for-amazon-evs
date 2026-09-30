#!/usr/bin/env python3
"""NSX-T Segment, DHCP, and SNAT management CLI for AWS EVS environments.

Connection defaults are resolved in order: CLI flags > environment variables >
config files (--config / --edge-spec) > Secrets Manager (password only).
"""

import argparse
import ipaddress
import json
import logging
import os
import sys

import boto3
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LOG = logging.getLogger("nsx_manager")

# ---------------------------------------------------------------------------
# Defaults — env-var overrides; None means "resolve from config files"
# ---------------------------------------------------------------------------
NSX_HOST = os.environ.get("NSX_HOST")
NSX_USER = os.environ.get("NSX_USER", "admin")
NSX_PASSWORD = os.environ.get("NSX_PASSWORD")
HTTPS_PROXY = os.environ.get("HTTPS_PROXY", "http://127.0.0.1:9999")

TIER1_ID = None
TRANSPORT_ZONE = None
EDGE_CLUSTER = None

DEFAULT_SEGMENT_ID = "logical_network_segment_1"
DEFAULT_SEGMENT_NAME = "logical network segment 1"
DEFAULT_GATEWAY = "10.28.40.1/25"
DEFAULT_NETWORK = "10.28.40.0/25"
DEFAULT_DHCP_RANGE = "10.28.40.50-10.28.40.120"
DEFAULT_DHCP_SERVER_ADDR = "10.28.40.2/25"
DEFAULT_DNS = "10.28.32.100"
DEFAULT_LEASE_TIME = 86400

DEFAULT_DHCP_ID = "DHCP_Server_1"
DEFAULT_DHCP_NAME = "DHCP Server 1"
DEFAULT_DHCP_LISTEN = "100.96.0.1/30"

DEFAULT_SNAT_ID = "snat-overlay-to-internet"
DEFAULT_SNAT_NAME = "SNAT-overlay-to-internet"
DEFAULT_SNAT_SOURCE = "10.28.40.0/21"
DEFAULT_SNAT_TRANSLATED = "10.28.41.10"

DEFAULT_NOSNAT_ID = "no-snat-east-west"
DEFAULT_NOSNAT_NAME = "NO-SNAT-east-west"
DEFAULT_NOSNAT_SOURCE = "10.28.40.0/21"
DEFAULT_NOSNAT_DEST = "10.0.0.0/8"


# ---------------------------------------------------------------------------
# HTTP helpers
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
        r = self.session.get(self._url(path))
        return r

    def put(self, path, payload):
        r = self.session.put(self._url(path), json=payload)
        return r

    def patch(self, path, payload):
        r = self.session.patch(self._url(path), json=payload)
        return r

    def delete(self, path):
        r = self.session.delete(self._url(path))
        return r


def resolve_edge_cluster(client, name_or_path):
    if name_or_path.startswith("/infra/"):
        return name_or_path
    resp = client.get(
        "/infra/sites/default/enforcement-points/default/edge-clusters"
    )
    if resp.status_code >= 400:
        print(f"ERROR: could not list edge clusters: {resp.status_code}", file=sys.stderr)
        sys.exit(1)
    for ec in resp.json().get("results", []):
        if ec["display_name"] == name_or_path or ec["id"] == name_or_path:
            return ec["path"]
    print(f"ERROR: edge cluster '{name_or_path}' not found", file=sys.stderr)
    sys.exit(1)


def resolve_transport_zone(client, name_or_path):
    if name_or_path.startswith("/infra/"):
        return name_or_path
    resp = client.get(
        "/infra/sites/default/enforcement-points/default/transport-zones"
    )
    if resp.status_code >= 400:
        print(f"ERROR: could not list transport zones: {resp.status_code}", file=sys.stderr)
        sys.exit(1)
    for tz in resp.json().get("results", []):
        if tz["display_name"] == name_or_path or tz["id"] == name_or_path:
            return tz["path"]
    print(f"ERROR: transport zone '{name_or_path}' not found", file=sys.stderr)
    sys.exit(1)


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
# Config / secret helpers
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

    # --- NSX host from config.json ---
    if not args.nsx_host and config:
        nsx_short = config.get("vcfHostnames", {}).get("nsx", "nsx")
        fqdn = config.get("fqdn", "")
        args.nsx_host = f"{nsx_short}.{fqdn}"
        LOG.info("NSX host (from config): %s", args.nsx_host)

    # --- Tier-0, Tier-1, edge cluster, transport zone from edge_cluster_spec.json ---
    if edge_spec:
        if not getattr(args, "tier0_id", None):
            args.tier0_id = edge_spec.get("tier0", {}).get("name")
            if args.tier0_id:
                LOG.info("Tier-0 (from edge spec): %s", args.tier0_id)
        if not args.tier1_id:
            args.tier1_id = edge_spec.get("tier1", {}).get("name")
            LOG.info("Tier-1 (from edge spec): %s", args.tier1_id)
        if not args.edge_cluster:
            args.edge_cluster = edge_spec.get("edgeClusterName")
            LOG.info("Edge cluster (from edge spec): %s", args.edge_cluster)
        if not args.transport_zone:
            args.transport_zone = edge_spec.get(
                "transportZones", {},
            ).get("overlay")
            LOG.info("Transport zone (from edge spec): %s",
                     args.transport_zone)

    # --- DNS servers from edge spec ---
    args._dns_servers = None
    if edge_spec:
        nodes = edge_spec.get("edgeNodes", [])
        if nodes and nodes[0].get("dnsServers"):
            args._dns_servers = nodes[0]["dnsServers"]
            LOG.info("DNS servers (from edge spec): %s", args._dns_servers)

    # --- NSX password from Secrets Manager ---
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

    # Validate required values
    missing = []
    if not args.nsx_host:
        missing.append("--nsx-host (or --config)")
    if not args.tier1_id:
        missing.append("--tier1-id (or --edge-spec)")
    if not args.edge_cluster:
        missing.append("--edge-cluster (or --edge-spec)")
    if not args.transport_zone:
        missing.append("--transport-zone (or --edge-spec)")
    if missing:
        print(f"ERROR: missing required values: {', '.join(missing)}",
              file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subnet computation for batch segment creation
# ---------------------------------------------------------------------------

def compute_subnets(overlay_cidr, segment_prefix):
    """Subdivide *overlay_cidr* into subnets of *segment_prefix* length.

    Returns a list of dicts, one per subnet, with all the per-segment values
    needed to create an NSX segment + inline DHCP config.
    """
    network = ipaddress.ip_network(overlay_cidr, strict=False)
    subnets = list(network.subnets(new_prefix=segment_prefix))
    result = []
    for idx, subnet in enumerate(subnets, 1):
        base = int(subnet.network_address)
        prefix = subnet.prefixlen
        ip = lambda offset: str(ipaddress.IPv4Address(base + offset))

        result.append({
            "index": idx,
            "segment_id": f"logical_network_segment_{idx}",
            "segment_name": f"logical network segment {idx}",
            "network": str(subnet),
            "gateway": f"{ip(1)}/{prefix}",
            "dhcp_server_addr": f"{ip(2)}/{prefix}",
            "dhcp_range": f"{ip(50)}-{ip(120)}",
        })
    return result


# ---------------------------------------------------------------------------
# DHCP server config
# ---------------------------------------------------------------------------
def dhcp_create(client, args):
    dhcp_id = args.dhcp_id
    edge_cluster_path = resolve_edge_cluster(client, args.edge_cluster)
    payload = {
        "display_name": args.dhcp_name,
        "server_address": args.dhcp_listen,
        "server_addresses": [args.dhcp_listen],
        "lease_time": args.lease_time,
        "edge_cluster_path": edge_cluster_path,
        "resource_type": "DhcpServerConfig",
    }
    resp = client.put(f"/infra/dhcp-server-configs/{dhcp_id}", payload)
    check_response(resp, "dhcp create")
    print(f"DHCP server config '{dhcp_id}' created.")
    pp(resp.json())


def dhcp_show(client, args):
    dhcp_id = args.dhcp_id
    if dhcp_id:
        resp = client.get(f"/infra/dhcp-server-configs/{dhcp_id}")
        check_response(resp, "dhcp show")
        pp(resp.json())
    else:
        resp = client.get("/infra/dhcp-server-configs")
        check_response(resp, "dhcp list")
        for item in resp.json().get("results", []):
            print(f"  {item['id']:30s}  {item['display_name']}")


def dhcp_delete(client, args):
    dhcp_id = args.dhcp_id
    resp = client.delete(f"/infra/dhcp-server-configs/{dhcp_id}")
    check_response(resp, "dhcp delete")
    print(f"DHCP server config '{dhcp_id}' deleted.")


# ---------------------------------------------------------------------------
# Segment
# ---------------------------------------------------------------------------
def segment_create(client, args):
    seg_id = args.segment_id
    gateway = args.gateway
    network = args.network
    dhcp_range = args.dhcp_range
    dns = args.dns
    dhcp_server_addr = args.dhcp_server_addr
    dhcp_id = args.dhcp_id
    transport_zone_path = resolve_transport_zone(client, args.transport_zone)

    payload = {
        "display_name": args.segment_name,
        "type": "ROUTED",
        "subnets": [
            {
                "gateway_address": gateway,
                "dhcp_ranges": [dhcp_range],
                "dhcp_config": {
                    "resource_type": "SegmentDhcpV4Config",
                    "server_address": dhcp_server_addr,
                    "lease_time": args.lease_time,
                    "dns_servers": [dns],
                },
                "network": network,
            }
        ],
        "connectivity_path": f"/infra/tier-1s/{args.tier1_id}",
        "transport_zone_path": transport_zone_path,
        "dhcp_config_path": f"/infra/dhcp-server-configs/{dhcp_id}",
        "admin_state": "UP",
        "replication_mode": "MTEP",
        "advanced_config": {
            "hybrid": False,
            "multicast": True,
            "inter_router": False,
            "local_egress": False,
            "urpf_mode": "STRICT",
            "connectivity": "ON",
        },
    }
    resp = client.put(f"/infra/segments/{seg_id}", payload)
    check_response(resp, "segment create")
    print(f"Segment '{seg_id}' created.")
    pp(resp.json())


def segment_show(client, args):
    seg_id = args.segment_id
    if seg_id:
        resp = client.get(f"/infra/segments/{seg_id}")
        check_response(resp, "segment show")
        pp(resp.json())
    else:
        resp = client.get("/infra/segments")
        check_response(resp, "segment list")
        for item in resp.json().get("results", []):
            subnet_info = ""
            for s in item.get("subnets", []):
                subnet_info = s.get("network", "")
            print(
                f"  {item['id']:40s}  {item['display_name']:30s}  {subnet_info}"
            )


def segment_delete(client, args):
    seg_id = args.segment_id
    resp = client.delete(f"/infra/segments/{seg_id}")
    check_response(resp, "segment delete")
    print(f"Segment '{seg_id}' deleted.")


# ---------------------------------------------------------------------------
# SNAT
# ---------------------------------------------------------------------------
def snat_create(client, args):
    tier1 = args.tier1_id
    snat_id = args.snat_id
    payload = {
        "display_name": args.snat_name,
        "action": "SNAT",
        "source_network": args.source,
        "translated_network": args.translated,
        "enabled": True,
        "logging": False,
        "firewall_match": "MATCH_INTERNAL_ADDRESS",
        "sequence_number": 100,
    }
    resp = client.put(
        f"/infra/tier-1s/{tier1}/nat/USER/nat-rules/{snat_id}", payload
    )
    check_response(resp, "snat create")
    print(f"SNAT rule '{snat_id}' created on tier-1 '{tier1}'.")
    pp(resp.json())


def snat_show(client, args):
    tier1 = args.tier1_id
    snat_id = args.snat_id
    if snat_id:
        resp = client.get(
            f"/infra/tier-1s/{tier1}/nat/USER/nat-rules/{snat_id}"
        )
        check_response(resp, "snat show")
        pp(resp.json())
    else:
        resp = client.get(f"/infra/tier-1s/{tier1}/nat/USER/nat-rules")
        check_response(resp, "snat list")
        for item in resp.json().get("results", []):
            print(
                f"  {item['id']:30s}  {item.get('source_network',''):20s} -> "
                f"{item.get('translated_network',''):20s}  {item['action']}"
            )


def snat_delete(client, args):
    tier1 = args.tier1_id
    snat_id = args.snat_id
    resp = client.delete(
        f"/infra/tier-1s/{tier1}/nat/USER/nat-rules/{snat_id}"
    )
    check_response(resp, "snat delete")
    print(f"SNAT rule '{snat_id}' deleted from tier-1 '{tier1}'.")


# ---------------------------------------------------------------------------
# NO_SNAT (east-west bypass)
# ---------------------------------------------------------------------------
def nosnat_create(client, args):
    tier1 = args.tier1_id
    nosnat_id = args.nosnat_id
    payload = {
        "display_name": args.nosnat_name,
        "action": "NO_SNAT",
        "source_network": args.nosnat_source,
        "destination_network": args.nosnat_dest,
        "enabled": True,
        "logging": False,
        "firewall_match": "MATCH_INTERNAL_ADDRESS",
        "sequence_number": 10,
    }
    resp = client.put(
        f"/infra/tier-1s/{tier1}/nat/USER/nat-rules/{nosnat_id}", payload
    )
    check_response(resp, "nosnat create")
    print(f"NO_SNAT rule '{nosnat_id}' created on tier-1 '{tier1}'.")
    pp(resp.json())


def nosnat_show(client, args):
    tier1 = args.tier1_id
    nosnat_id = args.nosnat_id
    if nosnat_id:
        resp = client.get(
            f"/infra/tier-1s/{tier1}/nat/USER/nat-rules/{nosnat_id}"
        )
        check_response(resp, "nosnat show")
        pp(resp.json())
    else:
        resp = client.get(f"/infra/tier-1s/{tier1}/nat/USER/nat-rules")
        check_response(resp, "nosnat list")
        for item in resp.json().get("results", []):
            if item["action"] == "NO_SNAT":
                print(
                    f"  {item['id']:30s}  src={item.get('source_network',''):18s} "
                    f"dst={item.get('destination_network',''):18s}  {item['action']}"
                )


def nosnat_delete(client, args):
    tier1 = args.tier1_id
    nosnat_id = args.nosnat_id
    resp = client.delete(
        f"/infra/tier-1s/{tier1}/nat/USER/nat-rules/{nosnat_id}"
    )
    check_response(resp, "nosnat delete")
    print(f"NO_SNAT rule '{nosnat_id}' deleted from tier-1 '{tier1}'.")


# ---------------------------------------------------------------------------
# BGP route aggregation on Tier-0
# ---------------------------------------------------------------------------

BGP_LOCALE_SERVICE_ID = "default"


def _resolve_tier0_id(args):
    """Return the Tier-0 ID from args or abort."""
    t0 = getattr(args, "tier0_id", None)
    if not t0:
        print(
            "ERROR: Tier-0 ID not set. Provide --tier0-id or "
            "--edge-spec with a tier0.name entry.",
            file=sys.stderr,
        )
        sys.exit(1)
    return t0


def bgp_aggregation_add(client, args, prefix, summary_only=False):
    """Add a route aggregation entry to the Tier-0 BGP config."""
    t0 = _resolve_tier0_id(args)
    bgp_path = (
        f"/infra/tier-0s/{t0}/locale-services/"
        f"{BGP_LOCALE_SERVICE_ID}/bgp"
    )

    resp = client.get(bgp_path)
    check_response(resp, "get BGP config")
    bgp_config = resp.json()

    aggregations = bgp_config.get("route_aggregations", [])

    for entry in aggregations:
        if entry.get("prefix") == prefix:
            print(f"BGP route aggregation '{prefix}' already exists — skipped")
            return

    aggregations.append({
        "prefix": prefix,
        "summary_only": summary_only,
    })
    bgp_config["route_aggregations"] = aggregations

    resp = client.put(bgp_path, bgp_config)
    check_response(resp, f"add BGP route aggregation '{prefix}'")
    print(f"BGP route aggregation added: {prefix} (summary_only={summary_only})")


def bgp_aggregation_show(client, args):
    """Show BGP route aggregation entries on the Tier-0."""
    t0 = _resolve_tier0_id(args)
    bgp_path = (
        f"/infra/tier-0s/{t0}/locale-services/"
        f"{BGP_LOCALE_SERVICE_ID}/bgp"
    )

    resp = client.get(bgp_path)
    check_response(resp, "get BGP config")
    bgp_config = resp.json()

    aggregations = bgp_config.get("route_aggregations", [])
    if not aggregations:
        print("No BGP route aggregation entries")
        return

    print(f"BGP route aggregation on Tier-0 '{t0}':\n")
    for entry in aggregations:
        prefix = entry.get("prefix", "")
        summary = entry.get("summary_only", False)
        print(f"  {prefix:<25} summary_only={summary}")


def bgp_aggregation_delete(client, args, prefix):
    """Remove a route aggregation entry from the Tier-0 BGP config."""
    t0 = _resolve_tier0_id(args)
    bgp_path = (
        f"/infra/tier-0s/{t0}/locale-services/"
        f"{BGP_LOCALE_SERVICE_ID}/bgp"
    )

    resp = client.get(bgp_path)
    check_response(resp, "get BGP config")
    bgp_config = resp.json()

    aggregations = bgp_config.get("route_aggregations", [])
    original_count = len(aggregations)

    aggregations = [e for e in aggregations if e.get("prefix") != prefix]

    if len(aggregations) == original_count:
        print(f"BGP route aggregation '{prefix}' not found — skipped")
        return

    bgp_config["route_aggregations"] = aggregations

    resp = client.put(bgp_path, bgp_config)
    check_response(resp, f"delete BGP route aggregation '{prefix}'")
    print(f"BGP route aggregation deleted: {prefix}")


# ---------------------------------------------------------------------------
# All-in-one helpers
# ---------------------------------------------------------------------------
def all_create(client, args):
    print("=== Creating DHCP server config ===")
    dhcp_create(client, args)
    print()
    print("=== Creating segment ===")
    segment_create(client, args)
    print()
    print("=== Creating NO_SNAT rule (east-west bypass) ===")
    nosnat_create(client, args)
    print()
    print("=== Creating SNAT rule ===")
    snat_create(client, args)


def all_show(client, args):
    print("=== DHCP server config ===")
    dhcp_show(client, args)
    print()
    print("=== Segment ===")
    segment_show(client, args)
    print()
    print("=== NO_SNAT rule ===")
    nosnat_show(client, args)
    print()
    print("=== SNAT rule ===")
    snat_show(client, args)


def all_delete(client, args):
    print("=== Deleting SNAT rule ===")
    snat_delete(client, args)
    print()
    print("=== Deleting NO_SNAT rule ===")
    nosnat_delete(client, args)
    print()
    print("=== Deleting segment ===")
    segment_delete(client, args)
    print()
    print("=== Deleting DHCP server config ===")
    dhcp_delete(client, args)


# ---------------------------------------------------------------------------
# Batch (multi-segment) helpers
# ---------------------------------------------------------------------------

def _resolve_batch_dns(args):
    """Return the DNS server IP to use for batch segments."""
    if getattr(args, "dns", None):
        return args.dns
    dns_list = getattr(args, "_dns_servers", None)
    if dns_list:
        return dns_list[0]
    return DEFAULT_DNS


def batch_create(client, args):
    overlay = args.nsxcidroverlay
    prefix = args.segmentscidr
    subnets = compute_subnets(overlay, prefix)
    snat_mode = args.snat_mode
    dns = _resolve_batch_dns(args)

    print(f"Overlay {overlay} / {prefix} => {len(subnets)} segments")
    print(f"SNAT mode: {snat_mode}")
    print(f"DNS: {dns}")
    print()

    # 1. Shared DHCP server config
    print("=== Creating DHCP server config ===")
    dhcp_create(client, args)
    print()

    # 2. Create each segment
    for sub in subnets:
        print(f"=== Creating segment {sub['index']}/{len(subnets)}: "
              f"{sub['segment_id']}  ({sub['network']}) ===")
        args.segment_id = sub["segment_id"]
        args.segment_name = sub["segment_name"]
        args.network = sub["network"]
        args.gateway = sub["gateway"]
        args.dhcp_server_addr = sub["dhcp_server_addr"]
        args.dhcp_range = sub["dhcp_range"]
        args.dns = dns
        segment_create(client, args)
        print()

    # 3. NAT rules
    if snat_mode == "none":
        print("Skipping NAT rules (--snat-mode none)")
    elif snat_mode == "full-stack":
        print("=== Creating NO_SNAT rule (east-west bypass) ===")
        args.nosnat_id = DEFAULT_NOSNAT_ID
        args.nosnat_name = DEFAULT_NOSNAT_NAME
        args.nosnat_source = args.nosnat_source or overlay
        nosnat_create(client, args)
        print()

        print("=== Creating SNAT rule ===")
        args.snat_id = DEFAULT_SNAT_ID
        args.snat_name = DEFAULT_SNAT_NAME
        args.source = overlay
        args.translated = args.snat_translated
        snat_create(client, args)
    else:
        for sub in subnets:
            idx = sub["index"]
            print(f"=== Creating NO_SNAT rule for segment {idx} ===")
            args.nosnat_id = f"no-snat-segment-{idx}"
            args.nosnat_name = f"NO-SNAT-segment-{idx}"
            args.nosnat_source = sub["network"]
            nosnat_create(client, args)
            print()

            print(f"=== Creating SNAT rule for segment {idx} ===")
            args.snat_id = f"snat-segment-{idx}"
            args.snat_name = f"SNAT-segment-{idx}"
            args.source = sub["network"]
            args.translated = args.snat_translated
            snat_create(client, args)
            print()

    # 4. BGP route aggregation
    if getattr(args, "bgp_aggregation", True):
        print()
        print("=== Adding BGP route aggregation ===")
        bgp_aggregation_add(client, args, overlay, summary_only=False)

    print()
    print(f"Done — created {len(subnets)} segments.")


def batch_show(client, args):
    print("=== DHCP server config ===")
    args.dhcp_id = args.dhcp_id or ""
    dhcp_show(client, args)
    print()

    print("=== Segments ===")
    args.segment_id = ""
    segment_show(client, args)
    print()

    print("=== NAT rules ===")
    args.snat_id = ""
    snat_show(client, args)
    print()
    args.nosnat_id = ""
    nosnat_show(client, args)
    print()

    print("=== BGP route aggregation ===")
    if getattr(args, "tier0_id", None):
        bgp_aggregation_show(client, args)
    else:
        print("  (skipped — no --tier0-id or --edge-spec with tier0.name)")


def batch_delete(client, args):
    overlay = args.nsxcidroverlay
    prefix = args.segmentscidr
    subnets = compute_subnets(overlay, prefix)
    snat_mode = args.snat_mode

    # 1. Delete BGP route aggregation first
    if getattr(args, "bgp_aggregation", True):
        print("=== Deleting BGP route aggregation ===")
        bgp_aggregation_delete(client, args, overlay)
        print()

    # 2. Delete NAT rules
    if snat_mode == "none":
        print("Skipping NAT rules (--snat-mode none)")
        print()
    elif snat_mode == "full-stack":
        print("=== Deleting SNAT rule ===")
        args.snat_id = DEFAULT_SNAT_ID
        snat_delete(client, args)
        print()
        print("=== Deleting NO_SNAT rule ===")
        args.nosnat_id = DEFAULT_NOSNAT_ID
        nosnat_delete(client, args)
        print()
    else:
        for sub in subnets:
            idx = sub["index"]
            print(f"=== Deleting SNAT rule for segment {idx} ===")
            args.snat_id = f"snat-segment-{idx}"
            snat_delete(client, args)
            print()
            print(f"=== Deleting NO_SNAT rule for segment {idx} ===")
            args.nosnat_id = f"no-snat-segment-{idx}"
            nosnat_delete(client, args)
            print()

    # 3. Delete segments
    for sub in subnets:
        print(f"=== Deleting segment {sub['index']}/{len(subnets)}: "
              f"{sub['segment_id']} ===")
        args.segment_id = sub["segment_id"]
        segment_delete(client, args)
        print()

    # 4. Delete DHCP
    print("=== Deleting DHCP server config ===")
    dhcp_delete(client, args)

    print()
    print(f"Done — deleted {len(subnets)} segments.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def add_connection_args(parser):
    g = parser.add_argument_group("connection")
    g.add_argument("--nsx-host", default=NSX_HOST, help="NSX Manager FQDN")
    g.add_argument("--nsx-user", default=NSX_USER)
    g.add_argument("--nsx-password", default=NSX_PASSWORD)
    g.add_argument(
        "--proxy", default=HTTPS_PROXY, help="HTTPS proxy (empty to disable)"
    )
    g.add_argument("--tier1-id", default=TIER1_ID, help="Tier-1 gateway ID")
    g.add_argument(
        "--transport-zone", default=TRANSPORT_ZONE,
        help="Transport zone path",
    )
    g.add_argument(
        "--edge-cluster", default=EDGE_CLUSTER,
        help="Edge cluster path",
    )

    c = parser.add_argument_group("config files")
    c.add_argument(
        "--config", default=None,
        help="Path to config.json (derives NSX host and env ID)",
    )
    c.add_argument(
        "--edge-spec", default=None,
        help="Path to edge_cluster_spec.json (derives tier1, edge cluster, "
             "transport zone)",
    )
    c.add_argument("--profile", default=None, help="AWS CLI profile")
    c.add_argument("--region", default=None, help="AWS region")


def add_dhcp_args(parser):
    parser.add_argument(
        "--dhcp-id", default=DEFAULT_DHCP_ID, help="DHCP config object ID"
    )
    parser.add_argument("--dhcp-name", default=DEFAULT_DHCP_NAME)
    parser.add_argument(
        "--dhcp-listen",
        default=DEFAULT_DHCP_LISTEN,
        help="DHCP server listen address (CIDR)",
    )
    parser.add_argument(
        "--lease-time", type=int, default=DEFAULT_LEASE_TIME, help="Seconds"
    )


def add_segment_args(parser, include_dhcp_ref=True):
    parser.add_argument(
        "--segment-id", default=DEFAULT_SEGMENT_ID, help="Segment object ID"
    )
    parser.add_argument("--segment-name", default=DEFAULT_SEGMENT_NAME)
    parser.add_argument(
        "--gateway", default=DEFAULT_GATEWAY, help="Gateway IP/prefix"
    )
    parser.add_argument(
        "--network", default=DEFAULT_NETWORK, help="Segment network CIDR"
    )
    parser.add_argument("--dhcp-range", default=DEFAULT_DHCP_RANGE)
    parser.add_argument(
        "--dhcp-server-addr",
        default=DEFAULT_DHCP_SERVER_ADDR,
        help="In-segment DHCP server IP/prefix",
    )
    parser.add_argument("--dns", default=DEFAULT_DNS, help="DNS server IP")
    if include_dhcp_ref:
        parser.add_argument("--dhcp-id", default=DEFAULT_DHCP_ID)
        parser.add_argument(
            "--lease-time", type=int, default=DEFAULT_LEASE_TIME, help="Seconds"
        )


def add_snat_args(parser):
    parser.add_argument("--snat-id", default=DEFAULT_SNAT_ID)
    parser.add_argument("--snat-name", default=DEFAULT_SNAT_NAME)
    parser.add_argument(
        "--source", default=DEFAULT_SNAT_SOURCE, help="Source network CIDR"
    )
    parser.add_argument(
        "--translated",
        default=DEFAULT_SNAT_TRANSLATED,
        help="Translated (SNAT) IP",
    )


def add_nosnat_args(parser):
    parser.add_argument("--nosnat-id", default=DEFAULT_NOSNAT_ID)
    parser.add_argument("--nosnat-name", default=DEFAULT_NOSNAT_NAME)
    parser.add_argument(
        "--nosnat-source", default=DEFAULT_NOSNAT_SOURCE,
        help="NO_SNAT source network CIDR",
    )
    parser.add_argument(
        "--nosnat-dest", default=DEFAULT_NOSNAT_DEST,
        help="NO_SNAT destination network CIDR",
    )


def add_batch_args(parser):
    b = parser.add_argument_group("batch overlay")
    b.add_argument(
        "--nsxcidroverlay", default="10.28.40.0/21",
        help="Overlay supernet to subdivide (default: 10.28.40.0/21)",
    )
    b.add_argument(
        "--segmentscidr", type=int, default=25,
        help="Prefix length for each segment subnet (default: 25)",
    )
    b.add_argument(
        "--snat-mode", choices=["full-stack", "per-segment", "none"],
        default="full-stack",
        help="full-stack: 1 SNAT/NO_SNAT for whole overlay; "
             "per-segment: individual rules per segment; "
             "none: skip NAT rules entirely (default: full-stack)",
    )
    b.add_argument(
        "--snat-translated", default=DEFAULT_SNAT_TRANSLATED,
        help="Translated (outbound NAT) IP",
    )
    b.add_argument("--dns", default=None, help="DNS server IP (default: from edge spec)")
    b.add_argument(
        "--nosnat-source", default=None,
        help="NO_SNAT source CIDR (default: overlay CIDR)",
    )
    b.add_argument(
        "--nosnat-dest", default=DEFAULT_NOSNAT_DEST,
        help="NO_SNAT destination CIDR",
    )

    bgp = parser.add_argument_group("BGP route aggregation")
    bgp.add_argument(
        "--tier0-id", dest="tier0_id", default=None,
        help="Tier-0 gateway ID (default: from --edge-spec tier0.name)",
    )
    bgp.add_argument(
        "--bgp-aggregation", dest="bgp_aggregation",
        action=argparse.BooleanOptionalAction, default=True,
        help="Add/remove BGP route aggregation for the overlay CIDR "
             "(default: enabled; use --no-bgp-aggregation to skip)",
    )


def build_parser():
    top = argparse.ArgumentParser(
        description="NSX-T network management for AWS EVS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s dhcp create
  %(prog)s segment create --gateway 10.28.44.1/24 --network 10.28.44.0/24
  %(prog)s segment show
  %(prog)s snat create --source 10.28.44.0/24 --translated 10.28.45.1
  %(prog)s all create
  %(prog)s all delete

  # batch: create 16 /25 segments from a /21 overlay
  %(prog)s batch create --nsxcidroverlay 10.28.40.0/21 --segmentscidr 25
  %(prog)s batch create --snat-mode per-segment
  %(prog)s batch show
  %(prog)s batch delete
""",
    )
    sub = top.add_subparsers(dest="resource", required=True)

    # ---- dhcp ----
    dhcp = sub.add_parser("dhcp", help="Manage DHCP server config")
    dhcp_sub = dhcp.add_subparsers(dest="action", required=True)

    p = dhcp_sub.add_parser("create", help="Create DHCP server config")
    add_connection_args(p)
    add_dhcp_args(p)
    p.set_defaults(func=dhcp_create)

    p = dhcp_sub.add_parser("show", help="Show DHCP config (omit --dhcp-id to list all)")
    add_connection_args(p)
    p.add_argument("--dhcp-id", default="", help="Specific ID or omit for all")
    p.set_defaults(func=dhcp_show)

    p = dhcp_sub.add_parser("delete", help="Delete DHCP server config")
    add_connection_args(p)
    p.add_argument("--dhcp-id", default=DEFAULT_DHCP_ID)
    p.set_defaults(func=dhcp_delete)

    # ---- segment ----
    seg = sub.add_parser("segment", help="Manage NSX overlay segments")
    seg_sub = seg.add_subparsers(dest="action", required=True)

    p = seg_sub.add_parser("create", help="Create a routed overlay segment")
    add_connection_args(p)
    add_segment_args(p)
    p.set_defaults(func=segment_create)

    p = seg_sub.add_parser("show", help="Show segment (omit --segment-id to list all)")
    add_connection_args(p)
    p.add_argument("--segment-id", default="", help="Specific ID or omit for all")
    p.set_defaults(func=segment_show)

    p = seg_sub.add_parser("delete", help="Delete a segment")
    add_connection_args(p)
    p.add_argument("--segment-id", default=DEFAULT_SEGMENT_ID)
    p.set_defaults(func=segment_delete)

    # ---- snat ----
    snat = sub.add_parser("snat", help="Manage SNAT rules on Tier-1")
    snat_sub = snat.add_subparsers(dest="action", required=True)

    p = snat_sub.add_parser("create", help="Create SNAT rule")
    add_connection_args(p)
    add_snat_args(p)
    p.set_defaults(func=snat_create)

    p = snat_sub.add_parser("show", help="Show SNAT rules (omit --snat-id to list all)")
    add_connection_args(p)
    p.add_argument("--snat-id", default="", help="Specific ID or omit for all")
    p.set_defaults(func=snat_show)

    p = snat_sub.add_parser("delete", help="Delete SNAT rule")
    add_connection_args(p)
    p.add_argument("--snat-id", default=DEFAULT_SNAT_ID)
    p.set_defaults(func=snat_delete)

    # ---- nosnat ----
    nosnat = sub.add_parser("nosnat", help="Manage NO_SNAT east-west bypass rules")
    nosnat_sub = nosnat.add_subparsers(dest="action", required=True)

    p = nosnat_sub.add_parser("create", help="Create NO_SNAT rule")
    add_connection_args(p)
    add_nosnat_args(p)
    p.set_defaults(func=nosnat_create)

    p = nosnat_sub.add_parser("show", help="Show NO_SNAT rules (omit --nosnat-id to list all)")
    add_connection_args(p)
    p.add_argument("--nosnat-id", default="", help="Specific ID or omit for all")
    p.set_defaults(func=nosnat_show)

    p = nosnat_sub.add_parser("delete", help="Delete NO_SNAT rule")
    add_connection_args(p)
    p.add_argument("--nosnat-id", default=DEFAULT_NOSNAT_ID)
    p.set_defaults(func=nosnat_delete)

    # ---- all ----
    a = sub.add_parser("all", help="Create/show/delete DHCP + segment + SNAT together")
    all_sub = a.add_subparsers(dest="action", required=True)

    p = all_sub.add_parser("create", help="Create DHCP, segment, NO_SNAT, and SNAT")
    add_connection_args(p)
    add_dhcp_args(p)
    add_segment_args(p, include_dhcp_ref=False)
    add_nosnat_args(p)
    add_snat_args(p)
    p.set_defaults(func=all_create)

    p = all_sub.add_parser("show", help="Show all resources")
    add_connection_args(p)
    p.add_argument("--dhcp-id", default=DEFAULT_DHCP_ID)
    p.add_argument("--segment-id", default=DEFAULT_SEGMENT_ID)
    p.add_argument("--nosnat-id", default=DEFAULT_NOSNAT_ID)
    p.add_argument("--snat-id", default=DEFAULT_SNAT_ID)
    p.set_defaults(func=all_show)

    p = all_sub.add_parser("delete", help="Delete SNAT, NO_SNAT, segment, and DHCP (in order)")
    add_connection_args(p)
    p.add_argument("--dhcp-id", default=DEFAULT_DHCP_ID)
    p.add_argument("--segment-id", default=DEFAULT_SEGMENT_ID)
    p.add_argument("--nosnat-id", default=DEFAULT_NOSNAT_ID)
    p.add_argument("--snat-id", default=DEFAULT_SNAT_ID)
    p.set_defaults(func=all_delete)

    # ---- batch ----
    batch = sub.add_parser(
        "batch",
        help="Create/show/delete multiple segments from an overlay CIDR",
    )
    batch_sub = batch.add_subparsers(dest="action", required=True)

    p = batch_sub.add_parser(
        "create",
        help="Subdivide overlay into segments and create DHCP + segments + NAT",
    )
    add_connection_args(p)
    add_batch_args(p)
    add_dhcp_args(p)
    p.set_defaults(func=batch_create)

    p = batch_sub.add_parser("show", help="Show all batch-created resources")
    add_connection_args(p)
    add_batch_args(p)
    p.add_argument("--dhcp-id", default="", help="Specific DHCP ID or omit for all")
    p.set_defaults(func=batch_show)

    p = batch_sub.add_parser(
        "delete",
        help="Delete all segments, NAT rules, and DHCP from a batch",
    )
    add_connection_args(p)
    add_batch_args(p)
    p.add_argument("--dhcp-id", default=DEFAULT_DHCP_ID)
    p.set_defaults(func=batch_delete)

    return top


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = build_parser()
    args = parser.parse_args()

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
