#!/usr/bin/env python3
"""
Add expansion ESXi hosts to an existing AWS EVS environment.

Creates Route53 forward (A) and reverse (PTR) DNS records for each new host,
then calls the EVS CreateEnvironmentHost API to provision them. Designed for
day-2 host expansion after the initial EVS environment is created by Phase 2.

IP addresses are computed from the ESXi management subnet CIDR and each host's
offset, matching the same convention used by Phase 1 Terraform (cidrhost).

Requirements:
  pip install boto3

Usage:
  # Full run — DNS + EVS:
  python3 expand_hosts.py --config expand_hosts_config.json --profile ci

  # Dry run:
  python3 expand_hosts.py --config expand_hosts_config.json --profile ci --dry-run

  # DNS only (skip EVS host creation):
  python3 expand_hosts.py --config expand_hosts_config.json --profile ci --skip-evs

  # EVS only (skip DNS, records already exist):
  python3 expand_hosts.py --config expand_hosts_config.json --profile ci --skip-dns
"""

import argparse
import ipaddress
import json
import logging
import sys
import time

import boto3
from botocore.exceptions import ClientError

LOG = logging.getLogger("expand_hosts")

DEFAULT_HOST_QUOTA = 5


# ---------------------------------------------------------------------------
# IP computation
# ---------------------------------------------------------------------------

def compute_host_ip(subnet_cidr, host_offset):
    """Compute an IP from a subnet CIDR and host offset.

    Matches Terraform's cidrhost() behavior:
      cidrhost("10.28.33.0/25", 15) -> "10.28.33.15"
    """
    network = ipaddress.ip_network(subnet_cidr, strict=False)
    return str(network.network_address + host_offset)


def build_host_records(config):
    """Build the list of host records with computed IPs."""
    subnet_cidr = config["esxiSubnetCidr"]
    fqdn = config["fqdn"]
    records = []

    for host in config["expansionHosts"]:
        ip = compute_host_ip(subnet_cidr, host["hostOffset"])
        hostname = host["hostName"]
        records.append({
            "hostName": hostname,
            "keyName": host["keyName"],
            "instanceType": host["instanceType"],
            "ip": ip,
            "fqdn_record": f"{hostname}.{fqdn}",
            "ptr_name": ".".join(reversed(ip.split("."))) + ".in-addr.arpa",
        })

    return records


# ---------------------------------------------------------------------------
# Route53 helpers
# ---------------------------------------------------------------------------

def find_forward_zone_id(r53, fqdn):
    """Find the Route53 hosted zone ID for the given FQDN."""
    resp = r53.list_hosted_zones_by_name(DNSName=fqdn, MaxItems="10")
    for zone in resp.get("HostedZones", []):
        if zone["Name"].rstrip(".") == fqdn:
            zone_id = zone["Id"].split("/")[-1]
            LOG.info("Forward zone: %s (id: %s)", fqdn, zone_id)
            return zone_id

    raise RuntimeError(f"No Route53 hosted zone found for '{fqdn}'")


def find_reverse_zone_id(r53, ip):
    """Find the reverse DNS zone for the given IP.

    Looks for a two-octet reverse zone matching Phase 1 Terraform's
    convention: {octet1}.{octet0}.in-addr.arpa
    """
    octets = ip.split(".")
    reverse_zone_name = f"{octets[1]}.{octets[0]}.in-addr.arpa"

    resp = r53.list_hosted_zones_by_name(
        DNSName=reverse_zone_name, MaxItems="10",
    )
    for zone in resp.get("HostedZones", []):
        if zone["Name"].rstrip(".") == reverse_zone_name:
            zone_id = zone["Id"].split("/")[-1]
            LOG.info("Reverse zone: %s (id: %s)", reverse_zone_name, zone_id)
            return zone_id

    raise RuntimeError(
        f"No Route53 reverse zone found for '{reverse_zone_name}'. "
        f"Check that Phase 1 Terraform created the reverse zone."
    )


