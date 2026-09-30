#!/usr/bin/env python3
"""
Deploy test VMs from an OVA to NSX segments matching a wildcard pattern.

Queries the NSX Manager for segments whose display_name contains the
``--segmentname`` search string, then deploys one VM per matching segment
using VMware ovftool.  Each VM is named ``<prefix>-<segment-display-name>``
(spaces replaced with dashes).

Connection defaults are resolved in order: CLI flags > environment variables >
config files (--config / --edge-spec) > AWS Secrets Manager.

Requirements:
  pip install boto3 pyvmomi requests
  ovftool must be in PATH (VMware OVF Tool)

Usage:
  # List matching segments (no deployment):
  python deploy_network_testvm.py \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \\
      --profile ci \\
      --segmentname "logical network segment" \\
      --show-segments

  # Dry run — show planned VMs without deploying:
  python deploy_network_testvm.py \\
      --ova /path/to/test.ova \\
      --segmentname "logical network segment" \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \\
      --profile ci --dry-run

  # Deploy test VMs to all matching segments:
  python deploy_network_testvm.py \\
      --ova /path/to/test.ova \\
      --segmentname "logical network segment" \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \\
      --profile ci

  # Inspect OVF properties without deploying:
  python deploy_network_testvm.py \\
      --ova /path/to/test.ova --show-properties
"""

import argparse
import json
import logging
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import urllib.parse

import boto3
import requests
import urllib3

from pyVim import connect
from pyVmomi import vim

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LOG = logging.getLogger("deploy_network_testvm")

