#!/usr/bin/env python3
"""
Commission expansion ESXi hosts via the VCF 9.1 recommended workflow.

Uses vCenter SSO credentials (administrator@vsphere.local) to authenticate
to the SDDC Manager API — the same path the vCenter VCF plugin takes when
you commission hosts through Global Inventory Lists > Hosts > Unassigned
Hosts > Commission Hosts.

After commissioning, verifies the hosts are visible in the Management
vCenter inventory.

Requirements:
  pip install boto3 requests

Usage:
  python3 commission_hosts_vcf9.py \
      --config expand_hosts_config.json \
      --profile ci

  # Dry run:
  python3 commission_hosts_vcf9.py \
      --config expand_hosts_config.json \
      --profile ci --dry-run
"""

import argparse
import hashlib
import ipaddress
import json
import logging
import os
import socket
import ssl
import sys
import time

import boto3
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LOG = logging.getLogger("commission_hosts_vcf9")

HTTPS_PROXY = os.environ.get("HTTPS_PROXY", "http://127.0.0.1:9999")
DEFAULT_STORAGE_TYPE = "VSAN_ESA"
POLL_INTERVAL = 30
POLL_TIMEOUT = 1800


# ---------------------------------------------------------------------------
# vCenter REST client (vSphere Automation API)
# ---------------------------------------------------------------------------

