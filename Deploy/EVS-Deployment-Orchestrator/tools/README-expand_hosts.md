# expand_hosts.py

Day-2 ESXi host expansion for an existing AWS EVS environment. Creates
Route53 DNS records (forward A + reverse PTR) and adds hosts via the EVS
`CreateEnvironmentHost` API.

## Prerequisites

- Python 3.9+
- Python packages: `boto3`
- AWS credentials with access to EVS and Route53 APIs
- EVS environment in `CREATED` state
- Sufficient EVS host quota (default: 5 per environment, check AWS Service Quotas)
- Sufficient EC2 on-demand vCPU quota for the instance type (quota `L-1216C47A`)
- Route53 forward and reverse zones (created by Phase 1 Terraform)

```bash
pip install boto3
```

## Config file

Copy `expand_hosts_config.json` and update for your environment:

```json
{
  "environmentId": "env-ksksyki5m0",
  "region": "us-east-1",
  "fqdn": "vci.devcluster.openshift.com",
  "vcfInstallerProductVersion": "9.1.0.0",
  "esxVersion": null,
  "esxiSubnetCidr": "10.28.33.0/25",
  "expansionHosts": [
    {"hostName": "esxi05", "keyName": "vsphere-ci", "instanceType": "i7i.metal-24xl", "hostOffset": 15},
    {"hostName": "esxi06", "keyName": "vsphere-ci", "instanceType": "i7i.metal-24xl", "hostOffset": 16},
    {"hostName": "esxi07", "keyName": "vsphere-ci", "instanceType": "i7i.metal-24xl", "hostOffset": 17},
    {"hostName": "esxi08", "keyName": "vsphere-ci", "instanceType": "i7i.metal-24xl", "hostOffset": 18}
  ]
}
```

### Config fields

| Field | Required | Description |
|---|---|---|
| `environmentId` | yes | EVS environment ID (from Phase 2 config) |
| `region` | yes | AWS region |
| `fqdn` | yes | Domain name for DNS records (matches Phase 1) |
| `vcfInstallerProductVersion` | if `esxVersion` null | VCF version prefix for ESXi auto-resolution |
| `esxVersion` | no | Exact ESXi version string; if null, auto-resolved via EVS API |
| `esxiSubnetCidr` | yes | vmkManagement subnet CIDR (from Phase 2 config) |
| `expansionHosts` | yes | Array of hosts to add |

### Host fields

| Field | Description |
|---|---|
| `hostName` | Short hostname (e.g. `esxi05`) |
| `keyName` | EC2 key pair name |
| `instanceType` | EC2 instance type (e.g. `i7i.metal-24xl`) |
| `hostOffset` | IP offset within the subnet CIDR |

### IP offset convention

IPs are computed as `network_address + hostOffset`, matching Terraform's
`cidrhost()` function from Phase 1:

| Host | Offset | IP (in 10.28.33.0/25) |
|---|---|---|
| esxi01 | 11 | 10.28.33.11 |
| esxi02 | 12 | 10.28.33.12 |
| esxi03 | 13 | 10.28.33.13 |
| esxi04 | 14 | 10.28.33.14 |
| esxi05 | 15 | 10.28.33.15 |
| esxi06 | 16 | 10.28.33.16 |
| esxi07 | 17 | 10.28.33.17 |
| esxi08 | 18 | 10.28.33.18 |

## Usage examples

### Dry run — preview what would happen

```bash
python3 expand_hosts.py --config expand_hosts_config.json --profile ci --dry-run
```

Shows computed IPs, DNS records, and EVS API calls without making any changes.

### Full run — DNS + EVS host creation

```bash
python3 expand_hosts.py --config expand_hosts_config.json --profile ci
```

This will:
1. Create Route53 A records (`esxi05.vci.devcluster.openshift.com` -> `10.28.33.15`, etc.)
2. Create Route53 PTR records (`15.33.28.10.in-addr.arpa` -> `esxi05.vci.devcluster.openshift.com`, etc.)
3. Check EVS host quota and EC2 on-demand vCPU quota
4. Verify the EVS environment is in `CREATED` state
5. Resolve the ESXi version from the EVS API (unless `esxVersion` is set)
6. Call `CreateEnvironmentHost` for each host sequentially
7. Poll host status until all are `CREATED` or `FAILED`

