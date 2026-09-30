# nsx_mac_profile.py

Create the NSX "Nested-MAC" MAC Discovery Profile and bind it to segments.
Required for nested VM environments where MAC learning, MAC change, and
unknown unicast flooding must be enabled on the overlay segments.

## Prerequisites

- Python 3.9+
- Python packages: `boto3`, `requests`
- AWS credentials with access to Secrets Manager
- SOCKS/HTTPS proxy to NSX Manager (default: `http://127.0.0.1:9999`)

```bash
pip install boto3 requests
```

## How it works

1. Connects to NSX Manager via the Policy API
2. Creates a MAC Discovery Profile (`PUT /infra/mac-discovery-profiles/Nested-MAC`)
   with:
   - `mac_change_enabled: true`
   - `mac_learning_enabled: true`
   - `unknown_unicast_flooding_enabled: true`
   - `mac_limit: 4096` / `mac_limit_policy: ALLOW`
   - `remote_overlay_mac_limit: 2048`
   - `mac_learning_aging_time: 600`
3. Binds the profile to segments matching `--segmentname` via
   `PUT /infra/segments/{id}/segment-discovery-profile-binding-maps/{map-id}`

## Usage examples

### Step 1 — Create the profile

```bash
python3 nsx_mac_profile.py profile create \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Step 2 — Bind to segments

```bash
python3 nsx_mac_profile.py bind create \
    --segmentname "logical network segment" \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Show the profile

```bash
python3 nsx_mac_profile.py profile show \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Show binding status

```bash
python3 nsx_mac_profile.py bind show \
    --segmentname "logical network segment" \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Unbind from segments

```bash
python3 nsx_mac_profile.py bind delete \
    --segmentname "logical network segment" \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Delete the profile

```bash
python3 nsx_mac_profile.py profile delete \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Bind to a single segment

Use a specific segment name to match just one:

```bash
python3 nsx_mac_profile.py bind create \
    --segmentname "segment 5" \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci
```

### Override NSX connection

```bash
python3 nsx_mac_profile.py profile create \
    --nsx-host nsx.example.com \
    --nsx-user admin \
    --nsx-password 'MyPassword!' \
    --proxy http://127.0.0.1:9999
```

### Verbose logging

Add `-v` for debug output:

```bash
python3 nsx_mac_profile.py profile create \
    --config ../Phase_2_evs_env/python/config.json \
    --edge-spec ../Phase_3_VCF9/edge_cluster_spec.json \
    --profile ci -v
```

## Segment matching

The `--segmentname` flag performs a case-insensitive **substring** match against
each segment's `display_name`:

| `--segmentname` value | Matches |
|---|---|
| `"logical network segment"` | `logical network segment 1`, `logical network segment 2`, ... |
| `"segment 1"` | `logical network segment 1`, `logical network segment 10`, ... |
| `"segment 5"` | `logical network segment 5` |

## CLI reference

### `profile create`

| Flag | Default | Description |
|---|---|---|
| `--profile-id` | `Nested-MAC` | Profile ID in NSX |
| `--profile-name` | `Nested-MAC` | Profile display name |
| `--mac-limit` | `4096` | MAC address table limit |
| `--remote-mac-limit` | `2048` | Remote overlay MAC limit |
| `--aging-time` | `600` | MAC learning aging time (seconds) |

### `profile show` / `profile delete`

| Flag | Default | Description |
|---|---|---|
| `--profile-id` | `Nested-MAC` | Profile ID to show/delete |

### `bind create` / `bind show` / `bind delete`

| Flag | Default | Description |
|---|---|---|
| `--segmentname` | (required) | Substring match for segment display_names |
| `--profile-id` | `Nested-MAC` | Profile to bind/unbind |
| `--binding-map-id` | `nested-mac-binding` | Binding map ID on each segment |

### Common connection flags (all subcommands)

| Flag | Default | Description |
|---|---|---|
| `--config` | -- | Path to `config.json` |
| `--edge-spec` | -- | Path to `edge_cluster_spec.json` |
| `--profile` | -- | AWS CLI profile name |
| `--region` | from config | AWS region override |
| `--nsx-host` | from config/env | NSX Manager hostname |
| `--nsx-user` | `admin` | NSX username |
| `--nsx-password` | Secrets Mgr | NSX password |
| `--proxy` | `http://127.0.0.1:9999` | HTTPS proxy for NSX |
| `--verbose` / `-v` | off | Enable debug logging |

## Idempotency

- `profile create` uses `PUT` — re-running overwrites with the same values
- `bind create` uses `PUT` — re-running is a no-op if already bound
- `bind delete` skips segments that are not bound
- Safe to run multiple times

## AWS Secrets Manager references

| Secret ID pattern | Usage |
|---|---|
| `evs-{environmentId}_nsxAdmin` | NSX Manager admin password |
