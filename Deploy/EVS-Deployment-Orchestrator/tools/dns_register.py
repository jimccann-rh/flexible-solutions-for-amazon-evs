#!/usr/bin/env python3
"""Register VM DNS records in Route 53 — A records and PTR records.

Queries vCenter for VM names and IPs (via VMware Tools), then creates or updates
A records in the forward zone and PTR records in the reverse zone.

Actions:
  show     - List VMs and their current DNS state
  register - Create/update A + PTR records for VMs
  delete   - Remove A + PTR records for VMs
  sync     - Register all VMs found in vCenter, remove stale records
"""

import argparse
import ipaddress
import json
import os
import re
import sys
import time
import urllib3

import boto3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_DOMAIN = "vci.devcluster.openshift.com"
DEFAULT_TTL = 300


SOAP_UPTIME = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:urn="urn:vim25">
  <soapenv:Body>
    <urn:RetrieveProperties>
      <urn:_this type="PropertyCollector">propertyCollector</urn:_this>
      <urn:specSet>
        <urn:propSet>
          <urn:type>VirtualMachine</urn:type>
          <urn:pathSet>summary.quickStats.uptimeSeconds</urn:pathSet>
        </urn:propSet>
        <urn:objectSet>
          <urn:obj type="VirtualMachine">{vm_id}</urn:obj>
        </urn:objectSet>
      </urn:specSet>
    </urn:RetrieveProperties>
  </soapenv:Body>
</soapenv:Envelope>"""

SOAP_LOGIN = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:urn="urn:vim25">
  <soapenv:Body>
    <urn:Login>
      <urn:_this type="SessionManager">SessionManager</urn:_this>
      <urn:userName>{user}</urn:userName>
      <urn:password>{password}</urn:password>
    </urn:Login>
  </soapenv:Body>
</soapenv:Envelope>"""


class VCenterClient:
    def __init__(self, host, user, password, proxy=None):
        self.base = f"https://{host}"
        self.session = requests.Session()
        self.session.verify = False
        if proxy:
            self.session.proxies = {"https": proxy, "http": proxy}
        resp = self.session.post(
            f"{self.base}/api/session",
            auth=(user, password),
        )
        resp.raise_for_status()
        token = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text.strip('"')
        self.session.headers["vmware-api-session-id"] = token
        self._soap_cookie = None
        self._user = user
        self._password = password

    def get(self, path):
        resp = self.session.get(f"{self.base}{path}")
        resp.raise_for_status()
        return resp.json()

    def list_vms(self):
        return self.get("/api/vcenter/vm")

    def get_vm_ips(self, vm_id):
        try:
            nics = self.get(f"/api/vcenter/vm/{vm_id}/guest/networking/interfaces")
        except requests.exceptions.HTTPError:
            return []
        ips = []
        for nic in nics:
            for addr in nic.get("ip", {}).get("ip_addresses", []):
                ip_str = addr.get("ip_address", "")
                if ":" in ip_str:
                    continue
                try:
                    ip = ipaddress.ip_address(ip_str)
                    if not ip.is_loopback and not ip.is_link_local:
                        ips.append(ip_str)
                except ValueError:
                    pass
        return ips

    def get_vm_info(self, vm_id):
        try:
            vm = self.get(f"/api/vcenter/vm/{vm_id}")
            return vm.get("name", vm_id), vm.get("power_state", "UNKNOWN")
        except requests.exceptions.HTTPError:
            return vm_id, "UNKNOWN"

    def _soap_login(self):
        if self._soap_cookie:
            return
        body = SOAP_LOGIN.format(user=self._user, password=self._password)
        resp = self.session.post(
            f"{self.base}/sdk",
            data=body,
            headers={"Content-Type": "text/xml", "SOAPAction": "urn:vim25/Login"},
            verify=False,
        )
        resp.raise_for_status()
        self._soap_cookie = resp.cookies.get("vmware_soap_session")

    def get_vm_uptime(self, vm_id):
        """Get VM uptime in seconds via SOAP PropertyCollector (OS-agnostic)."""
        try:
            self._soap_login()
            body = SOAP_UPTIME.format(vm_id=vm_id)
            cookies = {}
            if self._soap_cookie:
                cookies["vmware_soap_session"] = self._soap_cookie
            resp = self.session.post(
                f"{self.base}/sdk",
                data=body,
                headers={"Content-Type": "text/xml", "SOAPAction": "urn:vim25/RetrieveProperties"},
                cookies=cookies,
                verify=False,
            )
            resp.raise_for_status()
            match = re.search(r"<val[^>]*>(\d+)</val>", resp.text)
            if match:
                return int(match.group(1))
        except Exception:
            pass
        return 0


