# create_datastore_directory_iso_upload.py

Create a directory on a vSAN datastore via vCenter and optionally upload
ISO files (or any file) to it.

## Prerequisites

```bash
pip install boto3 pyvmomi
```

- A working vCenter with at least one cluster
- AWS credentials with access to Secrets Manager
- `config.json` (Phase 2) with `environmentId`, `fqdn`, `vcfHostnames`, and `region`

## How it works

1. Derives the vCenter FQDN from `config.json`
   (`vcfHostnames.vcenter` + `.` + `fqdn`)
2. Retrieves the vCenter `administrator@vsphere.local` password from
   Secrets Manager (`evs-<env-id>_vcenterSso`)
3. Connects to vCenter via pyVmomi
4. Finds the cluster (`<environmentId>-cl01` by default) and its vSAN datastore
5. Creates the directory (default: `isos`) via the FileManager API
6. Optionally uploads files using vCenter's HTTP file access endpoint
   (PUT to `/folder/...` with SOAP session cookie auth, 16 MB chunked)

## Usage

### Create the isos directory (default)

```bash
python3 create_datastore_directory_iso_upload.py \
  --profile ci \
  --config ../../Phase_2_evs_env/python/config.json
```

### Create directory and upload an ISO

```bash
python3 create_datastore_directory_iso_upload.py \
  --profile ci \
  --config ../../Phase_2_evs_env/python/config.json \
  --upload /path/to/rhcos-live.iso
```

### Upload multiple files

```bash
python3 create_datastore_directory_iso_upload.py \
  --profile ci \
  --config ../../Phase_2_evs_env/python/config.json \
  --upload /path/to/rhcos-live.iso /path/to/other.iso
```

### Override datastore and directory name

```bash
python3 create_datastore_directory_iso_upload.py \
  --profile ci \
  --config ../../Phase_2_evs_env/python/config.json \
  --datastore my-datastore \
  --directory my-isos \
  --upload /path/to/rhcos-live.iso
```

### Override vCenter host and cluster

```bash
python3 create_datastore_directory_iso_upload.py \
  --profile ci \
  --config ../../Phase_2_evs_env/python/config.json \
  --vcenter-host vc.example.com \
  --cluster my-cluster-01
```

### Show vCenter credentials

```bash
python3 create_datastore_directory_iso_upload.py \
  --profile ci \
  --config ../../Phase_2_evs_env/python/config.json \
  --showpassword
```

### Verbose / debug logging

```bash
python3 create_datastore_directory_iso_upload.py \
  --profile ci \
  --config ../../Phase_2_evs_env/python/config.json \
  --verbose
```

## CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--config` | (required) | Path to config.json |
| `--profile` | (none) | AWS CLI profile name |
| `--region` | from config | AWS region override |
| `--datastore` | auto-detect vSAN | Datastore name override |
| `--directory` | `isos` | Directory name to create on the datastore |
| `--vcenter-host` | from config | vCenter hostname/IP override |
| `--vcenter-user` | `administrator@vsphere.local` | vCenter username |
| `--cluster` | `{environmentId}-cl01` | Cluster name override |
| `--upload FILE [FILE ...]` | (none) | Upload one or more files to the directory |
| `--showpassword` | off | Display vCenter credentials on screen |
| `--verbose` / `-v` | off | Enable debug logging |

## Config file

### config.json (required fields)

```json
{
  "environmentId": "env-ksksyki5m0",
  "region": "us-east-1",
  "fqdn": "vci.devcluster.openshift.com",
  "vcfHostnames": {
    "vcenter": "vc"
  }
}
```

## Secrets Manager

The vCenter password is retrieved from:

```
evs-<environmentId>_vcenterSso
```

For example: `evs-env-ksksyki5m0_vcenterSso`

This is the `administrator@vsphere.local` password.

## Idempotent

If the directory already exists, the script prints a message and continues
(no error). Safe to run multiple times. Uploading a file that already exists
will overwrite it.
