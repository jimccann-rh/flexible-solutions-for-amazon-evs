#!/usr/bin/env python3
"""
Set an ESXi advanced option on all hosts in a cluster via the vSphere API.

Connects to vCenter, finds all ESXi hosts in the cluster, and uses the
AdvancedOption manager to set the specified option on each host. No SSH
required.

Default operation: set VSAN.FakeSCSIReservations to 1 on all hosts.

Requirements:
  pip install boto3 pyvmomi

Usage:
  # Set VSAN.FakeSCSIReservations=1 on all hosts (default):
  python set_esxi_advanced_option.py \\
      --config ../Phase_2_evs_env/python/config.json \\
      --profile ci

  # Dry run — show hosts without applying:
  python set_esxi_advanced_option.py \\
      --config ../Phase_2_evs_env/python/config.json \\
      --profile ci --dry-run

  # Set a different option:
  python set_esxi_advanced_option.py \\
      --config ../Phase_2_evs_env/python/config.json \\
      --profile ci \\
      --option Net.TcpipHeapSize --value 120

  # Override cluster name:
  python set_esxi_advanced_option.py \\
      --config ../Phase_2_evs_env/python/config.json \\
      --profile ci --cluster my-cluster
"""

import argparse
import json
import logging
import ssl
import sys

import boto3
from pyVim import connect
from pyVmomi import vim

LOG = logging.getLogger("set_esxi_advanced_option")


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


def find_cluster(si, cluster_name):
    content = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.ClusterComputeResource], True,
    )
    try:
        for cluster in container.view:
            if cluster.name == cluster_name:
                return cluster
    finally:
        container.Destroy()

    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.ClusterComputeResource], True,
    )
    try:
        available = [c.name for c in container.view]
    finally:
        container.Destroy()
    raise RuntimeError(
        f"Cluster '{cluster_name}' not found. Available: {available}"
    )


def get_advanced_option(host, key):
    """Read the current value of an advanced option on a host."""
    try:
        results = host.configManager.advancedOption.QueryOptions(key)
        if results:
            return results[0].value
    except vim.fault.InvalidName:
        pass
    return None


def set_advanced_option(host, key, value):
    """Set an advanced option on a host via the AdvancedOption manager."""
    option = vim.option.OptionValue()
    option.key = key
    option.value = value
    host.configManager.advancedOption.UpdateOptions(changedValue=[option])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Set an ESXi advanced option on all hosts in a cluster via the vSphere API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Default: set VSAN.FakeSCSIReservations=1 on all hosts
  python set_esxi_advanced_option.py \\
      --config ../Phase_2_evs_env/python/config.json --profile ci

  # Dry run:
  python set_esxi_advanced_option.py \\
      --config ../Phase_2_evs_env/python/config.json --profile ci --dry-run

  # Custom option:
  python set_esxi_advanced_option.py \\
      --config ../Phase_2_evs_env/python/config.json --profile ci \\
      --option Net.TcpipHeapSize --value 120
""",
    )

    parser.add_argument(
        "--option", default="VSAN.FakeSCSIReservations",
        help="Advanced option key (default: VSAN.FakeSCSIReservations)",
    )
    parser.add_argument(
        "--value", default=1, type=int,
        help="Integer value to set (default: 1)",
    )

    cfg = parser.add_argument_group("config")
    cfg.add_argument("--config", required=True, help="Path to config.json")
    cfg.add_argument("--profile", help="AWS CLI profile name")
    cfg.add_argument("--region", help="AWS region override")

    vc = parser.add_argument_group("vCenter connection")
    vc.add_argument("--vcenter-host", dest="vcenter_host",
                    help="vCenter hostname (default: from config)")
    vc.add_argument("--vcenter-user", dest="vcenter_user",
                    default="administrator@vsphere.local",
                    help="vCenter username (default: administrator@vsphere.local)")
    vc.add_argument("--cluster",
                    help="Cluster name (default: {environmentId}-cl01)")

    modes = parser.add_argument_group("modes")
    modes.add_argument("--dry-run", dest="dry_run", action="store_true",
                       help="Show current values without changing anything")
    modes.add_argument("--showpassword", action="store_true",
                       help="Display credentials on screen")
    modes.add_argument("--verbose", "-v", action="store_true",
                       help="Enable debug logging")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    with open(args.config) as f:
        config = json.load(f)

    region = args.region or config.get("region", "us-east-1")
    environment_id = config["environmentId"]
    fqdn = config["fqdn"]

    # --- vCenter host ---
    if args.vcenter_host:
        vcenter_host = args.vcenter_host
    else:
        vc_short = config.get("vcfHostnames", {}).get("vcenter", "vc")
        vcenter_host = f"{vc_short}.{fqdn}"

    # --- Cluster name ---
    cluster_name = args.cluster or f"{environment_id}-cl01"

    # --- vCenter password ---
    session_kwargs = {"region_name": region}
    if args.profile:
        session_kwargs["profile_name"] = args.profile
    session = boto3.Session(**session_kwargs)
    sm = session.client("secretsmanager")

    secret_id = f"evs-{environment_id}_vcenterSso"
    vcenter_password = get_secret_password(sm, secret_id)

    if args.showpassword:
        print(f"\n  vCenter Host : {vcenter_host}")
        print(f"  vCenter User : {args.vcenter_user}")
        print(f"  vCenter Pass : {vcenter_password}\n")

    # --- Connect to vCenter ---
    LOG.info("Connecting to vCenter: %s", vcenter_host)
    si = connect_to_vcenter(vcenter_host, args.vcenter_user, vcenter_password)

    try:
        cluster = find_cluster(si, cluster_name)
        hosts = cluster.host

        if not hosts:
            LOG.error("No hosts found in cluster '%s'", cluster_name)
            sys.exit(1)

        print(f"\nCluster: {cluster_name}")
        print(f"Option:  {args.option}")
        print(f"Target:  {args.value}")
        print(f"Hosts:   {len(hosts)}\n")

        for host in hosts:
            host_name = host.name
            current = get_advanced_option(host, args.option)

            if args.dry_run:
                print(f"  {host_name:<30} current={current}")
                continue

            if current == args.value:
                print(f"  {host_name:<30} already {args.value} — skipped")
                continue

            LOG.info("Setting %s=%s on %s (was %s)",
                     args.option, args.value, host_name, current)
            set_advanced_option(host, args.option, args.value)
            new_val = get_advanced_option(host, args.option)
            print(f"  {host_name:<30} {current} -> {new_val}")

    finally:
        connect.Disconnect(si)

    if args.dry_run:
        print("\n[DRY RUN] No changes made.")
    else:
        print("\nDone.")


if __name__ == "__main__":
    main()
