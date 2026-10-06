#!/usr/bin/env python3
"""Manage NSX T1 Gateway Firewall rules to control internet access per VM.

Uses the T1 gateway firewall (runs on edge nodes) instead of DFW, which is
unavailable in AWS EVS environments running ENS_INTERRUPT mode.

VMs can be blocked by IP directly, or by tagging them in vCenter with the
"nointernet" tag and running the "sync" command.
"""

import argparse
import ipaddress
import os
import sys
import time
import urllib3

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

RFC1918 = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
PLACEHOLDER_IP = "240.0.0.1"

DEFAULT_TAG_CATEGORY = "network-policy"
DEFAULT_TAG_NAME = "nointernet"
DEFAULT_NSX_GROUP = "nointernet-vms"
DEFAULT_RFC1918_GROUP = "rfc1918-private"
DEFAULT_POLICY_ID = "block-internet-access"


# ---------------------------------------------------------------------------
# vCenter client
# ---------------------------------------------------------------------------
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

    def get(self, path):
        resp = self.session.get(f"{self.base}{path}")
        resp.raise_for_status()
        return resp.json()

    def post(self, path, data):
        resp = self.session.post(f"{self.base}{path}", json=data)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    def find_tag(self, category_name, tag_name):
        """Find a tag by category name and tag name. Returns (category_id, tag_id) or None."""
        for cid in self.get("/api/cis/tagging/category"):
            cat = self.get(f"/api/cis/tagging/category/{cid}")
            if cat["name"] == category_name:
                for tid in self.get("/api/cis/tagging/tag"):
                    tag = self.get(f"/api/cis/tagging/tag/{tid}")
                    if tag.get("category_id") == cid and tag["name"] == tag_name:
                        return cid, tid
                return cid, None
        return None, None

    def get_tagged_vms(self, tag_id):
        """Return list of VM IDs tagged with the given tag."""
        objs = self.post(f"/api/cis/tagging/tag-association/{tag_id}?action=list-attached-objects", {"tag_id": tag_id})
        return [o["id"] for o in (objs or []) if o.get("type") == "VirtualMachine"]

    def get_vm_ips(self, vm_id):
        """Get IPv4 addresses from VM guest networking (requires VMware Tools)."""
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
        """Return (name, power_state) for a VM. power_state is POWERED_ON, POWERED_OFF, or SUSPENDED."""
        try:
            vm = self.get(f"/api/vcenter/vm/{vm_id}")
            return vm.get("name", vm_id), vm.get("power_state", "UNKNOWN")
        except requests.exceptions.HTTPError:
            return vm_id, "UNKNOWN"

    def get_vm_name(self, vm_id):
        name, _ = self.get_vm_info(vm_id)
        return name


