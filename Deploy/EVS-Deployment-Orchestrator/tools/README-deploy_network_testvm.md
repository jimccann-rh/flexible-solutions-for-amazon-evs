# deploy_network_testvm.py

Deploy test VMs from an OVA file to NSX segments matching a wildcard pattern.
The script queries NSX Manager for segments whose `display_name` contains the
search string, then deploys one VM per matching segment using VMware ovftool.

## Prerequisites

- Python 3.9+
- VMware ovftool in `PATH`
- Python packages: `boto3`, `pyvmomi`, `requests`
- AWS credentials with access to Secrets Manager
- SOCKS/HTTPS proxy to NSX Manager (default: `http://127.0.0.1:9999`)
- NSX segments already created (e.g. via `nsx_manager.py batch create`)

```bash
pip install boto3 pyvmomi requests
```

## How it works

1. Loads `config.json` and optionally `edge_cluster_spec.json` to derive the
   NSX Manager hostname, ESXi host, and AWS environment ID.
2. Retrieves the NSX admin password and ESXi root password from AWS Secrets Manager.
3. Connects to NSX and queries `GET /infra/segments`, filtering by case-insensitive
   substring match on `display_name`.
4. Parses the OVA to extract OVF-defined network names.
5. For each matching segment, deploys the OVA via ovftool to the ESXi host,
   mapping all OVF networks to the segment's `display_name` (which corresponds
   to the port group visible on the ESXi host).
6. Names each VM `<prefix>-<segment-display-name>` with spaces replaced by dashes.
7. Skips any VM that already exists on the ESXi host.

## Connection resolution order

NSX host and passwords are resolved in priority order:

| Value | 1st (CLI) | 2nd (env var) | 3rd (config file) | 4th (Secrets Manager) |
|---|---|---|---|---|
| NSX host | `--nsx-host` | `$NSX_HOST` | `config.json` vcfHostnames.nsx | -- |
| NSX password | `--nsx-password` | `$NSX_PASSWORD` | -- | `evs-{env}_nsxAdmin` |
| NSX user | `--nsx-user` | `$NSX_USER` | -- | -- (default: `admin`) |
| ESXi host | `--esxi-host` | -- | `config.json` vcfHostnames.esxi01 | -- |
| ESXi password | -- | -- | -- | `evs!{env}_{hostname}` |

## Segment matching

The `--segmentname` flag performs a case-insensitive **substring** match against
each segment's `display_name`. For example:

| `--segmentname` value | Matches |
|---|---|
| `"logical network segment"` | `logical network segment 1`, `logical network segment 2`, ... `logical network segment 16` |
| `"segment 1"` | `logical network segment 1`, `logical network segment 10`, ... `logical network segment 16` |
| `"segment 5"` | `logical network segment 5` |

## VM naming

Each VM is named `<prefix>-<segment-display-name>` with spaces replaced by dashes.
The default prefix is `testnetwork`.

| Segment display_name | VM name |
|---|---|
| `logical network segment 1` | `testnetwork-logical-network-segment-1` |
| `logical network segment 12` | `testnetwork-logical-network-segment-12` |

Override the prefix with `--vm-prefix`:

```bash
--vm-prefix mytest
# produces: mytest-logical-network-segment-1
```

## Usage examples

### Inspect OVF properties (no AWS/NSX needed)

```bash
python3 deploy_network_testvm.py \
    --ova /path/to/test-vm.ova \
    --show-properties
```

### Probe OVA with ovftool

```bash
python3 deploy_network_testvm.py \
    --ova /path/to/test-vm.ova \
    --ovftool-probe
```

### List matching segments (no deployment)

```bash
python3 deploy_network_testvm.py \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci \
    --segmentname "logical network segment" \
    --show-segments
```

### Dry run (show planned VMs)

```bash
python3 deploy_network_testvm.py \
    --ova /path/to/test-vm.ova \
    --segmentname "logical network segment" \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci \
    --dry-run
```

### Deploy to all matching segments

```bash
python3 deploy_network_testvm.py \
    --ova /path/to/test-vm.ova \
    --segmentname "logical network segment" \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Deploy to a single segment

```bash
python3 deploy_network_testvm.py \
    --ova /path/to/test-vm.ova \
    --segmentname "segment 5" \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Deploy without powering on VMs

```bash
python3 deploy_network_testvm.py \
    --ova /path/to/test-vm.ova \
    --segmentname "logical network segment" \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci \
    --no-power-on
```

### Deploy with extra OVF properties

```bash
python3 deploy_network_testvm.py \
    --ova /path/to/test-vm.ova \
    --segmentname "logical network segment" \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci \
    --prop guestinfo.hostname=testvm \
    --prop guestinfo.dns=10.28.32.100
```

### Deploy to a specific ESXi host and datastore

By default the script auto-detects the first vSAN datastore on the ESXi host.
Use `--datastore-name` to override:

```bash
python3 deploy_network_testvm.py \
    --ova /path/to/test-vm.ova \
    --segmentname "logical network segment" \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci \
    --esxi-host 10.28.33.10 \
    --datastore-name my-datastore
```

### Show passwords on screen

```bash
python3 deploy_network_testvm.py \
    --ova /path/to/test-vm.ova \
    --segmentname "logical network segment" \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci \
    --showpassword
```

### Verbose logging

Add `-v` to any command for debug-level output:

```bash
python3 deploy_network_testvm.py \
    --ova /path/to/test-vm.ova \
    --segmentname "logical network segment" \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci -v
```

## CLI reference

| Flag | Default | Description |
|---|---|---|
| `--ova` | -- | Path to the OVA file (required for deployment) |
| `--segmentname` | -- | Substring to match against segment display_names (required) |
| `--config` | -- | Path to `config.json` |
| `--edge-spec` | -- | Path to `edge_cluster_spec.json` |
| `--profile` | -- | AWS CLI profile name |
| `--region` | from config | AWS region override |
| `--nsx-host` | from config/env | NSX Manager hostname |
| `--nsx-user` | `admin` | NSX username |
| `--nsx-password` | Secrets Mgr | NSX password |
| `--proxy` | `http://127.0.0.1:9999` | HTTPS proxy for NSX |
| `--esxi-host` | from config | ESXi host IP or FQDN |
| `--datastore-name` | auto-detect vSAN | Target ESXi datastore (default: first vSAN datastore) |
| `--vm-prefix` | `testnetwork` | Prefix for VM names |
| `--no-power-on` | off | Skip powering on VMs after deploy |
| `--prop KEY=VALUE` | -- | Extra OVF property (repeatable) |
| `--dry-run` | off | Show plan without deploying |
| `--show-segments` | off | List matching segments and exit |
| `--show-properties` | off | List OVF properties and exit |
| `--ovftool-probe` | off | Run ovftool against the OVA and exit |
| `--showpassword` | off | Display credentials on screen |
| `--verbose` / `-v` | off | Enable debug logging |

## Idempotency

The script checks for existing VMs before each deployment. If a VM with the
planned name already exists on the ESXi host, it is skipped with a `SKIP`
message. This means you can safely re-run the script and it will only deploy
VMs that don't already exist.

## AWS Secrets Manager references

| Secret ID pattern | Usage |
|---|---|
| `evs-{environmentId}_nsxAdmin` | NSX Manager admin password |
| `evs!{environmentId}_{esxiHostname}` | ESXi root password |