class Route53Client:
    def __init__(self, profile=None, region=None):
        session = boto3.Session(profile_name=profile, region_name=region)
        self.client = session.client("route53")
        self._zones = None

    def find_forward_zone(self, domain):
        zones = self._list_zones()
        target = f"{domain}."
        for z in zones:
            if z["Name"] == target and z["Config"].get("PrivateZone"):
                return z["Id"].split("/")[-1]
        return None

    def find_reverse_zone(self, ip):
        zones = self._list_zones()
        octets = ip.split(".")
        for length in [3, 2]:
            target = ".".join(reversed(octets[:length])) + ".in-addr.arpa."
            for z in zones:
                if z["Name"] == target and z["Config"].get("PrivateZone"):
                    return z["Id"], length
        return None, None

    def _list_zones(self):
        if self._zones is None:
            self._zones = []
            resp = self.client.list_hosted_zones()
            self._zones = resp.get("HostedZones", [])
        return self._zones

    def get_record(self, zone_id, name, rtype):
        resp = self.client.list_resource_record_sets(
            HostedZoneId=zone_id,
            StartRecordName=name,
            StartRecordType=rtype,
            MaxItems="1",
        )
        for r in resp.get("ResourceRecordSets", []):
            if r["Name"] == name and r["Type"] == rtype:
                return r
        return None

    def upsert_record(self, zone_id, name, rtype, value, ttl=DEFAULT_TTL):
        self.client.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={
                "Changes": [{
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": name,
                        "Type": rtype,
                        "TTL": ttl,
                        "ResourceRecords": [{"Value": value}],
                    },
                }],
            },
        )

    def delete_record(self, zone_id, name, rtype, value, ttl=DEFAULT_TTL):
        try:
            self.client.change_resource_record_sets(
                HostedZoneId=zone_id,
                ChangeBatch={
                    "Changes": [{
                        "Action": "DELETE",
                        "ResourceRecordSet": {
                            "Name": name,
                            "Type": rtype,
                            "TTL": ttl,
                            "ResourceRecords": [{"Value": value}],
                        },
                    }],
                },
            )
            return True
        except self.client.exceptions.InvalidChangeBatch:
            return False

    def list_a_records(self, zone_id):
        """List all A records in a zone. Returns list of (name, ip, ttl)."""
        records = []
        paginator = self.client.get_paginator("list_resource_record_sets")
        for page in paginator.paginate(HostedZoneId=zone_id):
            for r in page.get("ResourceRecordSets", []):
                if r["Type"] == "A" and "ResourceRecords" in r:
                    ip = r["ResourceRecords"][0]["Value"]
                    records.append((r["Name"], ip, r.get("TTL", DEFAULT_TTL)))
        return records

    def ptr_name(self, ip):
        octets = ip.split(".")
        return ".".join(reversed(octets)) + ".in-addr.arpa."


def sanitize_hostname(vm_name):
    """Convert a vSphere VM name to a valid DNS hostname."""
    name = vm_name.lower()
    name = re.sub(r'[^a-z0-9-]', '-', name)
    name = re.sub(r'-+', '-', name)
    name = name.strip('-')
    return name


def ip_in_range(ip_str, ip_range):
    """Check if an IP matches a range pattern like '10.28.40-48.*' or '10.28.40.0/21'."""
    if "/" in ip_range:
        return ipaddress.ip_address(ip_str) in ipaddress.ip_network(ip_range, strict=False)
    octets = ip_str.split(".")
    parts = ip_range.split(".")
    if len(parts) != 4 or len(octets) != 4:
        return False
    for octet, part in zip(octets, parts):
        if part == "*":
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            if not (int(lo) <= int(octet) <= int(hi)):
                return False
        elif octet != part:
            return False
    return True


