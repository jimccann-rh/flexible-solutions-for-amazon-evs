# update_portgroup_notes.py

Add Custom Attributes to vCenter Distributed Port Groups with NSX segment
subnet data. After running `nsx_manager.py batch create` to build segments,
this script reads each segment's subnet metadata from NSX and sets it as
Custom Attributes on the matching port group in vCenter so operators can see
network details at a glance without switching to the NSX Manager UI.

NSX-managed port groups cannot be modified directly in vCenter (the Notes
field is locked). Custom Attributes are the supported way to annotate these
port groups — they appear in the port group's "Custom Attributes" section
in the vCenter UI.

## Prerequisites

- Python 3.9+
- Python packages: `boto3`, `pyvmomi`, `requests`
- AWS credentials with access to Secrets Manager
- SOCKS/HTTPS proxy to NSX Manager (default: `http://127.0.0.1:9999`)
- NSX segments already created (e.g. via `nsx_manager.py batch create`)

```bash
pip install boto3 pyvmomi requests
```

## How it works

1. Connects to NSX Manager and queries `GET /infra/segments`
2. Filters segments by case-insensitive substring match on `display_name`
3. Extracts subnet metadata from each segment:
   - Network CIDR
   - Gateway address
   - DHCP server address
   - DHCP range
   - DNS servers
4. Connects to vCenter as `administrator@vsphere.local`
5. Creates Custom Attribute definitions (type: Distributed Port Group) for
   each field if they don't already exist: `Network`, `Gateway`,
   `DHCP Server`, `DHCP Range`, `DNS`
6. Finds the matching port group by name and sets the attribute values

## Custom Attributes created

The script creates five Custom Attributes scoped to Distributed Port Groups:

| Attribute | Example value |
|---|---|
| Network | `10.28.40.0/25` |
| Gateway | `10.28.40.1/25` |
| DHCP Server | `10.28.40.2/25` |
| DHCP Range | `10.28.40.50-10.28.40.120` |
| DNS | `10.28.32.100` |

In the vCenter UI, navigate to the port group and look under **Custom Attributes**
to see these values.

## Usage examples

### List matching segments with subnet data

Query NSX and display the subnet info without connecting to vCenter:

```bash
python3 update_portgroup_notes.py \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci \
    --segmentname "logical network segment" \
    --show-segments
```

### Dry run

Show what custom attributes would be set without making changes:

```bash
python3 update_portgroup_notes.py \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci \
    --segmentname "logical network segment" \
    --dry-run
```

### Apply custom attributes to all matching port groups

```bash
python3 update_portgroup_notes.py \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci \
    --segmentname "logical network segment"
```

### Update a single segment's port group

Use a more specific pattern to match just one segment:

```bash
python3 update_portgroup_notes.py \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci \
    --segmentname "segment 5"
```

### Show passwords on screen

```bash
python3 update_portgroup_notes.py \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci \
    --segmentname "logical network segment" \
    --showpassword
```

### Verbose logging

Add `-v` for debug-level output:

```bash
python3 update_portgroup_notes.py \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci \
    --segmentname "logical network segment" \
    -v
```

### Override vCenter or NSX connection

```bash
python3 update_portgroup_notes.py \
    --segmentname "logical network segment" \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci \
    --vcenter-host vc.example.com \
    --nsx-host nsx.example.com
```

## Segment matching

The `--segmentname` flag performs a case-insensitive **substring** match against
each segment's `display_name`:

| `--segmentname` value | Matches |
|---|---|
| `"logical network segment"` | `logical network segment 1`, `logical network segment 2`, ... `logical network segment 16` |
| `"segment 1"` | `logical network segment 1`, `logical network segment 10`, ... `logical network segment 16` |
| `"segment 5"` | `logical network segment 5` |

## CLI reference

| Flag | Default | Description |
|---|---|---|
| `--segmentname` | -- | Substring to match against segment display_names (required) |
| `--config` | -- | Path to `config.json` |
| `--edge-spec` | -- | Path to `edge_cluster_spec.json` |
| `--profile` | -- | AWS CLI profile name |
| `--region` | from config | AWS region override |
| `--nsx-host` | from config/env | NSX Manager hostname |
| `--nsx-user` | `admin` | NSX username |
| `--nsx-password` | Secrets Mgr | NSX password |
| `--proxy` | `http://127.0.0.1:9999` | HTTPS proxy for NSX |
| `--vcenter-host` | from config | vCenter hostname |
| `--vcenter-user` | `administrator@vsphere.local` | vCenter username |
| `--dry-run` | off | Show plan without updating |
| `--show-segments` | off | List segments with subnet data and exit |
| `--showpassword` | off | Display credentials on screen |
| `--verbose` / `-v` | off | Enable debug logging |

## Idempotency

The script can be run multiple times safely. Custom Attribute definitions are
created once and reused on subsequent runs. Each run overwrites the attribute
values with the current NSX segment subnet data. If a segment's subnet
configuration changes, re-running the script will update the values.

## AWS Secrets Manager references

| Secret ID pattern | Usage |
|---|---|
| `evs-{environmentId}_nsxAdmin` | NSX Manager admin password |
| `evs-{environmentId}_vcenterSso` | vCenter SSO password |
