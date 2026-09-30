#!/usr/bin/env python3
"""
Set up or verify a Route 53 Inbound Resolver Endpoint for private hosted zone
resolution from an external network (e.g. corporate DNS via Transit Gateway).

This creates an inbound resolver endpoint in a VPC subnet so that external DNS
servers can forward queries for private hosted zones (like vci.devcluster.openshift.com)
to the resolver IPs, which then resolve against the Route 53 private zone.

Environment variables (override with CLI flags):
  AWS_PROFILE   AWS CLI profile
  AWS_REGION    AWS region

Usage:
  ./setup-dns-resolver.py --action status
  ./setup-dns-resolver.py --action create --subnet-id subnet-xxx --sg-id sg-xxx
  ./setup-dns-resolver.py --action test --resolver-ip 10.28.32.100 --domain vci.devcluster.openshift.com
  ./setup-dns-resolver.py --action delete --resolver-id rslvr-in-xxx
"""

import argparse
import json
import os
import subprocess
import sys
import time


def aws_cmd(service, *args, profile=None, region=None):
    """Run an AWS CLI command and return parsed JSON."""
    cmd = ["aws", service] + list(args) + ["--output", "json"]
    if profile:
        cmd += ["--profile", profile]
    if region:
        cmd += ["--region", region]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: aws {service} {args[0]} failed:")
        print(f"  {result.stderr.strip()}")
        return None
    return json.loads(result.stdout) if result.stdout.strip() else {}


def cmd_status(profile, region):
    """Show existing resolver endpoints and their IPs."""
    data = aws_cmd("route53resolver", "list-resolver-endpoints",
                   profile=profile, region=region)
    if data is None:
        return

    endpoints = data.get("ResolverEndpoints", [])
    if not endpoints:
        print("No Route 53 Resolver Endpoints found.")
        print("Use --action create to set one up.")
        return

    for ep in endpoints:
        direction = ep["Direction"]
        print(f"\n{'=' * 60}")
        print(f"Endpoint:   {ep['Name']}")
        print(f"ID:         {ep['Id']}")
        print(f"Direction:  {direction}")
        print(f"Status:     {ep['Status']}")
        print(f"VPC:        {ep['HostVPCId']}")
        print(f"SGs:        {', '.join(ep.get('SecurityGroupIds', []))}")
        print(f"Created:    {ep.get('CreationTime', '?')}")

        ips = aws_cmd("route53resolver", "list-resolver-endpoint-ip-addresses",
                      "--resolver-endpoint-id", ep["Id"],
                      profile=profile, region=region)
        if ips:
            print(f"\nResolver IPs:")
            for ip in ips.get("IpAddresses", []):
                print(f"  {ip['Ip']:16s}  Subnet={ip['SubnetId']}  Status={ip['Status']}")

        if direction == "INBOUND":
            resolver_ips = [ip["Ip"] for ip in ips.get("IpAddresses", [])
                           if ip["Status"] == "ATTACHED"]
            if resolver_ips:
                print(f"\n--- DNS Forwarder Config for IT ---")
                print(f"Forward queries for your private hosted zones to:")
                for rip in resolver_ips:
                    print(f"  {rip}")

                zones = aws_cmd("route53", "list-hosted-zones",
                                profile=profile, region=region)
                if zones:
                    private_zones = [z for z in zones.get("HostedZones", [])
                                    if z.get("Config", {}).get("PrivateZone")]
                    if private_zones:
                        print(f"\nPrivate hosted zones that would be resolved:")
                        for z in private_zones:
                            print(f"  {z['Name']}")


