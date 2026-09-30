# nsx_manager.py

NSX-T Segment, DHCP, and SNAT management CLI for AWS EVS environments.
Creates overlay segments with inline DHCP configuration, SNAT/NO_SNAT NAT
rules, and DHCP server configs — individually or in batch from a supernet
CIDR.

## Prerequisites

- Python 3.9+
- Python packages: `boto3`, `requests`
- AWS credentials with access to Secrets Manager
- SOCKS/HTTPS proxy to NSX Manager (default: `http://127.0.0.1:9999`)

```bash
pip install boto3 requests
```

## Connection resolution

Connection defaults are resolved in order:

1. CLI flags (`--nsx-host`, `--nsx-password`, etc.)
2. Environment variables (`NSX_HOST`, `NSX_USER`, `NSX_PASSWORD`, `HTTPS_PROXY`)
3. Config files (`--config` for `config.json`, `--edge-spec` for `edge_cluster_spec.json`)
4. AWS Secrets Manager (password only, via `evs-{environmentId}_nsxAdmin`)

## Resources managed

| Resource | NSX API path | Description |
|---|---|---|
| DHCP server config | `/infra/dhcp-server-configs/{id}` | Shared DHCP server for segments |
| Segment | `/infra/segments/{id}` | Routed overlay segment with inline DHCP |
| SNAT rule | `/infra/tier-1s/{t1}/nat/USER/nat-rules/{id}` | Outbound NAT for overlay traffic |
| NO_SNAT rule | `/infra/tier-1s/{t1}/nat/USER/nat-rules/{id}` | East-west bypass (skip NAT) |
| BGP route aggregation | `/infra/tier-0s/{t0}/locale-services/default/bgp` | Advertise overlay CIDR via BGP on Tier-0 |

## Subcommands

### Individual resources

| Subcommand | Actions | Description |
|---|---|---|
| `dhcp` | `create`, `show`, `delete` | Manage a DHCP server config |
| `segment` | `create`, `show`, `delete` | Manage an overlay segment |
| `snat` | `create`, `show`, `delete` | Manage SNAT NAT rules |
| `nosnat` | `create`, `show`, `delete` | Manage NO_SNAT bypass rules |
| `all` | `create`, `show`, `delete` | DHCP + segment + SNAT + NO_SNAT together |
| `batch` | `create`, `show`, `delete` | Multiple segments from a supernet CIDR |

## Usage examples

### Batch create — 16 segments from a /21 overlay (most common)

Subdivides the overlay CIDR into /25 subnets, creates a shared DHCP server
config, one segment per subnet, and NAT rules:

```bash
python3 nsx_manager.py batch create \
    --nsxcidroverlay 10.28.40.0/21 \
    --segmentscidr 25 \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

This creates:
- 1 DHCP server config (`DHCP_Server_1`)
- 16 segments (`logical_network_segment_1` through `logical_network_segment_16`)
- 1 SNAT rule + 1 NO_SNAT rule (full-stack mode, the default)
- 1 BGP route aggregation entry for `10.28.40.0/21` on the Tier-0 (summary_only=false)

### Batch create without NAT rules

Creates only the DHCP server config, segments, and BGP route aggregation —
no SNAT or NO_SNAT rules:

```bash
python3 nsx_manager.py batch create \
    --nsxcidroverlay 10.28.40.0/21 \
    --segmentscidr 25 \
    --snat-mode none \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Batch create without BGP route aggregation

Skip the BGP route aggregation step:

```bash
python3 nsx_manager.py batch create \
    --nsxcidroverlay 10.28.40.0/21 \
    --segmentscidr 25 \
    --no-bgp-aggregation \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Batch create with per-segment NAT

Creates individual SNAT/NO_SNAT rules for each segment instead of one
shared pair:

```bash
python3 nsx_manager.py batch create \
    --nsxcidroverlay 10.28.40.0/21 \
    --segmentscidr 25 \
    --snat-mode per-segment \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Batch show — list all resources

```bash
python3 nsx_manager.py batch show \
    --nsxcidroverlay 10.28.40.0/21 \
    --segmentscidr 25 \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Batch delete — tear down all segments and NAT

```bash
python3 nsx_manager.py batch delete \
    --nsxcidroverlay 10.28.40.0/21 \
    --segmentscidr 25 \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Create a single DHCP server config