### DNS only — skip EVS host creation

```bash
python3 expand_hosts.py --config expand_hosts_config.json --profile ci --skip-evs
```

Creates Route53 records only. Useful for pre-staging DNS before adding hosts.

### EVS only — skip DNS (records already exist)

```bash
python3 expand_hosts.py --config expand_hosts_config.json --profile ci --skip-dns
```

Adds hosts to EVS only. Use when DNS records are already in place.

### Override region

```bash
python3 expand_hosts.py --config expand_hosts_config.json --profile ci --region us-west-2
```

### Override host quota (after AWS quota increase)

```bash
python3 expand_hosts.py --config expand_hosts_config.json --profile ci --host-quota 8
```

The script queries the **actual AWS Service Quota** (quota code `L-96A49955`)
via the Service Quotas API. If `--host-quota` exceeds the applied AWS quota,
the script warns and uses the AWS value instead.

To automatically request a quota increase:

```bash
python3 expand_hosts.py --config expand_hosts_config.json --profile ci \
    --host-quota 8 --request-quota-increase
```

This submits a `request_service_quota_increase` API call, prints the case ID,
and exits. Re-run without `--request-quota-increase` once the increase is
approved.

### Verbose logging

```bash
python3 expand_hosts.py --config expand_hosts_config.json --profile ci --dry-run -v
```

## CLI reference

| Flag | Default | Description |
|---|---|---|
| `--config` | (required) | Path to expansion config JSON |
| `--profile` | — | AWS CLI profile name |
| `--region` | from config | AWS region override |
| `--dry-run` | off | Show what would happen without making changes |
| `--skip-dns` | off | Skip Route53 record creation |
| `--skip-evs` | off | Skip EVS host creation |
| `--host-quota` | `5` | Max hosts per EVS environment (AWS Service Quota) |
| `--request-quota-increase` | off | Auto-request AWS quota increase if `--host-quota` exceeds current quota |
| `--verbose` / `-v` | off | Enable debug logging |

## Host quota check

Before creating hosts, the script:

1. Queries the **live AWS Service Quota** for EVS host count (`L-96A49955`)
   via the `service-quotas` API
2. Compares `--host-quota` against the actual AWS value — if `--host-quota`
   is higher than the AWS quota, it warns and uses the AWS value
3. Counts existing hosts in the environment and verifies that
   `existing + new` does not exceed the effective quota

If the quota would be exceeded, the script stops:

```
WARNING: Host quota exceeded!
  Current hosts:   4
  Hosts to add:    4
  Total would be:  8
  Account quota:   5

Request a quota increase in the AWS console under
Service Quotas > EVS > 'Host count per EVS environment'.
The --host-quota flag only works if the AWS quota has
already been increased to match.
```

If the AWS quota API is unreachable (e.g. missing IAM permissions), the
script falls back to the `--host-quota` value with a log warning.

### Automatic quota increase

When `--request-quota-increase` is passed alongside `--host-quota`, the
script will automatically call the AWS `request_service_quota_increase` API
if the desired value exceeds the current quota. It also checks for existing
pending requests to avoid duplicates. After submitting, the script prints the
case ID and exits — re-run once the increase is approved.

```bash
# Request increase to 8 hosts and exit
python3 expand_hosts.py --config expand_hosts_config.json --profile ci \
    --host-quota 8 --request-quota-increase

# After approval, run normally
python3 expand_hosts.py --config expand_hosts_config.json --profile ci \
    --host-quota 8
```

## EC2 vCPU quota check

The script also checks the **EC2 on-demand vCPU quota** (`L-1216C47A` —
"Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances") before
creating hosts:

1. Looks up the vCPU count for the instance type (e.g. `i7i.metal-24xl` = 96 vCPUs)
2. Calculates total vCPUs needed: `(existing + new hosts) * vCPUs per host`
3. Compares against the EC2 on-demand vCPU quota

If the vCPUs needed exceed the quota, the script stops with a warning.
Use `--request-quota-increase` to automatically request both EVS host and
EC2 vCPU quota increases.

