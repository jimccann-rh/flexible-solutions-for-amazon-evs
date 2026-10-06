# commission_hosts_vcf9.py

Commission expansion ESXi hosts using the VCF 9.1 recommended workflow.
Authenticates with vCenter SSO credentials (`administrator@vsphere.local`)
instead of SDDC Manager local credentials — the same auth path the vCenter
VCF plugin uses when commissioning hosts through Global Inventory Lists.

After commissioning, verifies hosts are visible in the Management vCenter.

## VCF 9.1 Workflow (Recommended)

This script follows the primary VCF 9.1 workflow:

1. **Host Commissioning** — Commission via SDDC Manager API using vCenter
   SSO authentication (same as vCenter UI: Global Inventory Lists > Hosts >
   Unassigned Hosts > Commission Hosts)
2. **Domain Deployment** — Done separately via VCF Operations console
   (Inventory > VCF Instances > Add Workload Domain)

The legacy workflow (`commission_hosts.py`) uses SDDC Manager local auth
(`admin@local`) and is being deprecated by Broadcom.

## Prerequisites

- Python 3.9+
- Python packages: `boto3`, `requests`
- AWS credentials with access to Secrets Manager
- SOCKS/HTTPS proxy to VCF management network (default: `http://127.0.0.1:9999`)
- `expand_hosts.py` must have already run (hosts exist in EVS and DNS)
- ESXi hosts must be reachable on port 443 (for SSL thumbprint fetch)

```bash
pip install boto3 requests
```

## Input files

### expand_hosts_config.json

Same config used by `expand_hosts.py`:

```json
{
  "environmentId": "env-ksksyki5m0",
  "region": "us-east-1",
  "fqdn": "vci.devcluster.openshift.com",
  "esxiSubnetCidr": "10.28.33.0/25",
  "expansionHosts": [
    {"hostName": "esxi05", "hostOffset": 15, ...},
    {"hostName": "esxi06", "hostOffset": 16, ...},
    {"hostName": "esxi07", "hostOffset": 17, ...},
    {"hostName": "esxi08", "hostOffset": 18, ...}
  ]
}
```

## Usage examples

### Dry run

```bash
python3 commission_hosts_vcf9.py \
    --config expand_hosts_config.json \
    --profile ci --dry-run
```

### Full run

```bash
python3 commission_hosts_vcf9.py \
    --config expand_hosts_config.json \
    --profile ci
```

This will:
1. Fetch vCenter SSO credentials from AWS Secrets Manager (`vcenterSso`)
2. Authenticate to Management vCenter (`vc.{fqdn}`)
3. Authenticate to SDDC Manager (`sddcm.{fqdn}`) with SSO credentials
4. Check which hosts are already commissioned (skip duplicates)
5. Fetch each host's root password and SSL thumbprint
6. Look up the network pool ID
7. Validate the host specs
8. Commission the hosts
9. Verify hosts appear in Management vCenter inventory

### Skip validation

```bash
python3 commission_hosts_vcf9.py \
    --config expand_hosts_config.json \
    --profile ci --skip-validation
```

### Without proxy (direct access)

```bash
python3 commission_hosts_vcf9.py \
    --config expand_hosts_config.json \
    --profile ci --proxy ""
```

## CLI reference

| Flag | Default | Description |
|---|---|---|
| `--config` | (required) | Path to `expand_hosts_config.json` |
| `--profile` | — | AWS CLI profile name |
| `--region` | from config | AWS region override |
| `--vcenter-host` | `vc.{fqdn}` | Management vCenter FQDN |
| `--sddcm-host` | `sddcm.{fqdn}` | SDDC Manager FQDN |
| `--proxy` | `http://127.0.0.1:9999` | HTTPS proxy |
| `--network-pool-name` | `env-ksksyki5m0-cl01_pool` | Network pool name |
| `--storage-type` | `VSAN_ESA` | Storage type |
| `--skip-validation` | off | Skip host validation |
| `--skip-vcenter-check` | off | Skip post-commission vCenter verification |
| `--dry-run` | off | Preview without making changes |
| `--verbose` / `-v` | off | Enable debug logging |

## Credential sources

All credentials are fetched from AWS Secrets Manager:

| Credential | Secret ID | Username |
|---|---|---|
| vCenter SSO | `evs-{envId}_vcenterSso` | `administrator@vsphere.local` |
| ESXi root | `evs!{envId}_{hostName}` | `root` |
| SSL thumbprint | Live TLS connection to host:443 | — |

## Key differences from legacy script

| | `commission_hosts.py` (legacy) | `commission_hosts_vcf9.py` (VCF 9.1) |
|---|---|---|
| **Auth credentials** | `sddcManagerLocal` (`admin@local`) | `vcenterSso` (`administrator@vsphere.local`) |
| **Auth target** | SDDC Manager only | vCenter + SDDC Manager |
| **Post-commission check** | None | Verifies hosts in vCenter |
| **Recommended by** | Legacy (SDDC Manager UI) | Primary (vCenter + VCF Operations) |

## Workflow order

1. **`expand_hosts.py`** — Create DNS records + add hosts to EVS
2. **`create_vcenter_dns.py`** — Create DNS for new workload domain vCenter
3. **`commission_hosts_vcf9.py`** — Commission hosts (VCF 9.1 workflow)
4. Deploy workload domain via VCF Operations console

## Idempotency

- Checks `GET /v1/hosts` before commissioning and skips hosts that
  already exist. Safe to re-run.