```bash
python3 nsx_manager.py dhcp create \
    --dhcp-id DHCP_Server_1 \
    --dhcp-name "DHCP Server 1" \
    --dhcp-listen 100.96.0.1/30 \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### List all DHCP server configs

```bash
python3 nsx_manager.py dhcp show \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Show a specific DHCP config

```bash
python3 nsx_manager.py dhcp show \
    --dhcp-id DHCP_Server_1 \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Create a single segment

```bash
python3 nsx_manager.py segment create \
    --segment-id logical_network_segment_1 \
    --segment-name "logical network segment 1" \
    --gateway 10.28.40.1/25 \
    --network 10.28.40.0/25 \
    --dhcp-range "10.28.40.50-10.28.40.120" \
    --dhcp-server-addr 10.28.40.2/25 \
    --dns 10.28.32.100 \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### List all segments

```bash
python3 nsx_manager.py segment show \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Show a specific segment

```bash
python3 nsx_manager.py segment show \
    --segment-id logical_network_segment_1 \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Delete a segment

```bash
python3 nsx_manager.py segment delete \
    --segment-id logical_network_segment_1 \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Create a SNAT rule

```bash
python3 nsx_manager.py snat create \
    --snat-id snat-overlay-to-internet \
    --snat-name "SNAT-overlay-to-internet" \
    --source 10.28.40.0/21 \
    --translated 10.28.41.10 \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### List all NAT rules

```bash
python3 nsx_manager.py snat show \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Create a NO_SNAT rule (east-west bypass)

```bash
python3 nsx_manager.py nosnat create \
    --nosnat-id no-snat-east-west \
    --nosnat-name "NO-SNAT-east-west" \
    --nosnat-source 10.28.40.0/21 \
    --nosnat-dest 10.0.0.0/8 \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Create all resources at once (DHCP + segment + NAT)

```bash
python3 nsx_manager.py all create \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Delete all resources at once

Deletes in the correct order (SNAT -> NO_SNAT -> segment -> DHCP):

```bash
python3 nsx_manager.py all delete \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Override NSX connection

```bash
python3 nsx_manager.py segment show \
    --nsx-host nsx.example.com \
    --nsx-user admin \
    --nsx-password 'MyPassword!' \
    --proxy http://127.0.0.1:9999 \
    --tier1-id my-tier1 \
    --transport-zone my-overlay-tz \
    --edge-cluster my-edge-cluster
```

## Batch subnet computation

The `batch` subcommand subdivides an overlay CIDR into equal subnets. Each
subnet gets a segment with:

| Field | Offset | Example (first /25 in 10.28.40.0/21) |
|---|---|---|
| Network | — | `10.28.40.0/25` |
| Gateway | +1 | `10.28.40.1/25` |
| DHCP server | +2 | `10.28.40.2/25` |
| DHCP range start | +50 | `10.28.40.50` |
| DHCP range end | +120 | `10.28.40.120` |

Example: `10.28.40.0/21` with `/25` prefix produces 16 segments:

| Segment | Network | Gateway |
|---|---|---|
| logical network segment 1 | 10.28.40.0/25 | 10.28.40.1/25 |
| logical network segment 2 | 10.28.40.128/25 | 10.28.40.129/25 |
| ... | ... | ... |
| logical network segment 16 | 10.28.47.128/25 | 10.28.47.129/25 |

## SNAT modes

| Mode | Flag | What it creates |
|---|---|---|
| `full-stack` (default) | `--snat-mode full-stack` | 1 SNAT + 1 NO_SNAT for the entire overlay CIDR |
| `per-segment` | `--snat-mode per-segment` | 1 SNAT + 1 NO_SNAT per segment subnet |
| `none` | `--snat-mode none` | No NAT rules — segments only |

## BGP route aggregation

When running `batch create`, the script adds the overlay CIDR (e.g.
`10.28.40.0/21`) as a BGP route aggregation entry on the Tier-0 gateway.
This advertises the aggregate prefix via BGP instead of individual segment
routes.