# ---------------------------------------------------------------------------
# NSX client
# ---------------------------------------------------------------------------
class NSXClient:
    def __init__(self, host, user, password, proxy=None):
        self.base = f"https://{host}/policy/api/v1"
        self.session = requests.Session()
        self.session.verify = False
        self.session.auth = (user, password)
        if proxy:
            self.session.proxies = {"https": proxy, "http": proxy}

    def get(self, path):
        resp = self.session.get(f"{self.base}{path}")
        resp.raise_for_status()
        return resp.json()

    def patch(self, path, data):
        resp = self.session.patch(f"{self.base}{path}", json=data)
        resp.raise_for_status()

    def delete(self, path):
        resp = self.session.delete(f"{self.base}{path}")
        resp.raise_for_status()

    def get_or_none(self, path):
        try:
            return self.get(path)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                return None
            raise

    def find_t1(self):
        data = self.get("/infra/tier-1s")
        results = data.get("results", [])
        if len(results) == 1:
            return results[0]
        if not results:
            print("ERROR: no Tier-1 gateway found", file=sys.stderr)
            sys.exit(1)
        print("ERROR: multiple Tier-1 gateways found. Use --t1-id:", file=sys.stderr)
        for t in results:
            print(f"  {t['id']}  {t['display_name']}", file=sys.stderr)
        sys.exit(1)

    def get_blocked_ips(self):
        grp = self.get_or_none(f"/infra/domains/default/groups/{DEFAULT_NSX_GROUP}")
        if not grp:
            return []
        for expr in grp.get("expression", []):
            if expr.get("resource_type") == "IPAddressExpression":
                ips = expr.get("ip_addresses", [])
                return [ip for ip in ips if ip != PLACEHOLDER_IP]
        return []

    def set_blocked_ips(self, ips):
        ip_list = ips if ips else [PLACEHOLDER_IP]
        self.patch(f"/infra/domains/default/groups/{DEFAULT_NSX_GROUP}", {
            "display_name": "nointernet-vms",
            "description": "VM IPs blocked from internet access",
            "expression": [{
                "ip_addresses": ip_list,
                "resource_type": "IPAddressExpression",
            }],
        })


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def action_setup(nsx, vc, args):
    t1_id = args.t1_id
    if not t1_id:
        t1_id = nsx.find_t1()["id"]

    print(f"=== NSX Gateway Firewall Setup (T1: {t1_id}) ===")

    t1 = nsx.get(f"/infra/tier-1s/{t1_id}")
    if t1.get("disable_firewall"):
        nsx.patch(f"/infra/tier-1s/{t1_id}", {"disable_firewall": False})
        print(f"  Enabled firewall on T1 {t1_id}")
    else:
        print(f"  T1 firewall already enabled")

    if nsx.get_or_none(f"/infra/domains/default/groups/{DEFAULT_RFC1918_GROUP}"):
        print(f"  Group '{DEFAULT_RFC1918_GROUP}' already exists")
    else:
        nsx.patch(f"/infra/domains/default/groups/{DEFAULT_RFC1918_GROUP}", {
            "display_name": "rfc1918-private",
            "description": "RFC1918 private address ranges",
            "expression": [{"ip_addresses": RFC1918, "resource_type": "IPAddressExpression"}],
        })
        print(f"  Created group '{DEFAULT_RFC1918_GROUP}'")

    if nsx.get_or_none(f"/infra/domains/default/groups/{DEFAULT_NSX_GROUP}"):
        print(f"  Group '{DEFAULT_NSX_GROUP}' already exists")
    else:
        nsx.patch(f"/infra/domains/default/groups/{DEFAULT_NSX_GROUP}", {
            "display_name": "nointernet-vms",
            "description": "VM IPs blocked from internet access",
            "expression": [{"ip_addresses": [PLACEHOLDER_IP], "resource_type": "IPAddressExpression"}],
        })
        print(f"  Created group '{DEFAULT_NSX_GROUP}'")

    t1_path = f"/infra/tier-1s/{t1_id}"
    vm_path = f"/infra/domains/default/groups/{DEFAULT_NSX_GROUP}"
    rfc_path = f"/infra/domains/default/groups/{DEFAULT_RFC1918_GROUP}"

    if nsx.get_or_none(f"/infra/domains/default/gateway-policies/{DEFAULT_POLICY_ID}"):
        print(f"  Policy '{DEFAULT_POLICY_ID}' already exists")
    else:
        nsx.patch(f"/infra/domains/default/gateway-policies/{DEFAULT_POLICY_ID}", {
            "display_name": "Block Internet Access",
            "description": "Block internet for specific VMs by source IP",
            "category": "LocalGatewayRules",
            "sequence_number": 100,
            "rules": [
                {
                    "display_name": "Allow-Private-Networks",
                    "description": "Allow nointernet VMs to reach RFC1918 destinations",
                    "sequence_number": 1,
                    "source_groups": [vm_path],
                    "destination_groups": [rfc_path],
                    "services": ["ANY"],
                    "action": "ALLOW",
                    "scope": [t1_path],
                    "logged": False,
                    "disabled": False,
                    "id": "allow-private",
                },
                {
                    "display_name": "Drop-Internet",
                    "description": "Drop internet traffic from nointernet VMs",
                    "sequence_number": 2,
                    "source_groups": [vm_path],
                    "destination_groups": ["ANY"],
                    "services": ["ANY"],
                    "action": "DROP",
                    "scope": [t1_path],
                    "logged": True,
                    "disabled": False,
                    "id": "drop-internet",
                },
            ],
        })
        print(f"  Created gateway policy '{DEFAULT_POLICY_ID}' with Allow-Private + Drop-Internet rules")

    print()
    print("Setup complete. Block VMs with:")
    print(f"  python3 nsx_tag_firewall.py block --ip <VM_IP>")
    print(f"  python3 nsx_tag_firewall.py sync  (reads vCenter '{DEFAULT_TAG_CATEGORY}/{DEFAULT_TAG_NAME}' tags)")