def vc_short_name(vc_host):
    """Extract short identifier from vCenter hostname (e.g. 'vc2.example.com' -> 'vc2')."""
    return vc_host.split(".")[0]


def gather_vms(vc_clients, prefix=None, ip_range=None, dedupe_prefix=False, first_only=False):
    """Get powered-on VMs with IPs from all vCenters. Optionally filter by name prefix and IP range."""
    results = []
    seen_ips = set()
    for vc_host, vc in vc_clients:
        vms = vc.list_vms()
        for vm in vms:
            vm_id = vm["vm"]
            name, power_state = vc.get_vm_info(vm_id)
            if power_state != "POWERED_ON":
                continue
            ips = vc.get_vm_ips(vm_id)
            if not ips:
                continue
            if prefix and not name.lower().startswith(prefix.lower()):
                continue
            if ip_range:
                ip = next((i for i in ips if ip_in_range(i, ip_range)), None)
                if not ip:
                    continue
            else:
                ip = ips[0]
            if ip in seen_ips:
                continue
            seen_ips.add(ip)
            results.append({"vm_id": vm_id, "name": name, "ips": ips, "power_state": power_state, "vc_host": vc_host})

    if first_only:
        seen_names = set()
        deduped = []
        for vm in results:
            sanitized = sanitize_hostname(vm["name"])
            if sanitized in seen_names:
                continue
            seen_names.add(sanitized)
            deduped.append(vm)
        results = deduped
    else:
        name_groups = {}
        for vm in results:
            sanitized = sanitize_hostname(vm["name"])
            name_groups.setdefault(sanitized, []).append(vm)
        dupes = {n: vms for n, vms in name_groups.items() if len(vms) > 1}
        if dupes:
            vc_map = {h: vc for h, vc in vc_clients}
            for name, dupe_vms in dupes.items():
                for vm in dupe_vms:
                    vc = vc_map.get(vm["vc_host"])
                    if vc:
                        vm["_uptime"] = vc.get_vm_uptime(vm["vm_id"])
                    else:
                        vm["_uptime"] = 0
                best = max(dupe_vms, key=lambda v: v["_uptime"])
                print(f"  Duplicate VM '{name}': keeping {best['vc_host']} (uptime {best['_uptime']}s)")
                for vm in dupe_vms:
                    if vm is not best:
                        results.remove(vm)

    if dedupe_prefix:
        name_counts = {}
        for vm in results:
            sanitized = sanitize_hostname(vm["name"])
            name_counts[sanitized] = name_counts.get(sanitized, 0) + 1
        for vm in results:
            sanitized = sanitize_hostname(vm["name"])
            if name_counts[sanitized] > 1:
                short = vc_short_name(vm["vc_host"])
                vm["dns_hostname"] = f"{short}-{sanitized}"
            else:
                vm["dns_hostname"] = sanitized
    else:
        for vm in results:
            vm["dns_hostname"] = sanitize_hostname(vm["name"])

    return results


def action_show(vc_clients, r53, args):
    domain = args.domain
    fwd_zone_id = r53.find_forward_zone(domain)
    if not fwd_zone_id:
        print(f"ERROR: forward zone '{domain}' not found in Route 53", file=sys.stderr)
        sys.exit(1)

    vms = gather_vms(vc_clients, prefix=args.prefix, ip_range=args.ip_range, dedupe_prefix=args.dedupe_prefix, first_only=args.first_only)
    if not vms:
        print("No powered-on VMs with IPs found.")
        return

    print(f"{'VM Name':45s}  {'DNS Hostname':30s}  {'IP':16s}  {'vCenter':20s}  {'DNS (A)':15s}  {'DNS (PTR)':15s}")
    print("-" * 150)

    for vm in vms:
        hostname = vm["dns_hostname"]
        fqdn = f"{hostname}.{domain}."
        ip = vm["ips"][0]

        a_rec = r53.get_record(fwd_zone_id, fqdn, "A")
        a_status = "OK" if a_rec else "MISSING"
        if a_rec:
            existing_ip = a_rec["ResourceRecords"][0]["Value"]
            if existing_ip != ip:
                a_status = f"STALE ({existing_ip})"

        ptr_name = r53.ptr_name(ip)
        rev_zone_id, _ = r53.find_reverse_zone(ip)
        if rev_zone_id:
            ptr_rec = r53.get_record(rev_zone_id, ptr_name, "PTR")
            ptr_status = "OK" if ptr_rec else "MISSING"
        else:
            ptr_status = "NO ZONE"

        print(f"  {vm['name']:43s}  {hostname:30s}  {ip:16s}  {vm.get('vc_host',''):20s}  {a_status:15s}  {ptr_status:15s}")