def upsert_dns_record(r53, zone_id, name, record_type, value, ttl=300):
    """Create or update a DNS record using UPSERT (idempotent)."""
    r53.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={
            "Comment": f"EVS host expansion: {name}",
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": name,
                        "Type": record_type,
                        "TTL": ttl,
                        "ResourceRecords": [{"Value": value}],
                    },
                }
            ],
        },
    )


def create_dns_records(r53, records, fqdn, dry_run=False):
    """Create forward A and reverse PTR records for all hosts."""
    first_ip = records[0]["ip"]
    forward_zone_id = find_forward_zone_id(r53, fqdn)
    reverse_zone_id = find_reverse_zone_id(r53, first_ip)

    print(f"\nRoute53 DNS records ({len(records)} hosts):\n")

    for rec in records:
        a_name = f"{rec['fqdn_record']}."
        ptr_name = f"{rec['ptr_name']}."
        ptr_value = f"{rec['fqdn_record']}."

        print(f"  A    {rec['fqdn_record']:<45} -> {rec['ip']}")
        print(f"  PTR  {rec['ptr_name']:<45} -> {rec['fqdn_record']}")

        if not dry_run:
            LOG.info("UPSERT A: %s -> %s", rec["fqdn_record"], rec["ip"])
            upsert_dns_record(
                r53, forward_zone_id, a_name, "A", rec["ip"],
            )

            LOG.info("UPSERT PTR: %s -> %s", rec["ptr_name"], rec["fqdn_record"])
            upsert_dns_record(
                r53, reverse_zone_id, ptr_name, "PTR", ptr_value,
            )

            print(f"         created")
        print()

    if dry_run:
        print("[DRY RUN] No DNS changes made.\n")
    else:
        print(f"Done: {len(records)} forward + {len(records)} reverse records created.\n")


# ---------------------------------------------------------------------------
# EVS helpers
# ---------------------------------------------------------------------------

def resolve_esx_version(evs, config, instance_type):
    """Resolve the ESXi version, matching Phase 2's logic."""
    esx_version = config.get("esxVersion")
    if esx_version:
        LOG.info("ESXi version from config: %s", esx_version)
        return esx_version

    target = config.get("vcfInstallerProductVersion", "")
    if not target:
        print(
            "ERROR: 'esxVersion' and 'vcfInstallerProductVersion' are both "
            "empty. Set at least one in the config.",
            file=sys.stderr,
        )
        sys.exit(1)

    LOG.info("Resolving ESXi version from EVS API (target=%s, type=%s)",
             target, instance_type)
    response = evs.get_versions()

    esx_versions = []
    for entry in response.get("instanceTypeEsxVersions", []):
        if entry.get("instanceType") == instance_type:
            esx_versions = entry.get("esxVersions", [])
            break

    if not esx_versions:
        print(
            f"ERROR: No ESXi versions found for instance type "
            f"'{instance_type}' via get-versions API.",
            file=sys.stderr,
        )
        sys.exit(1)

    parts = target.split(".")
    prefix = f"ESXi-{parts[0]}.{parts[1]}." if len(parts) >= 2 else f"ESXi-{target}"

    matching = [v for v in esx_versions if v.startswith(prefix)]
    if not matching:
        print(
            f"ERROR: No ESXi version matching '{prefix}*' for "
            f"'{instance_type}'. Available: {esx_versions}",
            file=sys.stderr,
        )
        sys.exit(1)

    matching.sort(key=lambda v: int(v.rsplit(".", 1)[-1]), reverse=True)
    latest = matching[0]
    LOG.info("Resolved ESXi version: %s (%d candidates)", latest, len(matching))
    return latest


def verify_environment_ready(evs, environment_id):
    """Check that the EVS environment is in CREATED state."""
    resp = evs.get_environment(environmentId=environment_id)
    env = resp.get("environment", {})
    state = env.get("environmentState", "UNKNOWN")
    LOG.info("Environment %s state: %s", environment_id, state)

    if state != "CREATED":
        print(
            f"ERROR: Environment {environment_id} is in state '{state}', "
            f"expected 'CREATED'.",
            file=sys.stderr,
        )
        sys.exit(1)

    return env