OVF_NS = "http://schemas.dmtf.org/ovf/envelope/1"

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
    """Retrieve a password from Secrets Manager.

    Handles both JSON ``{"password": "..."}`` and raw-string formats.
    """
    LOG.info("Fetching secret %s", secret_id)
    response = sm.get_secret_value(SecretId=secret_id)
    raw = response.get("SecretString", "")
    try:
        data = json.loads(raw)
        return data.get("password", raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def connect_to_esxi(host_ip, password, port=443):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return connect.SmartConnect(
        host=host_ip, user="root", pwd=password,
        port=port, sslContext=ctx,
    )


def find_vsan_datastore(si):
    """Find the first vSAN datastore on the connected ESXi host."""
    content = si.RetrieveContent()
    host = (
        content.rootFolder
        .childEntity[0]
        .hostFolder
        .childEntity[0]
        .host[0]
    )
    for ds in host.datastore:
        if getattr(ds.summary, "type", "").lower() == "vsan":
            return ds.name
    available = [f"{ds.name} ({ds.summary.type})" for ds in host.datastore]
    raise RuntimeError(
        f"No vSAN datastore found on host. Available: {available}"
    )


def vm_exists(si, vm_name):
    """Check whether a VM with the given name already exists."""
    content = si.RetrieveContent()
    vm_folder = content.rootFolder.childEntity[0].vmFolder
    for child in vm_folder.childEntity:
        if hasattr(child, "name") and child.name == vm_name:
            return True
    return False


# ---------------------------------------------------------------------------
# OVA / OVF helpers
# ---------------------------------------------------------------------------

import xml.etree.ElementTree as ET


def read_ovf_from_ova(ova_path):
    """Extract the OVF descriptor string from an OVA (tar) file."""
    with tarfile.open(ova_path, "r") as tar:
        for member in tar.getmembers():
            if member.name.endswith(".ovf"):
                f = tar.extractfile(member)
                return f.read().decode("utf-8")
    raise RuntimeError(f"No .ovf file found inside {ova_path}")


def parse_ovf_properties(ovf_xml):
    """Return a list of (ovftool_key, ovf_key, class, instance, type, label, desc, default)."""
    root = ET.fromstring(ovf_xml)
    result = []

    for ps in root.iter(f"{{{OVF_NS}}}ProductSection"):
        class_id = ps.get(f"{{{OVF_NS}}}class", "")
        instance_id = ps.get(f"{{{OVF_NS}}}instance", "")

        for prop in ps.iter(f"{{{OVF_NS}}}Property"):
            key = prop.get(f"{{{OVF_NS}}}key", "")
            ptype = prop.get(f"{{{OVF_NS}}}type", "")
            default = prop.get(f"{{{OVF_NS}}}value", "")
            label = ""
            desc = ""
            for child in prop:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if tag == "Label":
                    label = (child.text or "").strip()
                elif tag == "Description":
                    desc = (child.text or "").strip()

            if class_id and instance_id:
                ovftool_key = f"{class_id}.{key}.{instance_id}"
            elif class_id:
                ovftool_key = f"{class_id}.{key}"
            else:
                ovftool_key = key

            result.append((ovftool_key, key, class_id, instance_id,
                           ptype, label, desc, default))

    return result


def parse_ovf_networks(ovf_xml):
    """Return a list of network names defined in the OVF."""
    root = ET.fromstring(ovf_xml)
    names = []
    for net in root.iter(f"{{{OVF_NS}}}Network"):
        name = net.get(f"{{{OVF_NS}}}name", "")
        if name:
            names.append(name)
    return names


# ---------------------------------------------------------------------------
# ovftool deployment
# ---------------------------------------------------------------------------

def deploy_with_ovftool(ova_path, vm_name, host_ip, esxi_password,
                        datastore_name, network_name, ovf_networks,
                        extra_props=None, power_on=True):
    """Deploy an OVA to an ESXi host via ovftool.

    All OVF-defined networks are mapped to *network_name*.
    Optional *extra_props* is a list of "KEY=VALUE" strings passed as
    ``--prop:KEY=VALUE``.
    """
    if shutil.which("ovftool") is None:
        raise RuntimeError(
            "ovftool not found in PATH. Install VMware OVF Tool from "
            "https://developer.vmware.com/web/tool/ovf-tool/"
        )

    net_args = []
    for ovf_net_name in ovf_networks:
        net_args.append(f"--net:{ovf_net_name}={network_name}")
        LOG.info("Network mapping: OVF '%s' -> '%s'", ovf_net_name, network_name)

    prop_args = []
    if extra_props:
        for kv in extra_props:
            prop_args.append(f"--prop:{kv}")

    encoded_pw = urllib.parse.quote(esxi_password, safe="")
    target_uri = f"vi://root:{encoded_pw}@{host_ip}/"

    cmd = [
        "ovftool",
        "--acceptAllEulas",
        "--noSSLVerify",
        "--X:injectOvfEnv",
        f"--datastore={datastore_name}",
        f"--name={vm_name}",
        "--diskMode=thin",
    ]

    if power_on:
        cmd.append("--powerOn")

    cmd.extend(net_args)
    cmd.extend(prop_args)
    cmd.append(ova_path)
    cmd.append(target_uri)

    safe_cmd = []
    for c in cmd:
        if encoded_pw in c:
            safe_cmd.append(f"vi://root:****@{host_ip}/")
        else:
            safe_cmd.append(c)
    LOG.info("Running ovftool:\n  %s", " \\\n    ".join(safe_cmd))

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in process.stdout:
        line = line.rstrip()
        if line:
            LOG.info("ovftool: %s", line)
    process.wait()

    if process.returncode != 0:
        raise RuntimeError(
            f"ovftool failed with exit code {process.returncode}"
        )

    LOG.info("VM '%s' deployed successfully", vm_name)


# ---------------------------------------------------------------------------
# NSX segment query
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Deploy test VMs from an OVA to NSX segments matching a wildcard pattern",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List matching segments:
  python deploy_network_testvm.py \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \\
      --profile ci --segmentname "logical network segment" --show-segments

  # Dry run:
  python deploy_network_testvm.py \\
      --ova /path/to/test.ova --segmentname "logical network segment" \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \\
      --profile ci --dry-run

  # Full deployment:
  python deploy_network_testvm.py \\
      --ova /path/to/test.ova --segmentname "logical network segment" \\
      --config ../Phase_2_evs_env/python/config.json \\
      --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \\
      --profile ci
""",
    )

    parser.add_argument(
        "--ova", help="Path to the OVA file",
    )
    parser.add_argument(
        "--segmentname",
        help="Wildcard search pattern for NSX segment display_names "
             "(case-insensitive substring match)",
    )

    cfg = parser.add_argument_group("config files")
    cfg.add_argument("--config", help="Path to config.json")
    cfg.add_argument("--edge-spec", dest="edge_spec", help="Path to edge_cluster_spec.json")
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

    deploy = parser.add_argument_group("deployment")
    deploy.add_argument(
        "--esxi-host", dest="esxi_host",
        help="ESXi host IP or FQDN (default: derived from config vcfHostnames.esxi01)",
    )
    deploy.add_argument(
        "--datastore-name", dest="datastore_name",
        help="Target datastore (default: auto-detect first vSAN datastore)",
    )
    deploy.add_argument(
        "--vm-prefix", dest="vm_prefix", default="testnetwork",
        help="VM name prefix (default: 'testnetwork')",
    )
    deploy.add_argument(
        "--no-power-on", dest="no_power_on", action="store_true",
        help="Do not power on VMs after deployment",
    )
    deploy.add_argument(
        "--prop", action="append", metavar="KEY=VALUE",
        help="Extra OVF property (repeatable, e.g. --prop guestinfo.hostname=test)",
    )

    modes = parser.add_argument_group("modes")
    modes.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="Show matching segments and planned VMs without deploying",
    )
    modes.add_argument(
        "--show-segments", dest="show_segments", action="store_true",
        help="Query and list matching segments then exit",
    )
    modes.add_argument(
        "--show-properties", dest="show_properties", action="store_true",
        help="List OVF properties from the OVA and exit (no AWS/NSX needed)",
    )
    modes.add_argument(
        "--ovftool-probe", dest="ovftool_probe", action="store_true",
        help="Run 'ovftool <ova>' to show its view of the OVA and exit",
    )

    disp = parser.add_argument_group("display")
    disp.add_argument(
        "--showpassword", action="store_true",
        help="Display credentials on screen",
    )
    disp.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging",
    )

    return parser


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


def resolve_esxi_connection(args, config, edge_spec, sm):
    """Fill in ESXi host and password from config files / Secrets Manager."""
    env_id = None
    if config:
        env_id = config.get("environmentId")
    elif edge_spec:
        env_id = edge_spec.get("environmentId")

    if not args.esxi_host and config:
        esxi_short = config.get("vcfHostnames", {}).get("esxi01", "esxi01")
        fqdn = config.get("fqdn", "")
        args.esxi_host = f"{esxi_short}.{fqdn}"
        LOG.info("ESXi host (from config): %s", args.esxi_host)

    if not args.esxi_host:
        LOG.error("ESXi host not set. Provide --esxi-host or --config.")
        sys.exit(1)

    esxi_hostname = args.esxi_host.split(".")[0]
    if env_id and sm:
        esxi_secret = f"evs!{env_id}_{esxi_hostname}"
        args._esxi_password = get_secret_password(sm, esxi_secret)
        LOG.info("ESXi password retrieved from Secrets Manager")
    else:
        LOG.error("Cannot retrieve ESXi password: need --config with "
                  "environmentId for Secrets Manager lookup.")
        sys.exit(1)


def main():
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    # --- OVA-only modes (no AWS/NSX needed) ---
    if args.ovftool_probe:
        if not args.ova:
            LOG.error("--ova is required with --ovftool-probe")
            sys.exit(1)
        if not os.path.isfile(args.ova):
            LOG.error("OVA file not found: %s", args.ova)
            sys.exit(1)
        if shutil.which("ovftool") is None:
            LOG.error("ovftool not found in PATH")
            sys.exit(1)
        subprocess.run(["ovftool", args.ova])
        sys.exit(0)

    if args.show_properties:
        if not args.ova:
            LOG.error("--ova is required with --show-properties")
            sys.exit(1)
        if not os.path.isfile(args.ova):
            LOG.error("OVA file not found: %s", args.ova)
            sys.exit(1)
        ovf_xml = read_ovf_from_ova(args.ova)
        props = parse_ovf_properties(ovf_xml)
        networks = parse_ovf_networks(ovf_xml)
        print(f"\nOVF Networks:")
        for n in networks:
            print(f"  - {n}")
        print(f"\nOVF Properties ({len(props)}):")
        print(f"  {'OVF Key':<30} {'ovftool Key':<50} {'Default':<20} Label")
        print(f"  {'-'*30} {'-'*50} {'-'*20} {'-'*30}")
        for ovftool_key, ovf_key, cls, inst, ptype, label, desc, default in props:
            print(f"  {ovf_key:<30} {ovftool_key:<50} {default:<20} {label}")
            if desc:
                print(f"  {'':30} {'':50} {'':20} -> {desc}")
        sys.exit(0)

    # --- Validate required args for segment query ---
    if not args.segmentname and not args.show_properties:
        LOG.error("--segmentname is required")
        sys.exit(1)

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

    print(f"\nFound {len(segments)} matching segment(s):")
    for seg in segments:
        subnets = [s.get("network", "") for s in seg.get("subnets", [])]
        net_str = ", ".join(subnets) if subnets else "no subnets"
        print(f"  {seg['id']:<40} {seg['display_name']:<45} {net_str}")

    if args.show_segments:
        sys.exit(0)

    # --- Validate OVA for deployment modes ---
    if not args.ova:
        LOG.error("--ova is required for deployment (or use --show-segments)")
        sys.exit(1)
    if not os.path.isfile(args.ova):
        LOG.error("OVA file not found: %s", args.ova)
        sys.exit(1)

    # Parse OVF networks from the OVA
    ovf_xml = read_ovf_from_ova(args.ova)
    ovf_networks = parse_ovf_networks(ovf_xml)
    LOG.info("OVF networks in OVA: %s", ovf_networks)

    # Build planned VM list
    planned = []
    for seg in segments:
        sanitized = seg["display_name"].replace(" ", "-")
        vm_name = f"{args.vm_prefix}-{sanitized}"
        planned.append({
            "vm_name": vm_name,
            "segment_id": seg["id"],
            "segment_display_name": seg["display_name"],
        })

    print(f"\nPlanned VMs ({len(planned)}):")
    for p in planned:
        print(f"  {p['vm_name']:<55} -> {p['segment_display_name']}")

    if args.dry_run:
        print("\n[DRY RUN] No VMs deployed.")
        sys.exit(0)

    # --- Resolve ESXi connection ---
    resolve_esxi_connection(args, config, edge_spec, sm)

    if args.showpassword:
        print(f"\n  ESXi Host    : {args.esxi_host}")
        print(f"  ESXi User    : root")
        print(f"  ESXi Pass    : {args._esxi_password}")
        print(f"  NSX Host     : {args.nsx_host}")
        print(f"  NSX User     : {args.nsx_user}")
        print(f"  NSX Pass     : {args.nsx_password}\n")

    # --- Connect to ESXi for VM-exists checks ---
    LOG.info("Connecting to ESXi host %s for pre-check...", args.esxi_host)
    si = connect_to_esxi(args.esxi_host, args._esxi_password)

    try:
        # --- Auto-detect vSAN datastore if not specified ---
        if not args.datastore_name:
            args.datastore_name = find_vsan_datastore(si)
            LOG.info("Auto-detected vSAN datastore: %s", args.datastore_name)

        deployed = 0
        skipped = 0

        for p in planned:
            vm_name = p["vm_name"]
            network_name = p["segment_display_name"]

            if vm_exists(si, vm_name):
                LOG.warning("VM '%s' already exists — skipping", vm_name)
                print(f"  SKIP  {vm_name} (already exists)")
                skipped += 1
                continue

            print(f"\n--- Deploying {vm_name} -> segment '{network_name}' ---")
            deploy_with_ovftool(
                ova_path=args.ova,
                vm_name=vm_name,
                host_ip=args.esxi_host,
                esxi_password=args._esxi_password,
                datastore_name=args.datastore_name,
                network_name=network_name,
                ovf_networks=ovf_networks,
                extra_props=args.prop,
                power_on=not args.no_power_on,
            )
            deployed += 1
    finally:
        connect.Disconnect(si)

    print(f"\nDeployment complete: {deployed} deployed, {skipped} skipped")
    print(f"  Datastore : {args.datastore_name}")
    print(f"  ESXi Host : {args.esxi_host}")
    print(f"  Power On  : {not args.no_power_on}")


if __name__ == "__main__":
    main()