def action_register(vc_clients, r53, args):
    domain = args.domain
    fwd_zone_id = r53.find_forward_zone(domain)
    if not fwd_zone_id:
        print(f"ERROR: forward zone '{domain}' not found in Route 53", file=sys.stderr)
        sys.exit(1)

    if args.hostname and args.ip:
        vms = [{"name": args.hostname, "ips": [args.ip], "power_state": "POWERED_ON"}]
    elif args.vm_name:
        vms = gather_vms(vc_clients, prefix=None, ip_range=args.ip_range, dedupe_prefix=args.dedupe_prefix, first_only=args.first_only)
        vms = [v for v in vms if v["name"] == args.vm_name]
        if not vms:
            print(f"ERROR: VM '{args.vm_name}' not found (must be powered on with VMware Tools)", file=sys.stderr)
            sys.exit(1)
    else:
        print("ERROR: use --vm-name or --hostname/--ip", file=sys.stderr)
        sys.exit(1)

    for vm in vms:
        if args.hostname and getattr(args, "raw_hostname", False):
            hostname = args.hostname
        elif args.hostname:
            hostname = sanitize_hostname(args.hostname)
        else:
            hostname = vm.get("dns_hostname", sanitize_hostname(vm["name"]))
        ip = vm["ips"][0]
        fqdn = f"{hostname}.{domain}."

        if args.dry_run:
            print(f"  DRY RUN: would create A {fqdn} -> {ip}")
        else:
            r53.upsert_record(fwd_zone_id, fqdn, "A", ip)
            print(f"  A     {fqdn:50s} -> {ip}")

        ptr_name = r53.ptr_name(ip)
        rev_zone_id, _ = r53.find_reverse_zone(ip)
        if rev_zone_id:
            if args.dry_run:
                print(f"  DRY RUN: would create PTR {ptr_name} -> {fqdn}")
            else:
                r53.upsert_record(rev_zone_id, ptr_name, "PTR", fqdn)
                print(f"  PTR   {ptr_name:50s} -> {fqdn}")
        else:
            print(f"  PTR   skipped — no reverse zone found for {ip}")

    if not args.dry_run:
        print(f"\nDone. {len(vms)} VM(s) registered.")