def cmd_create(profile, region, subnet_id, sg_id, name, ip1=None, ip2=None, dry_run=False):
    """Create an inbound resolver endpoint."""
    if not subnet_id or not sg_id:
        print("ERROR: --subnet-id and --sg-id are required for create.")
        sys.exit(1)

    existing = aws_cmd("route53resolver", "list-resolver-endpoints",
                       profile=profile, region=region)
    if existing:
        for ep in existing.get("ResolverEndpoints", []):
            if ep["Direction"] == "INBOUND" and ep["Status"] == "OPERATIONAL":
                print(f"An inbound resolver already exists: {ep['Id']} ({ep['Name']})")
                print("Use --action status to see its IPs.")
                print("Use --action delete first if you want to recreate it.")
                return

    ip_addresses = []
    if ip1:
        ip_addresses.append({"SubnetId": subnet_id, "Ip": ip1})
    else:
        ip_addresses.append({"SubnetId": subnet_id})
    if ip2:
        ip_addresses.append({"SubnetId": subnet_id, "Ip": ip2})
    else:
        ip_addresses.append({"SubnetId": subnet_id})

    print(f"Creating Inbound Resolver Endpoint...")
    print(f"  Name:     {name}")
    print(f"  Subnet:   {subnet_id}")
    print(f"  SG:       {sg_id}")
    if ip1:
        print(f"  IP 1:     {ip1}")
    if ip2:
        print(f"  IP 2:     {ip2}")

    if dry_run:
        print("\nDRY RUN: would create resolver endpoint with above settings.")
        return

    result = aws_cmd("route53resolver", "create-resolver-endpoint",
                     "--direction", "INBOUND",
                     "--name", name,
                     "--security-group-ids", sg_id,
                     "--ip-addresses", json.dumps(ip_addresses),
                     "--resolver-endpoint-type", "IPV4",
                     "--protocols", "Do53",
                     profile=profile, region=region)

    if result is None:
        sys.exit(1)

    ep = result.get("ResolverEndpoint", {})
    ep_id = ep.get("Id", "?")
    print(f"\n  Created: {ep_id}")
    print(f"  Status:  {ep.get('Status', '?')}")

    print("\nWaiting for endpoint to become OPERATIONAL...")
    for _ in range(30):
        time.sleep(10)
        check = aws_cmd("route53resolver", "get-resolver-endpoint",
                        "--resolver-endpoint-id", ep_id,
                        profile=profile, region=region)
        if check:
            status = check.get("ResolverEndpoint", {}).get("Status", "?")
            print(f"  Status: {status}")
            if status == "OPERATIONAL":
                break
            if status in ("ACTION_NEEDED", "FAILED"):
                msg = check.get("ResolverEndpoint", {}).get("StatusMessage", "")
                print(f"  Error: {msg}")
                sys.exit(1)

    ips = aws_cmd("route53resolver", "list-resolver-endpoint-ip-addresses",
                  "--resolver-endpoint-id", ep_id,
                  profile=profile, region=region)
    if ips:
        print(f"\nResolver IPs (give these to IT for DNS forwarding):")
        for ip in ips.get("IpAddresses", []):
            print(f"  {ip['Ip']}")

    print("\nDone.")


def cmd_test(resolver_ip, domain):
    """Test DNS resolution against the resolver endpoint."""
    print(f"Testing DNS resolution via {resolver_ip}...")
    print(f"  Domain: {domain}")

    print(f"\n  Pinging {resolver_ip}...")
    ping = subprocess.run(
        ["ping", "-c", "3", "-W", "3", resolver_ip],
        capture_output=True, text=True
    )
    if ping.returncode != 0:
        print(f"  FAILED: {resolver_ip} is not reachable")
        print(f"  {ping.stderr.strip()}" if ping.stderr.strip() else "")
        print(f"\n  Possible causes:")
        print(f"  - No network route to {resolver_ip} from this machine")
        print(f"  - ICMP blocked by security group or firewall")
        print(f"  - Resolver endpoint not operational")
        sys.exit(1)
    print(f"  OK: {resolver_ip} is reachable")

    result = subprocess.run(
        ["dig", f"@{resolver_ip}", domain, "+short", "+timeout=5", "+tries=1"],
        capture_output=True, text=True
    )

    if result.returncode == 0 and result.stdout.strip():
        print(f"\n  Resolved: {result.stdout.strip()}")
        print("  DNS resolution is working!")
    else:
        print(f"\n  dig failed or timed out.")
        print(f"  stderr: {result.stderr.strip()}" if result.stderr.strip() else "")
        print(f"\n  Possible causes:")
        print(f"  - No network route to {resolver_ip} from this machine")
        print(f"  - Security group not allowing UDP/TCP 53 from your IP")
        print(f"  - Resolver endpoint not operational")
        print(f"\n  Try from a machine on the corp network with TGW routing:")
        print(f"    dig @{resolver_ip} {domain}")
        print(f"    nslookup {domain} {resolver_ip}")


