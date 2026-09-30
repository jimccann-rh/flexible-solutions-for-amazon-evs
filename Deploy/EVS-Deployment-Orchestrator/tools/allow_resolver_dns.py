#!/usr/bin/env python3
"""Allow an NSX guest CIDR to query an associated Route 53 Resolver endpoint."""

import argparse
import ipaddress
import json
import os
import subprocess
import sys


def aws_json(profile, region, service, *args):
    result = subprocess.run(
        ["aws", service, *args, "--profile", profile, "--region", region,
         "--output", "json", "--no-cli-pager"],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"aws {service} failed")
    return json.loads(result.stdout)


def allows_source(permissions, protocol, source):
    protocol_number = {"tcp": "6", "udp": "17"}[protocol]
    for permission in permissions:
        rule_protocol = permission["IpProtocol"]
        if rule_protocol not in ("-1", protocol, protocol_number):
            continue
        if rule_protocol != "-1" and not (
            permission.get("FromPort", 54) <= 53 <= permission.get("ToPort", 52)
        ):
            continue
        for ip_range in permission.get("IpRanges", []):
            try:
                if source.subnet_of(ipaddress.ip_network(ip_range["CidrIp"])):
                    return True
            except ValueError:
                continue
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolver-endpoint-id", required=True)
    parser.add_argument("--security-group-id", required=True)
    parser.add_argument("--vpc-id", required=True)
    parser.add_argument("--source-cidr", required=True, help="NSX guest IPv4 CIDR")
    parser.add_argument("--profile", default=os.getenv("AWS_PROFILE", "ci"))
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "us-east-1"))
    parser.add_argument("--apply", action="store_true", help="Apply missing rules; otherwise only report")
    args = parser.parse_args()

    source = ipaddress.ip_network(args.source_cidr, strict=True)
    if source.version != 4:
        parser.error("--source-cidr must be IPv4")

    endpoint = aws_json(
        args.profile, args.region, "route53resolver", "get-resolver-endpoint",
        "--resolver-endpoint-id", args.resolver_endpoint_id,
    )["ResolverEndpoint"]
    if (endpoint["Status"] != "OPERATIONAL"
            or endpoint["Direction"] != "INBOUND"
            or endpoint["HostVPCId"] != args.vpc_id
            or args.security_group_id not in endpoint["SecurityGroupIds"]):
        raise RuntimeError("Resolver endpoint status, direction, VPC, or security group does not match")

    group = aws_json(
        args.profile, args.region, "ec2", "describe-security-groups",
        "--group-ids", args.security_group_id,
    )["SecurityGroups"][0]
    if group["VpcId"] != args.vpc_id:
        raise RuntimeError("Security group is not in the expected VPC")

    missing = [p for p in ("tcp", "udp")
               if not allows_source(group["IpPermissions"], p, source)]
    for protocol in missing:
        action = "Would add" if not args.apply else "Adding"
        print(f"{action} {protocol}/53 from {source} to {args.security_group_id}")
        if args.apply:
            aws_json(
                args.profile, args.region, "ec2", "authorize-security-group-ingress",
                "--group-id", args.security_group_id,
                "--protocol", protocol,
                "--port", "53",
                "--cidr", str(source),
            )

    if not args.apply:
        print("Dry run only; pass --apply to add the missing rules.")
        return

    updated = aws_json(
        args.profile, args.region, "ec2", "describe-security-groups",
        "--group-ids", args.security_group_id,
    )["SecurityGroups"][0]
    if any(not allows_source(updated["IpPermissions"], p, source)
           for p in ("tcp", "udp")):
        raise RuntimeError("AWS did not report both DNS ingress rules after applying")
    print("Verified TCP and UDP port 53 ingress.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