def get_existing_hosts(evs, environment_id):
    """List all hosts already in the EVS environment (paginated).

    Returns a dict of {hostname: {"state": str, "stateDetails": str}}.
    """
    hosts = {}
    next_token = None

    while True:
        params = {"environmentId": environment_id}
        if next_token:
            params["nextToken"] = next_token

        response = evs.list_environment_hosts(**params)
        for host in response.get("environmentHosts", []):
            name = host.get("hostName", "")
            if name:
                hosts[name] = {
                    "state": host.get("hostState", "UNKNOWN"),
                    "stateDetails": host.get("stateDetails", ""),
                }

        next_token = response.get("nextToken")
        if not next_token:
            break

    return hosts


EVS_HOST_QUOTA_CODE = "L-96A49955"
EVS_SERVICE_CODE = "evs"

EC2_VCPU_QUOTA_CODE = "L-1216C47A"
EC2_SERVICE_CODE = "ec2"

HOST_POLL_INTERVAL = 60
HOST_POLL_TIMEOUT = 3600


def get_aws_host_quota(session, region):
    """Query the actual EVS host-per-environment quota from AWS Service Quotas.

    Tries the applied (account-level) value first, falls back to the AWS
    default if no custom value has been set.
    """
    try:
        sq = session.client("service-quotas", region_name=region)
        try:
            resp = sq.get_service_quota(
                ServiceCode=EVS_SERVICE_CODE,
                QuotaCode=EVS_HOST_QUOTA_CODE,
            )
            value = int(resp["Quota"]["Value"])
            LOG.info("AWS applied quota %s = %d", EVS_HOST_QUOTA_CODE, value)
            return value
        except sq.exceptions.NoSuchResourceException:
            resp = sq.get_aws_default_service_quota(
                ServiceCode=EVS_SERVICE_CODE,
                QuotaCode=EVS_HOST_QUOTA_CODE,
            )
            value = int(resp["Quota"]["Value"])
            LOG.info("AWS default quota %s = %d", EVS_HOST_QUOTA_CODE, value)
            return value
    except Exception as e:
        LOG.warning("Could not query Service Quotas API: %s", e)

    return None


def get_pending_quota_request(sq, service_code, quota_code):
    """Check for an existing pending quota increase request."""
    try:
        resp = sq.list_requested_service_quota_change_history_by_quota(
            ServiceCode=service_code,
            QuotaCode=quota_code,
        )
        for req in resp.get("RequestedQuotas", []):
            status = req.get("Status", "")
            if status in ("PENDING", "CASE_OPENED"):
                return req
    except Exception as e:
        LOG.warning("Could not check pending quota requests: %s", e)
    return None


def request_quota_increase(session, region, desired_value,
                           service_code=EVS_SERVICE_CODE,
                           quota_code=EVS_HOST_QUOTA_CODE):
    """Request a quota increase via the Service Quotas API.

    Returns True if a request was submitted (or already pending).
    """
    sq = session.client("service-quotas", region_name=region)

    pending = get_pending_quota_request(sq, service_code, quota_code)
    if pending:
        pending_value = int(pending.get("DesiredValue", 0))
        case_id = pending.get("CaseId", "N/A")
        status = pending.get("Status", "UNKNOWN")
        print(
            f"\n  A quota increase request is already pending:\n"
            f"    Status:          {status}\n"
            f"    Requested value: {pending_value}\n"
            f"    Case ID:         {case_id}\n",
        )
        if pending_value >= desired_value:
            print("  The pending request already covers the desired value.\n")
            return True
        print(
            f"  The pending request ({pending_value}) is less than the "
            f"desired value ({desired_value}).\n"
            f"  You may need to open a new request after the current one "
            f"completes.\n",
            file=sys.stderr,
        )
        return True

    print(f"\n  Requesting quota increase: {desired_value} hosts per "
          f"environment...")

    try:
        resp = sq.request_service_quota_increase(
            ServiceCode=service_code,
            QuotaCode=quota_code,
            DesiredValue=float(desired_value),
        )
        req = resp.get("RequestedQuota", {})
        case_id = req.get("CaseId", "N/A")
        status = req.get("Status", "UNKNOWN")
        print(
            f"  Quota increase request submitted!\n"
            f"    Desired value:   {desired_value}\n"
            f"    Status:          {status}\n"
            f"    Case ID:         {case_id}\n\n"
            f"  Monitor progress:\n"
            f"    AWS Console > Service Quotas > EVS > "
            f"'Host count per EVS environment'\n"
            f"    or: aws service-quotas get-requested-service-quota-change "
            f"--request-id {req.get('Id', '<id>')}\n",
        )
        return True
    except ClientError as e:
        code = e.response["Error"].get("Code", "")
        if code == "AccessDeniedException":
            print(
                f"\n  ERROR: IAM user is not authorized to request quota "
                f"increases.\n"
                f"  Add the following IAM permissions to the user/role:\n"
                f"    servicequotas:RequestServiceQuotaIncrease\n"
                f"    servicequotas:ListRequestedServiceQuotaChange"
                f"HistoryByQuota\n"
                f"\n"
                f"  Or request the increase manually:\n"
                f"    AWS Console > Service Quotas > EVS > "
                f"'Host count per EVS environment' > Request increase\n",
                file=sys.stderr,
            )
        else:
            print(f"\n  ERROR: Could not request quota increase: {e}\n",
                  file=sys.stderr)
        return False
    except Exception as e:
        print(f"\n  ERROR: Could not request quota increase: {e}\n",
              file=sys.stderr)
        return False


