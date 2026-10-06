# commission_hosts.py

Commission expansion ESXi hosts in VCF Operations Manager (SDDC Manager).
After `expand_hosts.py` adds new hosts at the AWS/EVS layer, this script
registers them with VCF so they can be assigned to clusters.

## Prerequisites

- Python 3.9+
- Python packages: `boto3`, `requests`
- AWS credentials with access to Secrets Manager
- SOCKS/HTTPS proxy to VCF Operations Manager (default: `http://127.0.0.1:9999`)
- `expand_hosts.py` must have already run (hosts exist in EVS and DNS)
- ESXi hosts must be reachable on port 443 (for SSL thumbprint fetch)

```bash
pip install boto3 requests
```

## Input files

### expand_hosts_config.json

The same config used by `expand_hosts.py`. Provides environment ID, FQDN,
and the list of expansion hosts:

```json
{
  "environmentId": "env-ksksyki5m0",
  "region": "us-east-1",
  "fqdn": "vci.devcluster.openshift.com",
  "esxiSubnetCidr": "10.28.33.0/25",
  "expansionHosts": [
    {"hostName": "esxi05", "keyName": "vsphere-ci", "instanceType": "i7i.metal-24xl", "hostOffset": 15},
    {"hostName": "esxi06", "keyName": "vsphere-ci", "instanceType": "i7i.metal-24xl", "hostOffset": 16},
    {"hostName": "esxi07", "keyName": "vsphere-ci", "instanceType": "i7i.metal-24xl", "hostOffset": 17},
    {"hostName": "esxi08", "keyName": "vsphere-ci", "instanceType": "i7i.metal-24xl", "hostOffset": 18}
  ]
}
```

## Usage examples

### Dry run — preview what would happen

```bash
python3 commission_hosts.py \
    --config expand_hosts_config.json \
    --profile ci --dry-run
```

Shows which hosts would be commissioned, credentials gathered, and the
API payload — without making any changes.

### Full run — validate and commission

```bash
python3 commission_hosts.py \
    --config expand_hosts_config.json \
    --profile ci
```

This will:
1. Fetch VCF Operations admin password from AWS Secrets Manager
2. Authenticate to `vcfops01.vci.devcluster.openshift.com` via the proxy
3. Check which hosts are already commissioned (skip duplicates)
4. Fetch each host's root password from AWS Secrets Manager
5. Fetch each host's SSL thumbprint via TLS connection
6. Look up the network pool ID from the VCF Operations API
7. Validate the host specs (`POST /v1/hosts/validations`)
8. Commission the hosts (`POST /v1/hosts`)
9. Poll the task until completion

### Skip validation

```bash
python3 commission_hosts.py \
    --config expand_hosts_config.json \
    --profile ci --skip-validation
```

### Custom network pool

```bash
python3 commission_hosts.py \
    --config expand_hosts_config.json \
    --profile ci \
    --network-pool-name my-custom-pool
```

### Override proxy

```bash
python3 commission_hosts.py \
    --config expand_hosts_config.json \
    --profile ci \
    --proxy http://127.0.0.1:8080
```

### Verbose logging

```bash
python3 commission_hosts.py \
    --config expand_hosts_config.json \
    --profile ci -v
```

## CLI reference

| Flag | Default | Description |
|---|---|---|
| `--config` | (required) | Path to `expand_hosts_config.json` |
| `--profile` | — | AWS CLI profile name |
| `--region` | from config | AWS region override |
| `--proxy` | `http://127.0.0.1:9999` | HTTPS proxy for VCF Operations API |
| `--network-pool-name` | `env-ksksyki5m0-cl01_pool` | Network pool name |
| `--storage-type` | `VSAN_ESA` | Storage type for host commissioning |
| `--skip-validation` | off | Skip host validation before commissioning |
| `--dry-run` | off | Show what would happen without changes |
| `--verbose` / `-v` | off | Enable debug logging |

## Credential sources

All credentials are fetched from AWS Secrets Manager:

| Credential | Secret pattern | Username |
|---|---|---|
| VCF Ops admin | `evs-{environmentId}_operationsAdmin` | `admin` |
| ESXi root | `evs!{environmentId}_{hostName}` | `root` |
| SSL thumbprint | Live TLS connection to host:443 | — |

## Commissioning API payload

Each host is submitted with:

```json
{
  "fqdn": "esxi05.vci.devcluster.openshift.com",
  "username": "root",
  "password": "<from Secrets Manager>",
  "storageType": "VSAN_ESA",
  "networkPoolId": "<looked up from API>",
  "sslThumbprint": "96:9D:9F:EE:75:6C:..."
}
```

## Workflow order

Run these scripts in sequence for a full host expansion:

1. **`expand_hosts.py`** — Create DNS records + add hosts to EVS
2. **`commission_hosts.py`** — Commission hosts in VCF Operations Manager
3. (Next) Add hosts to a VCF cluster via the VCF Operations console

## Idempotency

- The script checks `GET /v1/hosts` before commissioning and skips any
  host whose FQDN already appears. Safe to re-run.
