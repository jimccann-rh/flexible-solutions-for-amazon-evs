# setup-dns-resolver.py

Sets up or verifies an AWS Route 53 Inbound Resolver Endpoint so that an
external network (e.g. Red Hat corporate) can resolve private hosted zone
DNS records via a Transit Gateway.

## Problem

Private hosted zones in Route 53 (like `vci.devcluster.openshift.com`) are
only resolvable from within the associated VPC. Machines on the corporate
network can reach VPC IPs via the Transit Gateway, but DNS resolution fails
because the corp DNS servers don't know about the private zone.

## Solution

A Route 53 Inbound Resolver Endpoint creates ENIs in the VPC that accept DNS
queries. Corporate DNS servers are configured with a **conditional forwarder**
to send queries for the private zone to the resolver IPs. The flow:

```
Laptop -> Corp DNS -> (conditional forward) -> TGW -> Resolver ENI -> Private Zone
```

## Prerequisites

- AWS CLI configured with a profile that has Route 53 Resolver permissions
- VPC subnet with available IPs for the resolver ENIs
- Security group allowing inbound UDP/TCP port 53
- Transit Gateway routing between corp network and the VPC subnet

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_PROFILE` | `ci` | AWS CLI profile |
| `AWS_REGION` | `us-east-1` | AWS region |

## Actions

### status — Show existing resolver endpoints

```bash
./setup-dns-resolver.py --action status

# With a different profile/region
AWS_PROFILE=prod AWS_REGION=eu-west-1 ./setup-dns-resolver.py --action status
```

Shows all resolver endpoints, their IPs, status, and the DNS forwarder
configuration that IT needs.

### create — Create an inbound resolver endpoint

```bash
# Dry run first
./setup-dns-resolver.py --action create \
  --subnet-id subnet-0c2d2effdde444c51 \
  --sg-id sg-0a3ce9f3debeabeba \
  --dry-run

# Create with auto-assigned IPs
./setup-dns-resolver.py --action create \
  --subnet-id subnet-0c2d2effdde444c51 \
  --sg-id sg-0a3ce9f3debeabeba

# Create with specific IPs
./setup-dns-resolver.py --action create \
  --subnet-id subnet-0c2d2effdde444c51 \
  --sg-id sg-0a3ce9f3debeabeba \
  --ip1 10.28.32.100 \
  --ip2 10.28.32.101

# Custom name
./setup-dns-resolver.py --action create \
  --subnet-id subnet-0c2d2effdde444c51 \
  --sg-id sg-0a3ce9f3debeabeba \
  --name "MyResolver"
```

### test — Test DNS resolution via the resolver

```bash
# Test default domain (vc.vci.devcluster.openshift.com)
./setup-dns-resolver.py --action test --resolver-ip 10.28.32.100

# Test a specific domain
./setup-dns-resolver.py --action test --resolver-ip 10.28.32.100 \
  --domain nsx.vci.devcluster.openshift.com
```

**Note:** This must be run from a machine that has network routing to the
resolver IP (e.g. a machine on the corp network with TGW connectivity).
You can also test manually:

```bash
dig @10.28.32.100 vc.vci.devcluster.openshift.com
nslookup vc.vci.devcluster.openshift.com 10.28.32.100
```

### delete — Remove a resolver endpoint

```bash
# Dry run
./setup-dns-resolver.py --action delete \
  --resolver-id rslvr-in-2d937fddb8fa4dc98 --dry-run

# Delete
./setup-dns-resolver.py --action delete \
  --resolver-id rslvr-in-2d937fddb8fa4dc98
```

## CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--action` | (required) | `status`, `create`, `test`, or `delete` |
| `--profile` | `$AWS_PROFILE` or `ci` | AWS CLI profile |
| `--region` | `$AWS_REGION` or `us-east-1` | AWS region |
| `--subnet-id` | — | Subnet for resolver ENIs (create) |
| `--sg-id` | — | Security group (create) |
| `--name` | `R53InboundResolver` | Resolver name (create) |
| `--ip1` | auto | First resolver ENI IP (create) |
| `--ip2` | auto | Second resolver ENI IP (create) |
| `--resolver-id` | — | Endpoint ID (delete) |
| `--resolver-ip` | — | IP to query (test) |
| `--domain` | `vc.vci.devcluster.openshift.com` | Domain to resolve (test) |
| `--dry-run` | off | Preview changes |

## Current Setup

The EVS environment already has an operational resolver:

| Setting | Value |
|---------|-------|
| Endpoint ID | `rslvr-in-2d937fddb8fa4dc98` |
| Name | `R53InboundRslvr` |
| Resolver IP 1 | `10.28.32.100` |
| Resolver IP 2 | `10.28.32.101` |
| Subnet | `subnet-0c2d2effdde444c51` (EVS-Service-Access-Subnet) |
| Security Group | `sg-0a3ce9f3debeabeba` (EVS-Service-Access-SG) |
| VPC | `vpc-0f13b1e8feec1cefb` |

## What to tell Red Hat IT

Ask IT to add a **conditional DNS forwarder** on the corporate DNS servers:

> Forward all DNS queries for `vci.devcluster.openshift.com` to:
> - **10.28.32.100**
> - **10.28.32.101**
>
> These are Route 53 Inbound Resolver endpoints in the EVS VPC, reachable
> via the Transit Gateway attachment `tgw-attach-0458cc6cbf5211a50`.

Once configured, `dig vc.vci.devcluster.openshift.com` from any machine on
the corp network will resolve to `10.28.33.138` (vCenter).