def cmd_delete(profile, region, resolver_id, dry_run=False):
    """Delete a resolver endpoint."""
    if not resolver_id:
        print("ERROR: --resolver-id is required for delete.")
        sys.exit(1)

    check = aws_cmd("route53resolver", "get-resolver-endpoint",
                    "--resolver-endpoint-id", resolver_id,
                    profile=profile, region=region)
    if check is None:
        print(f"Resolver endpoint {resolver_id} not found.")
        sys.exit(1)

    ep = check["ResolverEndpoint"]
    print(f"Deleting resolver endpoint:")
    print(f"  ID:   {ep['Id']}")
    print(f"  Name: {ep['Name']}")
    print(f"  IPs:  {ep['IpAddressCount']}")

    if dry_run:
        print("\nDRY RUN: would delete this resolver endpoint.")
        return

    result = aws_cmd("route53resolver", "delete-resolver-endpoint",
                     "--resolver-endpoint-id", resolver_id,
                     profile=profile, region=region)
    if result:
        print("\nDeleting... (takes ~30s)")
        for _ in range(12):
            time.sleep(10)
            chk = aws_cmd("route53resolver", "get-resolver-endpoint",
                          "--resolver-endpoint-id", resolver_id,
                          profile=profile, region=region)
            if chk is None:
                print("  Deleted.")
                break
            status = chk.get("ResolverEndpoint", {}).get("Status", "?")
            print(f"  Status: {status}")
    print("\nDone.")


def main():
    parser = argparse.ArgumentParser(
        description="Set up or verify Route 53 Inbound Resolver for private zone DNS forwarding"
    )
    parser.add_argument("--action", required=True,
                        choices=["status", "create", "test", "delete"],
                        help="Action to perform")
    parser.add_argument("--profile",
                        default=os.environ.get("AWS_PROFILE", "ci"))
    parser.add_argument("--region",
                        default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--subnet-id", default=None,
                        help="Subnet for resolver ENIs (create)")
    parser.add_argument("--sg-id", default=None,
                        help="Security group for resolver (create)")
    parser.add_argument("--name", default="R53InboundResolver",
                        help="Resolver endpoint name (create)")
    parser.add_argument("--ip1", default=None,
                        help="Specific IP for first resolver ENI (create, optional)")
    parser.add_argument("--ip2", default=None,
                        help="Specific IP for second resolver ENI (create, optional)")
    parser.add_argument("--resolver-id", default=None,
                        help="Resolver endpoint ID (delete)")
    parser.add_argument("--resolver-ip", default=None,
                        help="Resolver IP to test against (test)")
    parser.add_argument("--domain", default="vc.vci.devcluster.openshift.com",
                        help="Domain to resolve (test)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without changes")
    args = parser.parse_args()

    if args.action == "status":
        cmd_status(args.profile, args.region)
    elif args.action == "create":
        cmd_create(args.profile, args.region, args.subnet_id, args.sg_id,
                   args.name, args.ip1, args.ip2, args.dry_run)
    elif args.action == "test":
        if not args.resolver_ip:
            print("ERROR: --resolver-ip required for test action.")
            sys.exit(1)
        cmd_test(args.resolver_ip, args.domain)
    elif args.action == "delete":
        cmd_delete(args.profile, args.region, args.resolver_id, args.dry_run)


if __name__ == "__main__":
    main()