- **Enabled by default** — use `--no-bgp-aggregation` to skip
- **summary_only** is set to `false` (individual routes are still advertised
  alongside the aggregate)
- **Tier-0 ID** is auto-resolved from `--edge-spec` (`tier0.name` field),
  or can be set with `--tier0-id`
- **Idempotent** — skips if the prefix already exists on create, skips if
  not found on delete
- `batch show` displays the current route aggregation entries
- `batch delete` removes the aggregation entry before deleting segments

## CLI reference — common connection flags

| Flag | Default | Description |
|---|---|---|
| `--nsx-host` | from config/env | NSX Manager FQDN |
| `--nsx-user` | `admin` | NSX username |
| `--nsx-password` | Secrets Mgr | NSX password |
| `--proxy` | `http://127.0.0.1:9999` | HTTPS proxy for NSX |
| `--tier1-id` | from edge-spec | Tier-1 gateway ID |
| `--transport-zone` | from edge-spec | Transport zone path |
| `--edge-cluster` | from edge-spec | Edge cluster path |
| `--config` | -- | Path to `config.json` |
| `--edge-spec` | -- | Path to `edge_cluster_spec.json` |
| `--profile` | -- | AWS CLI profile name |
| `--region` | from config | AWS region override |

## CLI reference — batch flags

| Flag | Default | Description |
|---|---|---|
| `--nsxcidroverlay` | `10.28.40.0/21` | Overlay supernet to subdivide |
| `--segmentscidr` | `25` | Prefix length for each subnet |
| `--snat-mode` | `full-stack` | `full-stack`, `per-segment`, or `none` |
| `--snat-translated` | `10.28.41.10` | Translated (outbound NAT) IP |
| `--dns` | from edge-spec | DNS server IP |
| `--nosnat-source` | overlay CIDR | NO_SNAT source CIDR |
| `--nosnat-dest` | `10.0.0.0/8` | NO_SNAT destination CIDR |
| `--tier0-id` | from edge-spec | Tier-0 gateway ID for BGP aggregation |
| `--bgp-aggregation` | enabled | Add BGP route aggregation for overlay CIDR |
| `--no-bgp-aggregation` | -- | Skip BGP route aggregation |

## CLI reference — DHCP flags

| Flag | Default | Description |
|---|---|---|
| `--dhcp-id` | `DHCP_Server_1` | DHCP config object ID |
| `--dhcp-name` | `DHCP Server 1` | DHCP display name |
| `--dhcp-listen` | `100.96.0.1/30` | DHCP server listen address |
| `--lease-time` | `86400` | DHCP lease time in seconds |

## CLI reference — segment flags

| Flag | Default | Description |
|---|---|---|
| `--segment-id` | `logical_network_segment_1` | Segment object ID |
| `--segment-name` | `logical network segment 1` | Segment display name |
| `--gateway` | `10.28.40.1/25` | Gateway IP/prefix |
| `--network` | `10.28.40.0/25` | Segment network CIDR |
| `--dhcp-range` | `10.28.40.50-10.28.40.120` | DHCP range |
| `--dhcp-server-addr` | `10.28.40.2/25` | In-segment DHCP server IP |
| `--dns` | `10.28.32.100` | DNS server IP |

## CLI reference — NAT flags

| Flag | Default | Description |
|---|---|---|
| `--snat-id` | `snat-overlay-to-internet` | SNAT rule ID |
| `--snat-name` | `SNAT-overlay-to-internet` | SNAT display name |
| `--source` | `10.28.40.0/21` | Source network CIDR |
| `--translated` | `10.28.41.10` | Translated (SNAT) IP |
| `--nosnat-id` | `no-snat-east-west` | NO_SNAT rule ID |
| `--nosnat-name` | `NO-SNAT-east-west` | NO_SNAT display name |
| `--nosnat-source` | `10.28.40.0/21` | NO_SNAT source CIDR |
| `--nosnat-dest` | `10.0.0.0/8` | NO_SNAT destination CIDR |

## Idempotency

All `create` operations use `PUT`, so re-running overwrites with the same
values. The script can be run multiple times safely.

## AWS Secrets Manager references

| Secret ID pattern | Usage |
|---|---|
| `evs-{environmentId}_nsxAdmin` | NSX Manager admin password |