def get_ec2_vcpu_info(session, region, instance_type):
    """Look up vCPUs per host and current EC2 on-demand vCPU quota.

    Returns (vcpus_per_host, vcpu_quota) or (None, None) on failure.
    """
    try:
        ec2 = session.client("ec2", region_name=region)
        resp = ec2.describe_instance_types(InstanceTypes=[instance_type])
        itypes = resp.get("InstanceTypes", [])
        if not itypes:
            return None, None
        vcpus_per_host = itypes[0]["VCpuInfo"]["DefaultVCpus"]
    except Exception as e:
        LOG.warning("Could not determine vCPUs for %s: %s", instance_type, e)
        return None, None

    try:
        sq = session.client("service-quotas", region_name=region)
        try:
            resp = sq.get_service_quota(
                ServiceCode=EC2_SERVICE_CODE,
                QuotaCode=EC2_VCPU_QUOTA_CODE,
            )
            vcpu_quota = int(resp["Quota"]["Value"])
        except sq.exceptions.NoSuchResourceException:
            resp = sq.get_aws_default_service_quota(
                ServiceCode=EC2_SERVICE_CODE,
                QuotaCode=EC2_VCPU_QUOTA_CODE,
            )
            vcpu_quota = int(resp["Quota"]["Value"])
    except Exception as e:
        LOG.warning("Could not query EC2 vCPU quota: %s", e)
        return vcpus_per_host, None

    return vcpus_per_host, vcpu_quota


def print_ec2_vcpu_console_steps(instance_type, vcpus_needed, environment_id,
                                 failed_hosts):
    """Print step-by-step instructions for resolving vCPU quota failures."""
    print(
        f"\n  To resolve via the AWS Console:\n"
        f"\n"
        f"  Step 1 — Increase EC2 vCPU quota:\n"
        f"    1. Open the AWS Console\n"
        f"    2. Go to: Service Quotas > Amazon Elastic Compute Cloud (Amazon EC2)\n"
        f"    3. Search for: 'Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances'\n"
        f"    4. Click 'Request increase at account level'\n"
        f"    5. Enter desired vCPU value: {vcpus_needed} or higher\n"
        f"    6. Submit and wait for approval (usually minutes for small increases)\n"
        f"\n"
        f"  Step 2 — Delete the failed host(s):\n"
        f"    1. Go to: Amazon EVS > Environments > {environment_id}\n"
        f"    2. Select the Hosts tab\n"
        f"    3. Select: {', '.join(failed_hosts)}\n"
        f"    4. Actions > Delete host\n"
        f"\n"
        f"  Step 3 — Re-run this script\n",
    )