def action_block(nsx, vc, args):
    ip = args.ip
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        print(f"ERROR: invalid IP address: {ip}", file=sys.stderr)
        sys.exit(1)

    current = nsx.get_blocked_ips()
    if ip in current:
        print(f"IP {ip} is already blocked")
        return

    current.append(ip)
    nsx.set_blocked_ips(current)
    print(f"Blocked {ip} from internet access")


def action_unblock(nsx, vc, args):
    ip = args.ip
    current = nsx.get_blocked_ips()
    if ip not in current:
        print(f"IP {ip} is not currently blocked")
        return

    current.remove(ip)
    nsx.set_blocked_ips(current)
    print(f"Unblocked {ip} — internet access restored")


def _do_sync(nsx, vc, category, tag_name, loop=False, interval=10, timeout=600):
    """Run one sync cycle. Returns True if changes were made."""
    _, tag_id = vc.find_tag(category, tag_name)
    if not tag_id:
        print(f"ERROR: vCenter tag '{category}/{tag_name}' not found.", file=sys.stderr)
        print(f"Create it in vCenter: Tags & Custom Attributes > New Category '{category}' > New Tag '{tag_name}'", file=sys.stderr)
        sys.exit(1)

    vm_ids = vc.get_tagged_vms(tag_id)
    if not vm_ids:
        print(f"No VMs tagged with '{category}/{tag_name}' in vCenter")
        current = nsx.get_blocked_ips()
        if current:
            nsx.set_blocked_ips([])
            print(f"Cleared {len(current)} previously blocked IP(s)")
            return True
        return False

    start_time = time.time()
    attempt = 0
    while True:
        attempt += 1
        tagged_ips = []
        missing = []
        if attempt > 1:
            print()
        print(f"=== VMs tagged '{category}/{tag_name}' ===")
        powered_off = []
        for vm_id in vm_ids:
            name, power_state = vc.get_vm_info(vm_id)
            if power_state != "POWERED_ON":
                powered_off.append(name)
                print(f"  {name:40s}  {power_state}")
                continue
            ips = vc.get_vm_ips(vm_id)
            if ips:
                tagged_ips.extend(ips)
                print(f"  {name:40s}  {', '.join(ips)}")
            else:
                missing.append(name)
                print(f"  {name:40s}  POWERED_ON (no IP — VMware Tools running?)")

        if powered_off:
            print(f"\n  WARNING: {len(powered_off)} VM(s) not powered on — skipping")

        if missing and loop:
            elapsed = time.time() - start_time
            remaining = timeout - elapsed
            if remaining <= 0:
                print(f"\nTimeout after {timeout}s — {len(missing)} VM(s) still have no IP:")
                for name in missing:
                    print(f"  {name}")
                if tagged_ips:
                    print("Proceeding with available IPs.")
                break
            mins, secs = divmod(int(remaining), 60)
            print(f"\nWaiting for {len(missing)} VM(s) to report IP... retrying in {interval}s ({mins}m{secs}s remaining, Ctrl+C to stop)")
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                print("\nInterrupted.")
                if tagged_ips:
                    print("Proceeding with available IPs.")
                break
            continue

        break

    current = set(nsx.get_blocked_ips())
    new = set(tagged_ips)

    added = new - current
    removed = current - new

    if not added and not removed:
        if new:
            print(f"\nAlready in sync. {len(new)} IP(s) blocked.")
        else:
            print(f"\nNo active IPs to block (VMs powered off or no IPs found).")
        return False

    nsx.set_blocked_ips(sorted(new))
    print()
    for ip in sorted(added):
        print(f"  + blocked {ip}")
    for ip in sorted(removed):
        print(f"  - unblocked {ip} (VM powered off or no IP)")
    print(f"\nSynced. {len(new)} IP(s) now blocked.")
    return True


