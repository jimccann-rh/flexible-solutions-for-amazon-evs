#!/usr/bin/env python3
"""
Commission expansion ESXi hosts in VCF Operations Manager.

After expand_hosts.py adds new ESXi hosts to the EVS environment at the AWS
layer, this script commissions them in the VCF Operations Manager (SDDC
Manager) so VCF can manage them. It authenticates to the VCF Operations API,
fetches each host's root password from AWS Secrets Manager and SSL thumbprint
via a live TLS connection, looks up the network pool, and calls the
commissioning API.

Requirements:
  pip install boto3 requests

Usage:
  python3 commission_hosts.py \
      --config expand_hosts_config.json \
      --profile ci

  # Dry run:
  python3 commission_hosts.py \
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

LOG = logging.getLogger("commission_hosts")

HTTPS_PROXY = os.environ.get("HTTPS_PROXY", "http://127.0.0.1:9999")
DEFAULT_STORAGE_TYPE = "VSAN_ESA"
POLL_INTERVAL = 30
POLL_TIMEOUT = 1800


# ---------------------------------------------------------------------------
# VCF Operations API client
# ---------------------------------------------------------------------------

class VCFOpsClient:
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
            print(f"ERROR: Authentication failed ({resp.status_code})",
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
        LOG.info("Authenticated to %s", self.base)

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

def get_secret_password(sm, secret_id):
    """Retrieve a password from AWS Secrets Manager."""
    LOG.info("Fetching secret %s", secret_id)
    response = sm.get_secret_value(SecretId=secret_id)
    raw = response.get("SecretString", "")
    try:
        data = json.loads(raw)
        return data.get("password", raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def get_sddcm_credentials(sm, env_id):
    """Get SDDC Manager local admin credentials from Secrets Manager."""
    secret_id = f"evs-{env_id}_sddcManagerLocal"
    LOG.info("Fetching SDDC Manager secret %s", secret_id)
    response = sm.get_secret_value(SecretId=secret_id)
    raw = response.get("SecretString", "")
    try:
        data = json.loads(raw)
        return data.get("username", "admin@local"), data.get("password", raw)
    except (json.JSONDecodeError, TypeError):
        return "admin@local", raw


def get_esxi_password(sm, env_id, hostname):
    """Get ESXi root password from Secrets Manager (evs!{envId}_{hostname})."""
    secret_id = f"evs!{env_id}_{hostname}"
    return get_secret_password(sm, secret_id)


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
# VCF Operations API operations
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
    """List all hosts already known to VCF Operations Manager."""
    resp = client.get("/v1/hosts")
    check_response(resp, "list hosts")

    fqdns = set()
    for host in resp.json().get("elements", []):
        host_fqdn = host.get("fqdn", "")
        if host_fqdn:
            fqdns.add(host_fqdn.lower())

    LOG.info("Existing hosts in VCF Ops: %s", ", ".join(sorted(fqdns)))
    return fqdns


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
        return

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
# Main flow
# ---------------------------------------------------------------------------

def run_commission(args):
    with open(args.config) as f:
        config = json.load(f)

    region = args.region or config.get("region", "us-east-1")
    env_id = config["environmentId"]
    fqdn = config["fqdn"]
    proxy = args.proxy

    sddcm_host = args.sddcm_host or f"sddcm.{fqdn}"
    hosts = build_host_fqdns(config)

    print(f"\nCommission hosts in SDDC Manager")
    print(f"  SDDC Manager:  {sddcm_host}")
    print(f"  Environment:   {env_id}")
    print(f"  Proxy:         {proxy}")
    print(f"  Hosts:         {len(hosts)}")
    for h in hosts:
        print(f"    {h['hostName']:<12} {h['fqdn']:<45} {h['ip']}")
    print()

    # --- AWS session (needed for auth + ESXi passwords) ---
    session_kwargs = {"region_name": region}
    if args.profile:
        session_kwargs["profile_name"] = args.profile
    sm = boto3.Session(**session_kwargs).client("secretsmanager")

    # --- Auth ---
    admin_user, admin_password = get_sddcm_credentials(sm, env_id)

    client = VCFOpsClient(sddcm_host, proxy=proxy)

    if not args.dry_run:
        client.authenticate(admin_user, admin_password)

    # --- Look up network pool ID ---
    if not args.dry_run:
        network_pool_id = get_network_pool_id(client, args.network_pool_name)
        print(f"  Network pool:  {args.network_pool_name} ({network_pool_id})")
    else:
        network_pool_id = "<dry-run>"

    # --- Pre-flight: skip already-commissioned hosts ---
    if not args.dry_run:
        existing = get_commissioned_hosts(client)
        to_commission = []
        for h in hosts:
            if h["fqdn"].lower() in existing:
                print(f"  {h['hostName']:<12} already commissioned — skipped")
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
        validate_hosts(client, host_specs)

    # --- Commission ---
    print(f"\nCommissioning {len(host_specs)} host(s)...")
    task_id = commission_hosts(client, host_specs)

    if task_id:
        poll_task(client, task_id)

    print(f"\nDone: {len(host_specs)} host(s) commissioned.\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Commission expansion ESXi hosts in VCF Operations Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full run:
  python3 commission_hosts.py \\
      --config expand_hosts_config.json \\
      --profile ci

  # Dry run:
  python3 commission_hosts.py \\
      --config expand_hosts_config.json \\
      --profile ci --dry-run

  # Skip validation step:
  python3 commission_hosts.py \\
      --config expand_hosts_config.json \\
      --profile ci --skip-validation
""",
    )

    parser.add_argument("--config", required=True,
                        help="Path to expand_hosts_config.json")
    parser.add_argument("--profile", help="AWS CLI profile name")
    parser.add_argument("--region", help="AWS region override")
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