def remediate_vcpu_failure(session, region, instance_type, num_total_hosts,
                           environment_id, failed_hosts):
    """Detect and attempt to fix EC2 vCPU quota issues for failed hosts.

    Returns True if a quota increase was attempted or instructions were given.
    """
    vcpus_per_host, vcpu_quota = get_ec2_vcpu_info(
        session, region, instance_type,
    )

    if vcpus_per_host is None:
        return False

    vcpus_needed = num_total_hosts * vcpus_per_host

    if vcpu_quota is not None and vcpus_needed <= vcpu_quota:
        print(
            f"\n  EC2 vCPU quota is now sufficient "
            f"({vcpus_needed}/{vcpu_quota}).\n"
            f"  The failed host(s) were likely created before a recent "
            f"quota increase.",
        )
        print(
            f"\n  To retry — delete the failed host(s) and re-run:\n"
            f"    AWS Console > Amazon EVS > Environments > "
            f"{environment_id} > Hosts\n"
            f"    Select: {', '.join(failed_hosts)} > Actions > Delete host\n",
        )
        return True

    if vcpu_quota is not None:
        print(
            f"\n  EC2 vCPU quota is still insufficient: "
            f"need {vcpus_needed}, have {vcpu_quota}.",
        )

    increased = request_quota_increase(
        session, region, float(vcpus_needed),
        EC2_SERVICE_CODE, EC2_VCPU_QUOTA_CODE,
    )

    if not increased:
        print_ec2_vcpu_console_steps(
            instance_type, vcpus_needed, environment_id, failed_hosts,
        )

    return True