class VCenterClient:
    def __init__(self, host, proxy=None):
        self.base = f"https://{host}"
        self.session = requests.Session()
        self.session.verify = False
        if proxy:
            self.session.proxies = {"https": proxy, "http": proxy}

    def authenticate(self, username, password):
        resp = self.session.post(
            f"{self.base}/api/session",
            auth=(username, password),
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code >= 400:
            print(f"ERROR: vCenter authentication failed ({resp.status_code})",
                  file=sys.stderr)
            sys.exit(1)

        token = resp.text.strip().strip('"')
        self.session.headers["vmware-api-session-id"] = token
        LOG.info("Authenticated to vCenter %s", self.base)

    def get(self, path):
        return self.session.get(f"{self.base}{path}")

    def get_hosts(self):
        resp = self.get("/api/vcenter/host")
        if resp.status_code >= 400:
            LOG.warning("Failed to list vCenter hosts: %s", resp.status_code)
            return []
        return resp.json()


# ---------------------------------------------------------------------------
# SDDC Manager REST client (VCF commissioning API)
# ---------------------------------------------------------------------------

class SDDCManagerClient:
    def __init__(self, host, proxy=None):
        self.base = f"https://{host}"
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers["Content-Type"] = "application/json"
        if proxy:
            self.session.proxies = {"https": proxy, "http": proxy}

    def authenticate(self, username, password):
        resp = self.session.post(
            f"{self.base}/v1/tokens",
            json={"username": username, "password": password},
        )
        if resp.status_code >= 400:
            print(f"ERROR: SDDC Manager authentication failed ({resp.status_code})",
                  file=sys.stderr)
            try:
                print(json.dumps(resp.json(), indent=2), file=sys.stderr)
            except Exception:
                print(resp.text, file=sys.stderr)
            sys.exit(1)

        token = resp.json().get("accessToken")
        if not token:
            print("ERROR: No accessToken in response", file=sys.stderr)
            sys.exit(1)

        self.session.headers["Authorization"] = f"Bearer {token}"
        LOG.info("Authenticated to SDDC Manager %s (SSO)", self.base)

    def get(self, path):
        return self.session.get(f"{self.base}{path}")

    def post(self, path, payload):
        return self.session.post(f"{self.base}{path}", json=payload)


def check_response(resp, action):
    if resp.status_code >= 400:
        print(f"ERROR {action}: {resp.status_code}", file=sys.stderr)
        try:
            print(json.dumps(resp.json(), indent=2), file=sys.stderr)
        except Exception:
            print(resp.text, file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def get_vcenter_sso_credentials(sm, env_id):
    """Get vCenter SSO credentials from Secrets Manager."""
    secret_id = f"evs-{env_id}_vcenterSso"
    LOG.info("Fetching vCenter SSO secret %s", secret_id)
    response = sm.get_secret_value(SecretId=secret_id)
    raw = response.get("SecretString", "")
    try:
        data = json.loads(raw)
        return data.get("username", "administrator@vsphere.local"), data["password"]
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        print(f"ERROR: Failed to parse vCenter SSO secret: {exc}",
              file=sys.stderr)
        sys.exit(1)


def get_esxi_password(sm, env_id, hostname):
    """Get ESXi root password from Secrets Manager (evs!{envId}_{hostname})."""
    secret_id = f"evs!{env_id}_{hostname}"
    LOG.info("Fetching ESXi secret %s", secret_id)
    response = sm.get_secret_value(SecretId=secret_id)
    raw = response.get("SecretString", "")
    try:
        data = json.loads(raw)
        return data.get("password", raw)
    except (json.JSONDecodeError, TypeError):
        return raw


# ---------------------------------------------------------------------------
# SSL thumbprint
# ---------------------------------------------------------------------------

def fetch_ssl_thumbprint(host, port=443, timeout=10):
    """Fetch the SHA-256 SSL thumbprint from an ESXi host."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)

    if not der:
        raise RuntimeError(f"Empty TLS certificate returned by {host}:{port}")

    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


# ---------------------------------------------------------------------------
# IP computation (same as expand_hosts.py)
# ---------------------------------------------------------------------------

def compute_host_ip(subnet_cidr, host_offset):
    network = ipaddress.ip_network(subnet_cidr, strict=False)
    return str(network.network_address + host_offset)


def build_host_fqdns(config):
    """Build host FQDN list from expand_hosts_config.json."""
    fqdn = config["fqdn"]
    hosts = []
    for host in config["expansionHosts"]:
        hostname = host["hostName"]
        ip = compute_host_ip(config["esxiSubnetCidr"], host["hostOffset"])
        hosts.append({
            "hostName": hostname,
            "fqdn": f"{hostname}.{fqdn}",
            "ip": ip,
        })
    return hosts


# ---------------------------------------------------------------------------
# SDDC Manager commissioning operations
# ---------------------------------------------------------------------------

def get_network_pool_id(client, pool_name):
    """Look up a network pool ID by name from SDDC Manager."""
    resp = client.get("/v1/network-pools")
    check_response(resp, "list network pools")

    for pool in resp.json().get("elements", []):
        if pool.get("name") == pool_name:
            pool_id = pool.get("id")
            LOG.info("Network pool '%s' -> %s", pool_name, pool_id)
            return pool_id

    available = [p.get("name") for p in resp.json().get("elements", [])]
    print(
        f"ERROR: Network pool '{pool_name}' not found.\n"
        f"  Available pools: {', '.join(available)}",
        file=sys.stderr,
    )
    sys.exit(1)


def get_commissioned_hosts(client):
    """List all hosts already known to SDDC Manager."""
    resp = client.get("/v1/hosts")
    check_response(resp, "list hosts")

    hosts = {}
    for host in resp.json().get("elements", []):
        host_fqdn = host.get("fqdn", "")
        if host_fqdn:
            hosts[host_fqdn.lower()] = host.get("status", "UNKNOWN")

    LOG.info("SDDC Manager hosts: %s", ", ".join(sorted(hosts.keys())))
    return hosts


def validate_hosts(client, host_specs):
    """Validate hosts before commissioning (POST /v1/hosts/validations)."""
    print("\nValidating hosts...")
    resp = client.post("/v1/hosts/validations", host_specs)
    check_response(resp, "validate hosts")

    validation = resp.json()
    validation_id = validation.get("id")
    if not validation_id:
        print("WARNING: No validation ID returned, skipping validation poll",
              file=sys.stderr)
        return True

    LOG.info("Validation ID: %s", validation_id)
    return poll_validation(client, validation_id)


def poll_validation(client, validation_id):
    """Poll validation status until complete."""
    start = time.time()
    while time.time() - start < POLL_TIMEOUT:
        resp = client.get(f"/v1/hosts/validations/{validation_id}")
        check_response(resp, f"poll validation {validation_id}")

        data = resp.json()
        status = data.get("executionStatus", "UNKNOWN")
        result = data.get("resultStatus", "")
        LOG.info("Validation %s: status=%s result=%s",
                 validation_id, status, result)

        if status == "COMPLETED":
            if result == "SUCCEEDED":
                print(f"  Validation passed")
                return True
            else:
                print(f"\nValidation FAILED:", file=sys.stderr)
                for check in data.get("validationChecks", []):
                    if check.get("resultStatus") != "SUCCEEDED":
                        print(f"  {check.get('description', 'unknown')}: "
                              f"{check.get('resultStatus')}",
                              file=sys.stderr)
                        for err in check.get("errors", []):
                            print(f"    - {err}", file=sys.stderr)
                sys.exit(1)

        time.sleep(POLL_INTERVAL)

    print(f"ERROR: Validation timed out after {POLL_TIMEOUT}s",
          file=sys.stderr)
    sys.exit(1)


def commission_hosts(client, host_specs):
    """Commission hosts via POST /v1/hosts."""
    resp = client.post("/v1/hosts", host_specs)
    check_response(resp, "commission hosts")

    data = resp.json()
    task_id = data.get("id")
    if task_id:
        LOG.info("Commission task ID: %s", task_id)
        return task_id

    print("  Commission request accepted")
    return None


def poll_task(client, task_id):
    """Poll a task until it completes."""
    print(f"\nPolling task {task_id}...")
    start = time.time()

    while time.time() - start < POLL_TIMEOUT:
        resp = client.get(f"/v1/tasks/{task_id}")
        check_response(resp, f"poll task {task_id}")

        data = resp.json()
        status = data.get("status", "UNKNOWN")
        LOG.info("Task %s: %s", task_id, status)

        if status in ("Successful", "SUCCESSFUL", "COMPLETED_WITH_WARNING"):
            print(f"  Task completed: {status}")
            return True
        elif status in ("Failed", "FAILED", "CANCELLED", "TIMED_OUT"):
            print(f"\nTask {status}:", file=sys.stderr)
            for err in data.get("errors", []):
                print(f"  - {err}", file=sys.stderr)
            sys.exit(1)

        elapsed = int(time.time() - start)
        print(f"  [{elapsed}s] Status: {status}")
        time.sleep(POLL_INTERVAL)

    print(f"ERROR: Task timed out after {POLL_TIMEOUT}s", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# vCenter host verification
# ---------------------------------------------------------------------------

def verify_hosts_in_vcenter(vc_client, expected_fqdns):
    """Verify commissioned hosts appear in the Management vCenter."""
    vc_hosts = vc_client.get_hosts()
    vc_names = {h.get("name", "").lower() for h in vc_hosts}

    print("\nVerifying hosts in Management vCenter:")
    all_found = True
    for fqdn in expected_fqdns:
        if fqdn.lower() in vc_names:
            print(f"  {fqdn:<50} OK")
        else:
            print(f"  {fqdn:<50} NOT YET VISIBLE")
            all_found = False

    if not all_found:
        print("\n  Some hosts are not yet visible in vCenter.")
        print("  They may take a few minutes to appear after commissioning.")
        print("  Check: vCenter > Global Inventory Lists > Hosts")

    return all_found


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def run_commission(args):
    with open(args.config) as f:
        config = json.load(f)

    region = args.region or config.get("region", "us-east-1")
    env_id = config["environmentId"]
    fqdn = config["fqdn"]
    proxy = args.proxy

    vc_host = args.vcenter_host or f"vc.{fqdn}"
    sddcm_host = args.sddcm_host or f"sddcm.{fqdn}"
    hosts = build_host_fqdns(config)

    print(f"\nVCF 9.1 Host Commissioning")
    print(f"  Workflow:        Primary (vCenter SSO)")
    print(f"  vCenter:         {vc_host}")
    print(f"  SDDC Manager:    {sddcm_host}")
    print(f"  Environment:     {env_id}")
    print(f"  Proxy:           {proxy}")
    print(f"  Hosts:           {len(hosts)}")
    for h in hosts:
        print(f"    {h['hostName']:<12} {h['fqdn']:<45} {h['ip']}")
    print()

    # --- AWS session ---
    session_kwargs = {"region_name": region}
    if args.profile:
        session_kwargs["profile_name"] = args.profile
    sm = boto3.Session(**session_kwargs).client("secretsmanager")

    # --- Get vCenter SSO credentials ---
    sso_user, sso_password = get_vcenter_sso_credentials(sm, env_id)
    print(f"  SSO user:        {sso_user}")

    # --- Authenticate to Management vCenter ---
    vc_client = VCenterClient(vc_host, proxy=proxy)
    if not args.dry_run:
        vc_client.authenticate(sso_user, sso_password)
        vc_hosts = vc_client.get_hosts()
        vc_host_names = {h.get("name", "").lower() for h in vc_hosts}
        print(f"  vCenter hosts:   {len(vc_hosts)} currently in inventory")

    # --- Authenticate to SDDC Manager with SSO credentials ---
    sddcm_client = SDDCManagerClient(sddcm_host, proxy=proxy)
    if not args.dry_run:
        sddcm_client.authenticate(sso_user, sso_password)

    # --- Look up network pool ID ---
    if not args.dry_run:
        network_pool_id = get_network_pool_id(sddcm_client, args.network_pool_name)
        print(f"  Network pool:    {args.network_pool_name} ({network_pool_id})")
    else:
        network_pool_id = "<dry-run>"

    # --- Pre-flight: check existing hosts in SDDC Manager ---
    if not args.dry_run:
        existing = get_commissioned_hosts(sddcm_client)
        to_commission = []
        for h in hosts:
            fqdn_lower = h["fqdn"].lower()
            if fqdn_lower in existing:
                status = existing[fqdn_lower]
                print(f"  {h['hostName']:<12} already in SDDC Manager "
                      f"(status={status}) — skipped")
            else:
                to_commission.append(h)
    else:
        to_commission = hosts

    if not to_commission:
        print("\nAll hosts already commissioned. Nothing to do.\n")
        return

    print(f"\n  {len(to_commission)} host(s) to commission, "
          f"{len(hosts) - len(to_commission)} already exist\n")

    host_specs = []
    print("Gathering host credentials and thumbprints:\n")

    for h in to_commission:
        password = get_esxi_password(sm, env_id, h["hostName"])

        if not args.dry_run:
            thumbprint = fetch_ssl_thumbprint(h["fqdn"])
        else:
            thumbprint = "<dry-run>"

        print(f"  {h['hostName']:<12} thumbprint={thumbprint[:20]}...")

        host_specs.append({
            "fqdn": h["fqdn"],
            "username": "root",
            "password": password,
            "storageType": args.storage_type,
            "networkPoolId": network_pool_id,
            "sslThumbprint": thumbprint,
        })

    # --- Dry run summary ---
    if args.dry_run:
        print(f"\n[DRY RUN] Would commission {len(host_specs)} host(s):\n")
        for spec in host_specs:
            safe = {k: v for k, v in spec.items() if k != "password"}
            safe["password"] = "***"
            print(f"  {json.dumps(safe)}")
        print("\n[DRY RUN] No changes made.\n")
        return

    # --- Validate ---
    if not args.skip_validation:
        validate_hosts(sddcm_client, host_specs)

    # --- Commission ---
    print(f"\nCommissioning {len(host_specs)} host(s) via SDDC Manager "
          f"(SSO auth)...")
    task_id = commission_hosts(sddcm_client, host_specs)

    if task_id:
        poll_task(sddcm_client, task_id)

    print(f"\n{len(host_specs)} host(s) commissioned successfully.")

    # --- Verify in vCenter ---
    if not args.skip_vcenter_check:
        verify_hosts_in_vcenter(
            vc_client,
            [h["fqdn"] for h in to_commission],
        )

    print(f"\nDone.")
    print(f"\nNext steps:")
    print(f"  1. Open vCenter: https://{vc_host}/ui")
    print(f"     Go to: Global Inventory Lists > Hosts")
    print(f"     Verify hosts appear under 'Unassigned Hosts'")
    print(f"  2. Deploy workload domain via VCF Operations:")
    print(f"     https://vcfops01.{fqdn}/vcf-operations/")
    print(f"     Inventory > VCF Instances > Add Workload Domain")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Commission expansion ESXi hosts using the VCF 9.1 "
            "recommended workflow (vCenter SSO authentication)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
VCF 9.1 Recommended Workflow:
  1. Commission hosts via SDDC Manager API (SSO auth)
     - Same API the vCenter VCF plugin uses internally
     - Hosts appear under Global Inventory Lists > Unassigned Hosts
  2. Deploy workload domain via VCF Operations console

Legacy Workflow (deprecated):
  Use commission_hosts.py (SDDC Manager local auth)

Examples:
  # Full run:
  python3 commission_hosts_vcf9.py \\
      --config expand_hosts_config.json \\
      --profile ci

  # Dry run:
  python3 commission_hosts_vcf9.py \\
      --config expand_hosts_config.json \\
      --profile ci --dry-run

  # Skip validation:
  python3 commission_hosts_vcf9.py \\
      --config expand_hosts_config.json \\
      --profile ci --skip-validation

  # Without proxy (direct access):
  python3 commission_hosts_vcf9.py \\
      --config expand_hosts_config.json \\
      --profile ci --proxy ""
""",
    )

    parser.add_argument("--config", required=True,
                        help="Path to expand_hosts_config.json")
    parser.add_argument("--profile", help="AWS CLI profile name")
    parser.add_argument("--region", help="AWS region override")
    parser.add_argument("--vcenter-host", dest="vcenter_host",
                        help="Management vCenter FQDN (default: vc.{fqdn})")
    parser.add_argument("--sddcm-host", dest="sddcm_host",
                        help="SDDC Manager FQDN (default: sddcm.{fqdn})")
    parser.add_argument("--proxy", default=HTTPS_PROXY,
                        help=f"HTTPS proxy (default: {HTTPS_PROXY})")
    parser.add_argument("--network-pool-name", dest="network_pool_name",
                        default="env-ksksyki5m0-cl01_pool",
                        help="Network pool name for host commissioning")
    parser.add_argument("--storage-type", dest="storage_type",
                        default=DEFAULT_STORAGE_TYPE,
                        help=f"Storage type (default: {DEFAULT_STORAGE_TYPE})")
    parser.add_argument("--skip-validation", dest="skip_validation",
                        action="store_true",
                        help="Skip host validation before commissioning")
    parser.add_argument("--skip-vcenter-check", dest="skip_vcenter_check",
                        action="store_true",
                        help="Skip post-commission vCenter verification")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="Show what would happen without making changes")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    run_commission(args)


if __name__ == "__main__":
    main()