def action_sync(nsx, vc, args):
    """Sync vCenter tags to gateway firewall — tag VMs in vCenter, run sync to apply."""
    if not vc:
        print("ERROR: vCenter credentials required for sync. Use --vc-password or VC_PASSWORD.", file=sys.stderr)
        sys.exit(1)

    category = args.tag_category
    tag_name = args.tag_name
    loop = getattr(args, "loop", False)
    interval = getattr(args, "loop_interval", 10)
    timeout = getattr(args, "loop_timeout", 600)
    watch = getattr(args, "watch", False)
    watch_interval = getattr(args, "watch_interval", 60)

    if watch:
        print(f"Watching for tag changes every {watch_interval}s (Ctrl+C to stop)\n")
        try:
            while True:
                _do_sync(nsx, vc, category, tag_name, loop=loop, interval=interval, timeout=timeout)
                print(f"\n--- Next check in {watch_interval}s ---")
                time.sleep(watch_interval)
                print()
        except KeyboardInterrupt:
            print("\nWatch stopped.")
    else:
        _do_sync(nsx, vc, category, tag_name, loop=loop, interval=interval, timeout=timeout)


def action_show(nsx, vc, args):
    print(f"=== Blocked VMs (no internet) ===")
    ips = nsx.get_blocked_ips()
    if not ips:
        print("  No VMs blocked")
    else:
        for ip in sorted(ips):
            print(f"  {ip}")

    if vc:
        _, tag_id = vc.find_tag(args.tag_category, args.tag_name)
        if tag_id:
            vm_ids = vc.get_tagged_vms(tag_id)
            if vm_ids:
                print()
                print(f"=== vCenter tagged '{args.tag_category}/{args.tag_name}' ===")
                for vm_id in vm_ids:
                    name, power_state = vc.get_vm_info(vm_id)
                    if power_state != "POWERED_ON":
                        print(f"  {name:40s}  {power_state}")
                        continue
                    vm_ips = vc.get_vm_ips(vm_id)
                    ip_str = ", ".join(vm_ips) if vm_ips else "(no IP)"
                    in_sync = all(ip in ips for ip in vm_ips) if vm_ips else False
                    status = "synced" if in_sync else "NOT synced — run 'sync'"
                    print(f"  {name:40s}  {ip_str:20s}  {status}")

    print()
    pol = nsx.get_or_none(f"/infra/domains/default/gateway-policies/{DEFAULT_POLICY_ID}")
    if pol:
        print(f"=== Gateway Policy: {pol['display_name']} ===")
        for r in pol.get("rules", []):
            status = "enabled" if not r.get("disabled") else "disabled"
            print(f"  {r['display_name']:30s}  action={r['action']:6s}  {status}")
    else:
        print("Gateway policy not found. Run 'setup' first.")


