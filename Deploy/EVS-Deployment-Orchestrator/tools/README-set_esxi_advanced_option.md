# set_esxi_advanced_option.py

Set an ESXi advanced option on all hosts in a vCenter cluster via the
vSphere API. No SSH access required — uses the pyVmomi `AdvancedOption`
manager to update host settings through vCenter.

Default operation: set `VSAN.FakeSCSIReservations` to `1` on every host
in the cluster.

## Prerequisites

- Python 3.9+
- Python packages: `boto3`, `pyvmomi`
- AWS credentials with access to Secrets Manager

```bash
pip install boto3 pyvmomi
```

## How it works

1. Loads `config.json` to derive vCenter hostname, cluster name, and
   environment ID
2. Retrieves the vCenter SSO password from AWS Secrets Manager
   (`evs-{environmentId}_vcenterSso`)
3. Connects to vCenter as `administrator@vsphere.local`
4. Finds the cluster (default: `{environmentId}-cl01`)
5. Iterates all ESXi hosts in the cluster
6. For each host, reads the current value of the advanced option and
   sets it to the target value via `host.configManager.advancedOption.UpdateOptions()`
7. Skips hosts that already have the target value

## Usage examples

### Set VSAN.FakeSCSIReservations=1 on all hosts (default)

```bash
python3 set_esxi_advanced_option.py \
    --config ../Phase_2_evs_env/python/config.json \
    --profile ci
```

### Dry run — check current values without changing anything

```bash
python3 set_esxi_advanced_option.py \
    --config ../Phase_2_evs_env/python/config.json \
    --profile ci \
    --dry-run
```

### Set a different advanced option

```bash
python3 set_esxi_advanced_option.py \
    --config ../Phase_2_evs_env/python/config.json \
    --profile ci \
    --option Net.TcpipHeapSize \
    --value 120
```

### Override cluster name

```bash
python3 set_esxi_advanced_option.py \
    --config ../Phase_2_evs_env/python/config.json \
    --profile ci \
    --cluster my-cluster
```

### Override vCenter connection

```bash
python3 set_esxi_advanced_option.py \
    --config ../Phase_2_evs_env/python/config.json \
    --profile ci \
    --vcenter-host vc.example.com \
    --vcenter-user admin@vsphere.local
```

### Show passwords on screen

```bash
python3 set_esxi_advanced_option.py \
    --config ../Phase_2_evs_env/python/config.json \
    --profile ci \
    --showpassword
```

### Verbose logging

```bash
python3 set_esxi_advanced_option.py \
    --config ../Phase_2_evs_env/python/config.json \
    --profile ci \
    -v
```

## CLI reference

| Flag | Default | Description |
|---|---|---|
| `--option` | `VSAN.FakeSCSIReservations` | Advanced option key to set |
| `--value` | `1` | Integer value to set |
| `--config` | -- | Path to `config.json` (required) |
| `--profile` | -- | AWS CLI profile name |
| `--region` | from config | AWS region override |
| `--vcenter-host` | from config | vCenter hostname |
| `--vcenter-user` | `administrator@vsphere.local` | vCenter username |
| `--cluster` | `{environmentId}-cl01` | Cluster name |
| `--dry-run` | off | Show current values without changing |
| `--showpassword` | off | Display credentials on screen |
| `--verbose` / `-v` | off | Enable debug logging |

## Idempotency

The script checks the current value of the option on each host before
setting it. If a host already has the target value, it is skipped. The
script can be run multiple times safely.

## AWS Secrets Manager references

| Secret ID pattern | Usage |
|---|---|
| `evs-{environmentId}_vcenterSso` | vCenter SSO password |