```
EC2 on-demand vCPU check (L-1216C47A):
  Instance type:   i7i.metal-24xl (96 vCPUs)
  EVS hosts:       4 existing + 4 new = 8
  vCPUs needed:    768 (EVS hosts only)
  vCPU quota:      384

WARNING: EVS hosts alone require 768 vCPUs but the on-demand quota is 384.
```

## Host creation status polling

After submitting host creation requests, the script polls each host's state
every 60 seconds (up to 60 minutes) until all reach a terminal state:

- **CREATED** — host is ready
- **CREATE_FAILED** — host creation failed (script warns and exits with error)
- **CREATING** — still provisioning (continues polling)

```
Polling host creation status...
  esxi05       CREATED  (120s)
  esxi06       CREATED  (150s)
  WARNING: esxi07       CREATE_FAILED  (90s)
           Reason: Operation failed due to exceeding your allocated EC2
           On-Demand Instance vCPU limit.

  Summary: 2 created, 1 failed, 0 still creating
```

## Failed host detection and vCPU remediation

When existing hosts are in `CREATE_FAILED` state, the script reports
the failure reason from the EVS API (`stateDetails`). For vCPU quota
failures, the script automatically:

1. Checks the current EC2 vCPU quota to see if it's now sufficient
2. If still insufficient — attempts to request a quota increase via the
   Service Quotas API
3. If the IAM user lacks `servicequotas:RequestServiceQuotaIncrease`
   permissions, prints step-by-step manual instructions:

```
  To resolve via the AWS Console:

  Step 1 — Increase EC2 vCPU quota:
    1. Open the AWS Console
    2. Go to: Service Quotas > Amazon Elastic Compute Cloud (Amazon EC2)
    3. Search for: 'Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances'
    4. Click 'Request increase at account level'
    5. Enter desired vCPU value: 1152 or higher
    6. Submit and wait for approval (usually minutes for small increases)

  Step 2 — Delete the failed host(s):
    1. Go to: Amazon EVS > Environments > env-ksksyki5m0
    2. Select the Hosts tab
    3. Select: esxi07
    4. Actions > Delete host

  Step 3 — Re-run this script
```

If the quota has already been increased since the failure, the script
reports that and tells you to just delete the failed host and re-run:

```
  EC2 vCPU quota is now sufficient (1152/1152).
  The failed host(s) were likely created before a recent quota increase.

  To retry — delete the failed host(s) and re-run:
    AWS Console > Amazon EVS > Environments > env-ksksyki5m0 > Hosts
    Select: esxi07 > Actions > Delete host
```

### Required IAM permissions for auto-increase

To use the automatic quota increase feature, the IAM user/role needs:

```json
{
  "Effect": "Allow",
  "Action": [
    "servicequotas:GetServiceQuota",
    "servicequotas:GetAWSDefaultServiceQuota",
    "servicequotas:RequestServiceQuotaIncrease",
    "servicequotas:ListRequestedServiceQuotaChangeHistoryByQuota"
  ],
  "Resource": "*"
}
```

## DNS records created

For each host, two records are created using UPSERT (idempotent):

| Type | Name | Value | TTL |
|---|---|---|---|
| A | `esxi05.vci.devcluster.openshift.com` | `10.28.33.15` | 300 |
| PTR | `15.33.28.10.in-addr.arpa` | `esxi05.vci.devcluster.openshift.com` | 300 |

The forward zone is located by matching the FQDN. The reverse zone follows
Phase 1 Terraform's convention: `{octet1}.{octet0}.in-addr.arpa` (e.g.
`28.10.in-addr.arpa`).

## ESXi version resolution

If `esxVersion` is null in the config, the script auto-resolves it:

1. Calls `evs.get_versions()` to list available ESXi versions
2. Filters by instance type
3. Matches versions starting with `ESXi-{major}.{minor}.`
4. Selects the latest build number

Set `esxVersion` explicitly to skip the API call and use a specific version.

## Idempotency

- DNS records use UPSERT — safe to re-run
- EVS host creation checks for existing hosts by hostname before creating.
  Hosts that already exist in the environment are skipped automatically,
  so the script is safe to re-run.