def action_teardown(nsx, vc, args):
    print("=== NSX Gateway Firewall Teardown ===")

    pol = nsx.get_or_none(f"/infra/domains/default/gateway-policies/{DEFAULT_POLICY_ID}")
    if pol:
        nsx.delete(f"/infra/domains/default/gateway-policies/{DEFAULT_POLICY_ID}")
        print(f"  Deleted gateway policy '{DEFAULT_POLICY_ID}'")
    else:
        print(f"  Policy '{DEFAULT_POLICY_ID}' not found, skipping")

    for gid in [DEFAULT_NSX_GROUP, DEFAULT_RFC1918_GROUP]:
        if nsx.get_or_none(f"/infra/domains/default/groups/{gid}"):
            nsx.delete(f"/infra/domains/default/groups/{gid}")
            print(f"  Deleted group '{gid}'")
        else:
            print(f"  Group '{gid}' not found, skipping")

    if args.disable_firewall:
        t1_id = args.t1_id
        if not t1_id:
            t1_id = nsx.find_t1()["id"]
        nsx.patch(f"/infra/tier-1s/{t1_id}", {"disable_firewall": True})
        print(f"  Disabled firewall on T1 {t1_id}")

    print()
    print("Teardown complete.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    top = argparse.ArgumentParser(
        description="Manage NSX T1 Gateway Firewall to block internet access per VM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s setup
  %(prog)s block   --ip 10.28.40.53
  %(prog)s unblock --ip 10.28.40.53
  %(prog)s sync
  %(prog)s show
  %(prog)s teardown
""",
    )
    sub = top.add_subparsers(dest="action", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--nsx-host", default=os.environ.get("NSX_HOST", "nsx.vci.devcluster.openshift.com"))
    common.add_argument("--nsx-user", default=os.environ.get("NSX_USER", "admin"))
    common.add_argument("--nsx-password", default=os.environ.get("NSX_PASSWORD"))
    common.add_argument("--vc-host", default=os.environ.get("VC_HOST", "vc.vci.devcluster.openshift.com"))
    common.add_argument("--vc-user", default=os.environ.get("VC_USER", "administrator@vsphere.local"))
    common.add_argument("--vc-password", default=os.environ.get("VC_PASSWORD"))
    common.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY", "http://127.0.0.1:9999"))
    common.add_argument("--t1-id", default=os.environ.get("NSX_T1_ID"), help="Tier-1 gateway ID (auto-detected if only one)")
    common.add_argument("--tag-category", default=DEFAULT_TAG_CATEGORY, help="vCenter tag category name")
    common.add_argument("--tag-name", default=DEFAULT_TAG_NAME, help="vCenter tag name")

    sub.add_parser("setup", parents=[common], help="Enable T1 firewall and create gateway policy")
    sub.add_parser("show", parents=[common], help="Show blocked VMs and policy status")
    sync_p = sub.add_parser("sync", parents=[common], help="Sync vCenter-tagged VMs to gateway firewall")
    sync_p.add_argument("--loop", action="store_true", help="Retry until all tagged VMs report an IP address")
    sync_p.add_argument("--loop-interval", type=int, default=10, help="Seconds between retries (default: 10)")
    sync_p.add_argument("--loop-timeout", type=int, default=600, help="Max seconds to wait before giving up (default: 600)")
    sync_p.add_argument("--watch", action="store_true", help="Continuously sync on a timer (Ctrl+C to stop)")
    sync_p.add_argument("--watch-interval", type=int, default=60, help="Seconds between watch cycles (default: 60)")

    for name in ("block", "unblock"):
        p = sub.add_parser(name, parents=[common], help=f"{'Block' if name == 'block' else 'Unblock'} a VM IP from internet")
        p.add_argument("--ip", required=True, help="VM IP address")

    td = sub.add_parser("teardown", parents=[common], help="Remove gateway policy and groups")
    td.add_argument("--disable-firewall", action="store_true", help="Also disable T1 gateway firewall")

    return top


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.nsx_password:
        print("ERROR: NSX password required. Use --nsx-password or NSX_PASSWORD env var.", file=sys.stderr)
        sys.exit(1)

    nsx = NSXClient(args.nsx_host, args.nsx_user, args.nsx_password, proxy=args.proxy)

    vc = None
    if args.vc_password:
        try:
            vc = VCenterClient(args.vc_host, args.vc_user, args.vc_password, proxy=args.proxy)
        except requests.exceptions.HTTPError:
            print("WARNING: failed to connect to vCenter, sync/show will be limited", file=sys.stderr)

    actions = {
        "setup": action_setup,
        "block": action_block,
        "unblock": action_unblock,
        "sync": action_sync,
        "show": action_show,
        "teardown": action_teardown,
    }
    actions[args.action](nsx, vc, args)


if __name__ == "__main__":
    main()
