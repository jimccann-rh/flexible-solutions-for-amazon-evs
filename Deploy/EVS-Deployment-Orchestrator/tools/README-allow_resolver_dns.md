# allow_resolver_dns.py

Allow an NSX guest network to send DNS queries to an existing AWS Route 53 Resolver inbound endpoint. The script adds only TCP and UDP port 53 ingress rules to the endpoint's security group; it does not change NSX, routing, or ICMP rules.

## Prerequisites

- Python 3 and AWS CLI configured with credentials for the target account.
- IAM permissions for `route53resolver:GetResolverEndpoint`, `ec2:DescribeSecurityGroups`, and `ec2:AuthorizeSecurityGroupIngress`.
- The resolver endpoint is inbound and operational, and its security group belongs to the supplied VPC.

## Usage

Run a dry run first. Replace the example IDs and CIDR with the values for your environment:

```bash
python3 Deploy/EVS-Deployment-Orchestrator/tools/allow_resolver_dns.py \
  --resolver-endpoint-id rslvr-in-0123456789abcdef0 \
  --security-group-id sg-0123456789abcdef0 \
  --vpc-id vpc-0123456789abcdef0 \
  --source-cidr 10.0.0.0/21 \
  --profile my-profile \
  --region us-east-1
```

The script reports any missing rules without changing AWS. Add `--apply` to create them:

```bash
python3 Deploy/EVS-Deployment-Orchestrator/tools/allow_resolver_dns.py \
  --resolver-endpoint-id rslvr-in-0123456789abcdef0 \
  --security-group-id sg-0123456789abcdef0 \
  --vpc-id vpc-0123456789abcdef0 \
  --source-cidr 10.0.0.0/21 \
  --profile my-profile \
  --region us-east-1 \
  --apply
```

`--profile` defaults to `$AWS_PROFILE`, or `ci`; `--region` defaults to `$AWS_REGION`, or `us-east-1`. The source must be a valid, network-aligned IPv4 CIDR.

## Safety and behavior

- Verifies the resolver endpoint is inbound, operational, attached to the supplied VPC, and associated with the supplied security group.
- Verifies the security group belongs to that VPC before changing it.
- Adds only missing TCP/53 and UDP/53 permissions from the specified CIDR. Existing permissions that already cover the CIDR are left as-is, so reruns are safe.
- Re-reads the security group after `--apply` and verifies both DNS protocols are allowed.
- Does not add ICMP. A failed ping is not a DNS test; verify resolution with `dig` or `nslookup` on an affected VM.