def check_ec2_vcpu_quota(session, region, instance_type, num_existing,
                         num_new, auto_request_increase=False):
    """Pre-flight check for EC2 on-demand vCPU quota."""
    vcpus_per_host, vcpu_quota = get_ec2_vcpu_info(
        session, region, instance_type,
    )

    if vcpus_per_host is None:
        LOG.warning("Skipping EC2 vCPU check — could not look up %s",
                    instance_type)
        return

    if vcpu_quota is None:
        LOG.warning("Skipping EC2 vCPU check — could not query quota")
        return

    total_hosts = num_existing + num_new
    vcpus_needed = total_hosts * vcpus_per_host

    print(f"  EC2 on-demand vCPU check ({EC2_VCPU_QUOTA_CODE}):")
    print(f"    Instance type:   {instance_type} ({vcpus_per_host} vCPUs)")
    print(f"    EVS hosts:       {num_existing} existing + {num_new} new "
          f"= {total_hosts}")
    print(f"    vCPUs needed:    {vcpus_needed} (EVS hosts only)")
    print(f"    vCPU quota:      {vcpu_quota}")

    if vcpus_needed > vcpu_quota:
        print(
            f"\n  WARNING: EVS hosts alone require {vcpus_needed} vCPUs "
            f"but the on-demand quota is {vcpu_quota}.",
            file=sys.stderr,
        )
        if auto_request_increase:
            request_quota_increase(
                session, region, float(vcpus_needed),
                EC2_SERVICE_CODE, EC2_VCPU_QUOTA_CODE,
            )
            print(
                f"\n  Re-run this script once the vCPU quota increase is "
                f"approved.\n",
            )
            sys.exit(0)
        else:
            print(
                f"  Increase your EC2 on-demand vCPU limit via Service "
                f"Quotas,\n"
                f"  or re-run with --request-quota-increase.\n",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        print(f"    Status:          OK ({vcpus_needed}/{vcpu_quota} from "
              f"EVS hosts)")
        if vcpus_needed > vcpu_quota * 0.8:
            print(f"    Note: Other EC2 instances also count toward this "
                  f"quota")
    print()


def poll_host_status(evs, environment_id, new_hostnames,
                     session=None, region=None, instance_type=None,
                     num_total_hosts=0):
    """Poll newly created hosts until they reach a terminal state."""
    print(f"\nPolling host creation status...")
    start = time.time()
    pending = set(new_hostnames)
    created = []
    failed = []
    failed_details = {}

    while pending and (time.time() - start) < HOST_POLL_TIMEOUT:
        time.sleep(HOST_POLL_INTERVAL)

        host_info = {}
        next_token = None
        while True:
            params = {"environmentId": environment_id}
            if next_token:
                params["nextToken"] = next_token
            response = evs.list_environment_hosts(**params)
            for host in response.get("environmentHosts", []):
                name = host.get("hostName", "")
                if name in pending:
                    host_info[name] = {
                        "state": host.get("hostState", "UNKNOWN"),
                        "stateDetails": host.get("stateDetails", ""),
                    }
            next_token = response.get("nextToken")
            if not next_token:
                break

        still_pending = set()
        for name in sorted(pending):
            info = host_info.get(name, {})
            state = info.get("state", "UNKNOWN")
            details = info.get("stateDetails", "")
            elapsed = int(time.time() - start)

            if state == "CREATED":
                print(f"  {name:<12} CREATED  ({elapsed}s)")
                created.append(name)
            elif "FAIL" in state.upper():
                print(
                    f"  WARNING: {name:<12} {state}  ({elapsed}s)",
                    file=sys.stderr,
                )
                if details:
                    print(f"           Reason: {details}", file=sys.stderr)
                    failed_details[name] = details
                failed.append(name)
            else:
                still_pending.add(name)
                LOG.info("  %s: %s (%ds)", name, state, elapsed)

        pending = still_pending
        if pending:
            elapsed = int(time.time() - start)
            print(f"  [{elapsed}s] Waiting: {', '.join(sorted(pending))}")

    if pending:
        print(
            f"\n  WARNING: Hosts still creating after {HOST_POLL_TIMEOUT}s: "
            f"{', '.join(sorted(pending))}",
            file=sys.stderr,
        )

    if failed:
        print(
            f"\n  WARNING: {len(failed)} host(s) FAILED: "
            f"{', '.join(sorted(failed))}",
            file=sys.stderr,
        )

        vcpu_failures = [
            name for name in failed
            if name in failed_details
            and ("vcpu" in failed_details[name].lower()
                 or "vCPU" in failed_details[name]
                 or "On-Demand Instance" in failed_details[name])
        ]

        if vcpu_failures and session and instance_type:
            remediate_vcpu_failure(
                session, region, instance_type,
                num_total_hosts, environment_id, vcpu_failures,
            )

    print(f"\n  Summary: {len(created)} created, {len(failed)} failed, "
          f"{len(pending)} still creating\n")

    return failed


def create_evs_hosts(evs, records, config, session, region,
                     dry_run=False, host_quota=DEFAULT_HOST_QUOTA,
                     auto_request_increase=False):
    """Add expansion hosts to the EVS environment."""
    environment_id = config["environmentId"]
    instance_type = records[0]["instanceType"]

    esx_version = resolve_esx_version(evs, config, instance_type)

    aws_quota = get_aws_host_quota(session, region)
    effective_quota = host_quota

    if aws_quota is not None:
        if host_quota > aws_quota:
            if auto_request_increase:
                print(
                    f"\n  --host-quota {host_quota} exceeds the current "
                    f"AWS quota of {aws_quota}.",
                )
                request_quota_increase(session, region, host_quota)
                print(
                    f"  Re-run this script once the quota increase is "
                    f"approved.\n",
                )
                sys.exit(0)
            else:
                print(
                    f"\n  WARNING: --host-quota {host_quota} exceeds the "
                    f"actual AWS Service Quota of {aws_quota}.\n"
                    f"  The quota increase may not have taken effect yet.\n"
                    f"  Use --request-quota-increase to automatically request "
                    f"an increase,\n"
                    f"  or check: AWS Console > Service Quotas > EVS > "
                    f"'Host count per EVS environment'\n"
                    f"  Using the AWS quota value of {aws_quota} instead.\n",
                    file=sys.stderr,
                )
            effective_quota = aws_quota
        else:
            effective_quota = max(host_quota, aws_quota)
    else:
        LOG.info("Could not retrieve AWS quota, using --host-quota %d",
                 host_quota)

    print(f"\nEVS host creation ({len(records)} hosts):\n")
    print(f"  Environment:  {environment_id}")
    print(f"  ESXi version: {esx_version}")
    print(f"  Instance type: {instance_type}")
    print(f"  Host quota:    {effective_quota}"
          f"{' (from AWS Service Quotas)' if aws_quota is not None else ''}")
    print()

    existing = get_existing_hosts(evs, environment_id)

    check_ec2_vcpu_quota(
        session, region, instance_type,
        len(existing), len(records),
        auto_request_increase=auto_request_increase,
    )
    if existing:
        LOG.info("Existing hosts in environment: %s", ", ".join(sorted(existing)))

    to_create = []
    failed_existing = []
    for rec in records:
        if rec["hostName"] in existing:
            host_info = existing[rec["hostName"]]
            state = host_info["state"]
            details = host_info["stateDetails"]
            if "FAIL" in state.upper():
                print(
                    f"  WARNING: {rec['hostName']:<12} {state}",
                    file=sys.stderr,
                )
                if details:
                    print(f"           Reason: {details}", file=sys.stderr)
                failed_existing.append(rec["hostName"])
            else:
                print(f"  {rec['hostName']:<12} already exists — {state}")
        else:
            to_create.append(rec)

    if failed_existing:
        print(
            f"\n  {len(failed_existing)} existing host(s) in failed state: "
            f"{', '.join(failed_existing)}",
            file=sys.stderr,
        )

        vcpu_failures = [
            name for name in failed_existing
            if "vcpu" in existing[name]["stateDetails"].lower()
            or "vCPU" in existing[name]["stateDetails"]
            or "On-Demand Instance" in existing[name]["stateDetails"]
        ]

        if vcpu_failures:
            remediate_vcpu_failure(
                session, region, instance_type,
                len(existing) + len(to_create),
                environment_id, vcpu_failures,
            )
        else:
            print(
                f"\n  Delete the failed host(s) in the AWS EVS console and "
                f"re-run,\n"
                f"  or remove them from the config to skip.\n"
                f"    AWS Console > Amazon EVS > Environments > "
                f"{environment_id} > Hosts\n"
                f"    Select failed host(s) > Actions > Delete host\n",
                file=sys.stderr,
            )

    if not to_create:
        if failed_existing:
            sys.exit(1)
        print("\nAll hosts already exist. Nothing to do.\n")
        return

    total_after = len(existing) + len(to_create)
    if total_after > effective_quota:
        print(
            f"\n  WARNING: Host quota exceeded!\n"
            f"    Current hosts:   {len(existing)}\n"
            f"    Hosts to add:    {len(to_create)}\n"
            f"    Total would be:  {total_after}\n"
            f"    Account quota:   {effective_quota}",
            file=sys.stderr,
        )
        if auto_request_increase:
            request_quota_increase(session, region, total_after)
            print(
                f"\n  Re-run this script once the quota increase is "
                f"approved.\n",
            )
            sys.exit(0)
        else:
            print(
                f"\n  Request a quota increase in the AWS console under\n"
                f"  Service Quotas > EVS > 'Host count per EVS environment',\n"
                f"  or re-run with --host-quota {total_after} "
                f"--request-quota-increase.",
                file=sys.stderr,
            )
            sys.exit(1)

    print(f"\n  {len(to_create)} host(s) to create, "
          f"{len(records) - len(to_create)} already exist"
          f" (quota: {len(existing)}+{len(to_create)}="
          f"{total_after}/{effective_quota})\n")

    if dry_run:
        for rec in to_create:
            host_payload = {
                "hostName": rec["hostName"],
                "keyName": rec["keyName"],
                "instanceType": rec["instanceType"],
            }
            print(f"  [DRY RUN] Would create host: {rec['hostName']}")
            print(f"            IP: {rec['ip']}")
            print(f"            Payload: {json.dumps(host_payload)}")
            print()

        print("[DRY RUN] No EVS changes made.\n")
        return

    verify_environment_ready(evs, environment_id)

    submitted = []
    create_failures = []

    for rec in to_create:
        host_payload = {
            "hostName": rec["hostName"],
            "keyName": rec["keyName"],
            "instanceType": rec["instanceType"],
        }

        LOG.info("Creating host '%s' in environment %s...",
                 rec["hostName"], environment_id)

        try:
            response = evs.create_environment_host(
                environmentId=environment_id,
                host=host_payload,
                esxVersion=esx_version,
            )

            created_host = response.get("host", {})
            state = created_host.get("hostState", "unknown")
            print(f"  {rec['hostName']:<12} IP={rec['ip']:<16} state={state}")
            submitted.append(rec["hostName"])
        except ClientError as e:
            error_msg = e.response["Error"].get("Message", str(e))
            print(
                f"  WARNING: {rec['hostName']:<12} FAILED to create: "
                f"{error_msg}",
                file=sys.stderr,
            )
            create_failures.append(rec["hostName"])
        except Exception as e:
            print(
                f"  WARNING: {rec['hostName']:<12} FAILED to create: {e}",
                file=sys.stderr,
            )
            create_failures.append(rec["hostName"])

    if create_failures:
        print(
            f"\n  {len(create_failures)} host(s) failed at creation: "
            f"{', '.join(create_failures)}",
            file=sys.stderr,
        )

    if submitted:
        print(f"\n{len(submitted)} host creation request(s) submitted.")
        failed = poll_host_status(
            evs, environment_id, submitted,
            session=session, region=region,
            instance_type=instance_type,
            num_total_hosts=total_after,
        )
        create_failures.extend(failed)

    if create_failures:
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Add expansion ESXi hosts to an existing EVS environment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full run — DNS + EVS:
  python3 expand_hosts.py --config expand_hosts_config.json --profile ci

  # Dry run:
  python3 expand_hosts.py --config expand_hosts_config.json --profile ci --dry-run

  # DNS only:
  python3 expand_hosts.py --config expand_hosts_config.json --profile ci --skip-evs

  # EVS only (DNS already done):
  python3 expand_hosts.py --config expand_hosts_config.json --profile ci --skip-dns
""",
    )

    parser.add_argument("--config", required=True,
                        help="Path to expansion config JSON")
    parser.add_argument("--profile", help="AWS CLI profile name")
    parser.add_argument("--region", help="AWS region override")

    modes = parser.add_argument_group("modes")
    modes.add_argument("--dry-run", dest="dry_run", action="store_true",
                       help="Show what would happen without making changes")
    modes.add_argument("--skip-dns", dest="skip_dns", action="store_true",
                       help="Skip Route53 record creation")
    modes.add_argument("--skip-evs", dest="skip_evs", action="store_true",
                       help="Skip EVS host creation (DNS only)")
    modes.add_argument("--host-quota", dest="host_quota", type=int,
                       default=DEFAULT_HOST_QUOTA,
                       help=f"Max hosts per EVS environment "
                            f"(default: {DEFAULT_HOST_QUOTA})")
    modes.add_argument("--request-quota-increase",
                       dest="request_quota_increase", action="store_true",
                       help="Automatically request an AWS Service Quota "
                            "increase if --host-quota exceeds the current "
                            "quota")
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
    fqdn = config["fqdn"]
    environment_id = config["environmentId"]

    session_kwargs = {"region_name": region}
    if args.profile:
        session_kwargs["profile_name"] = args.profile
    session = boto3.Session(**session_kwargs)

    records = build_host_records(config)

    print(f"\nExpansion hosts for environment {environment_id}:")
    print(f"  FQDN:   {fqdn}")
    print(f"  Subnet: {config['esxiSubnetCidr']}")
    print(f"  Hosts:  {len(records)}")
    for rec in records:
        print(f"    {rec['hostName']:<12} -> {rec['ip']}")
    print()

    if not args.skip_dns:
        print("=" * 60)
        print("Phase 1: Route53 DNS records")
        print("=" * 60)
        r53 = session.client("route53")
        create_dns_records(r53, records, fqdn, dry_run=args.dry_run)

    if not args.skip_evs:
        print("=" * 60)
        print("Phase 2: EVS host creation")
        print("=" * 60)
        evs = session.client("evs")
        create_evs_hosts(evs, records, config, session, region,
                         dry_run=args.dry_run, host_quota=args.host_quota,
                         auto_request_increase=args.request_quota_increase)

    if args.skip_dns and args.skip_evs:
        print("Both --skip-dns and --skip-evs set — nothing to do.")


if __name__ == "__main__":
    main()