def action_delete(vc_clients, r53, args):
    domain = args.domain
    fwd_zone_id = r53.find_forward_zone(domain)
    if not fwd_zone_id:
        print(f"ERROR: forward zone '{domain}' not found in Route 53", file=sys.stderr)
        sys.exit(1)

    if args.hostname and args.ip:
        hostname = args.hostname if getattr(args, "raw_hostname", False) else sanitize_hostname(args.hostname)
        ip = args.ip
    elif args.vm_name:
        vms = gather_vms(vc_clients, prefix=None, ip_range=args.ip_range, dedupe_prefix=args.dedupe_prefix, first_only=args.first_only)
        vms = [v for v in vms if v["name"] == args.vm_name]
        if not vms:
            print(f"ERROR: VM '{args.vm_name}' not found", file=sys.stderr)
            sys.exit(1)
        hostname = sanitize_hostname(vms[0]["name"])
        ip = vms[0]["ips"][0]
    else:
        print("ERROR: use --vm-name or --hostname/--ip", file=sys.stderr)
        sys.exit(1)

    fqdn = f"{hostname}.{domain}."

    a_rec = r53.get_record(fwd_zone_id, fqdn, "A")
    if a_rec:
        existing_ip = a_rec["ResourceRecords"][0]["Value"]
        if args.dry_run:
            print(f"  DRY RUN: would delete A {fqdn} -> {existing_ip}")
        else:
            r53.delete_record(fwd_zone_id, fqdn, "A", existing_ip, a_rec.get("TTL", DEFAULT_TTL))
            print(f"  Deleted A     {fqdn} -> {existing_ip}")
    else:
        print(f"  A     {fqdn} — not found, skipping")

    ptr_name = r53.ptr_name(ip)
    rev_zone_id, _ = r53.find_reverse_zone(ip)
    if rev_zone_id:
        ptr_rec = r53.get_record(rev_zone_id, ptr_name, "PTR")
        if ptr_rec:
            existing_fqdn = ptr_rec["ResourceRecords"][0]["Value"]
            if args.dry_run:
                print(f"  DRY RUN: would delete PTR {ptr_name} -> {existing_fqdn}")
            else:
                r53.delete_record(rev_zone_id, ptr_name, "PTR", existing_fqdn, ptr_rec.get("TTL", DEFAULT_TTL))
                print(f"  Deleted PTR   {ptr_name} -> {existing_fqdn}")
        else:
            print(f"  PTR   {ptr_name} — not found, skipping")

    if not args.dry_run:
        print("\nDone.")


def action_sync(vc_clients, r53, args):
    domain = args.domain
    fwd_zone_id = r53.find_forward_zone(domain)
    if not fwd_zone_id:
        print(f"ERROR: forward zone '{domain}' not found in Route 53", file=sys.stderr)
        sys.exit(1)

    ip_range = args.ip_range
    if not ip_range and getattr(args, "cleanup_prefix", None) and "/" in args.cleanup_prefix:
        ip_range = args.cleanup_prefix

    vms = gather_vms(vc_clients, prefix=args.prefix, ip_range=ip_range, dedupe_prefix=args.dedupe_prefix, first_only=args.first_only)
    if not vms:
        print("No powered-on VMs with IPs found.")
        return

    vc_counts = {}
    for vm in vms:
        h = vm.get("vc_host", "unknown")
        vc_counts[h] = vc_counts.get(h, 0) + 1

    print(f"=== Syncing {len(vms)} VM(s) to Route 53 ({domain}) ===")
    for h, c in vc_counts.items():
        print(f"  {h}: {c} VM(s)")
    print()

    created = 0
    updated = 0
    unchanged = 0

    for vm in vms:
        hostname = vm["dns_hostname"]
        ip = vm["ips"][0]
        fqdn = f"{hostname}.{domain}."

        a_rec = r53.get_record(fwd_zone_id, fqdn, "A")
        if a_rec:
            existing_ip = a_rec["ResourceRecords"][0]["Value"]
            if existing_ip == ip:
                unchanged += 1
                continue
            if args.dry_run:
                print(f"  DRY RUN: would update A {fqdn} {existing_ip} -> {ip}")
            else:
                r53.upsert_record(fwd_zone_id, fqdn, "A", ip)
                print(f"  Updated A     {fqdn:50s} {existing_ip} -> {ip}")
            updated += 1
        else:
            if args.dry_run:
                print(f"  DRY RUN: would create A {fqdn} -> {ip}")
            else:
                r53.upsert_record(fwd_zone_id, fqdn, "A", ip)
                print(f"  Created A     {fqdn:50s} -> {ip}")
            created += 1

        ptr_name = r53.ptr_name(ip)
        rev_zone_id, _ = r53.find_reverse_zone(ip)
        if rev_zone_id:
            ptr_rec = r53.get_record(rev_zone_id, ptr_name, "PTR")
            if ptr_rec:
                existing_fqdn = ptr_rec["ResourceRecords"][0]["Value"]
                if existing_fqdn == fqdn:
                    continue
            if args.dry_run:
                print(f"  DRY RUN: would upsert PTR {ptr_name} -> {fqdn}")
            else:
                r53.upsert_record(rev_zone_id, ptr_name, "PTR", fqdn)
                print(f"  Created PTR   {ptr_name:50s} -> {fqdn}")

    cleaned = 0
    if getattr(args, "cleanup", False):
        cleanup_prefix = getattr(args, "cleanup_prefix", None)
        vm_fqdns = set()
        vm_ips = set()
        for vm in vms:
            hostname = vm["dns_hostname"]
            vm_fqdns.add(f"{hostname}.{domain}.")
            vm_ips.add(vm["ips"][0])

        all_a_records = r53.list_a_records(fwd_zone_id)
        for rec_name, rec_ip, rec_ttl in all_a_records:
            if not rec_name.endswith(f".{domain}."):
                continue
            if rec_name in vm_fqdns:
                continue
            short = rec_name.replace(f".{domain}.", "")
            if not short:
                continue
            if cleanup_prefix and not short.startswith(cleanup_prefix.lower()):
                continue
            if args.dry_run:
                print(f"  DRY RUN: would remove stale A {rec_name} -> {rec_ip}")
            else:
                r53.delete_record(fwd_zone_id, rec_name, "A", rec_ip, rec_ttl)
                print(f"  Removed stale A     {rec_name:50s} -> {rec_ip}")
            ptr_name = r53.ptr_name(rec_ip)
            rev_zone_id, _ = r53.find_reverse_zone(rec_ip)
            if rev_zone_id:
                ptr_rec = r53.get_record(rev_zone_id, ptr_name, "PTR")
                if ptr_rec:
                    ptr_val = ptr_rec["ResourceRecords"][0]["Value"]
                    ptr_normalized = ptr_val.rstrip(".") + "."
                    rec_normalized = rec_name.rstrip(".") + "."
                    if ptr_normalized == rec_normalized:
                        if args.dry_run:
                            print(f"  DRY RUN: would remove stale PTR {ptr_name} -> {ptr_val}")
                        else:
                            r53.delete_record(rev_zone_id, ptr_name, "PTR", ptr_val, ptr_rec.get("TTL", DEFAULT_TTL))
                            print(f"  Removed stale PTR   {ptr_name:50s} -> {ptr_val}")
            cleaned += 1

    print(f"\n  Created: {created}  Updated: {updated}  Unchanged: {unchanged}  Cleaned: {cleaned}")
    if args.dry_run:
        print("  (dry run — no changes made)")


def build_parser():
    top = argparse.ArgumentParser(
        description="Register VM DNS records (A + PTR) in Route 53",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s show
  %(prog)s register --vm-name my-vm-01
  %(prog)s register --hostname myhost --ip 10.28.40.53
  %(prog)s delete   --vm-name my-vm-01
  %(prog)s sync
  %(prog)s sync     --prefix test --dry-run
""",
    )
    sub = top.add_subparsers(dest="action", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vc-host", action="append", default=None,
                        help="vCenter hostname (repeatable, or comma-separated, or VC_HOSTS env var)")
    common.add_argument("--vc-user", default=os.environ.get("VC_USER", "ai@vsphere.local"))
    common.add_argument("--vc-password", default=os.environ.get("VC_PASSWORD"))
    common.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY", "http://127.0.0.1:9999"))
    common.add_argument("--aws-profile", default=os.environ.get("AWS_PROFILE", "ci"))
    common.add_argument("--aws-region", default=os.environ.get("AWS_REGION", "us-east-1"))
    common.add_argument("--domain", default=DEFAULT_DOMAIN, help=f"DNS domain (default: {DEFAULT_DOMAIN})")
    common.add_argument("--ip-range", default=None,
                        help="Only process VMs in this IP range (e.g. '10.28.40-48.*' or '10.28.40.0/21')")
    common.add_argument("--dedupe-prefix", action="store_true",
                        help="Prefix DNS hostname with vCenter name when duplicate VM names exist across vCenters")
    common.add_argument("--first-only", action="store_true",
                        help="When duplicate VM names exist, keep only the first one found (skip duplicates)")
    common.add_argument("--dry-run", action="store_true", help="Show what would change without applying")

    show = sub.add_parser("show", parents=[common], help="Show VMs and their DNS record status")
    show.add_argument("--prefix", default=None, help="Only show VMs whose name starts with this prefix")

    reg = sub.add_parser("register", parents=[common], help="Create/update A + PTR records for a VM")
    reg.add_argument("--vm-name", default=None, help="vSphere VM name (looks up IP via VMware Tools)")
    reg.add_argument("--hostname", default=None, help="DNS hostname (used with --ip for manual registration)")
    reg.add_argument("--ip", default=None, help="VM IP address (used with --hostname)")
    reg.add_argument("--raw-hostname", action="store_true",
                     help="Use hostname exactly as given (skip sanitization, allows subdomains like api.jimccann-dev)")

    delete = sub.add_parser("delete", parents=[common], help="Remove A + PTR records for a VM")
    delete.add_argument("--vm-name", default=None, help="vSphere VM name")
    delete.add_argument("--hostname", default=None, help="DNS hostname (used with --ip)")
    delete.add_argument("--ip", default=None, help="VM IP address (used with --hostname)")
    delete.add_argument("--raw-hostname", action="store_true",
                     help="Use hostname exactly as given (skip sanitization)")

    sync = sub.add_parser("sync", parents=[common], help="Sync all VM DNS records from vCenter")
    sync.add_argument("--prefix", default=None, help="Only sync VMs whose name starts with this prefix")
    sync.add_argument("--cleanup", action="store_true", help="Remove stale A + PTR records not matching any current VM")
    sync.add_argument("--cleanup-prefix", default=None,
                       help="Only clean up records whose hostname starts with this prefix (protects infrastructure records)")
    sync.add_argument("--watch", action="store_true", help="Continuously sync on a timer")
    sync.add_argument("--watch-interval", type=int, default=120, help="Seconds between watch cycles (default: 120)")

    return top


def main():
    parser = build_parser()
    args = parser.parse_args()

    r53 = Route53Client(profile=args.aws_profile, region=args.aws_region)

    vc_hosts = args.vc_host
    if not vc_hosts:
        env_hosts = os.environ.get("VC_HOSTS", os.environ.get("VC_HOST", "vc.vci.devcluster.openshift.com"))
        vc_hosts = [h.strip() for h in env_hosts.split(",") if h.strip()]
    else:
        expanded = []
        for h in vc_hosts:
            expanded.extend(h2.strip() for h2 in h.split(",") if h2.strip())
        vc_hosts = expanded

    vc_clients = []
    needs_vc = args.action in ("show", "sync") or (args.action in ("register", "delete") and not (getattr(args, "hostname", None) and getattr(args, "ip", None)))

    if args.vc_password and needs_vc:
        for host in vc_hosts:
            try:
                vc = VCenterClient(host, args.vc_user, args.vc_password, proxy=args.proxy)
                vc_clients.append((host, vc))
                print(f"  Connected to vCenter: {host}")
            except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                print(f"WARNING: failed to connect to vCenter {host}: {e}", file=sys.stderr)
        if not vc_clients:
            print("ERROR: could not connect to any vCenter", file=sys.stderr)
            sys.exit(1)
        print()
    elif needs_vc:
        print("ERROR: vCenter credentials required. Use --vc-password or VC_PASSWORD.", file=sys.stderr)
        sys.exit(1)

    actions = {
        "show": action_show,
        "register": action_register,
        "delete": action_delete,
        "sync": action_sync,
    }

    if args.action == "sync" and getattr(args, "watch", False):
        watch_interval = args.watch_interval
        print(f"Watching for VM changes every {watch_interval}s (Ctrl+C to stop)\n")
        try:
            while True:
                actions[args.action](vc_clients, r53, args)
                print(f"\n--- Next sync in {watch_interval}s ---")
                time.sleep(watch_interval)
                print()
        except KeyboardInterrupt:
            print("\nWatch stopped.")
    else:
        actions[args.action](vc_clients, r53, args)


if __name__ == "__main__":
    main()
