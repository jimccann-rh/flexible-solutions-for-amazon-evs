# DNS Register — Route 53 A + PTR Records for vSphere VMs

Automatically create DNS records in AWS Route 53 for VMs running in vSphere/NSX. Creates both forward (A) and reverse (PTR) records.

## How It Works

1. Queries vCenter for VM name and IP (via VMware Tools guest networking)
2. Sanitizes the VM name into a valid DNS hostname (lowercase, special chars to dashes)
3. Creates an **A record** in the forward zone (`vm-name.vci.devcluster.openshift.com`)
4. Creates a **PTR record** in the reverse zone (`x.40.28.10.in-addr.arpa`)

## Prerequisites

- Python 3 with `requests` and `boto3` installed (`pip install -r requirements.txt`)
- AWS credentials configured (`aws configure` or `AWS_PROFILE`)
- IAM permissions: `route53:ListHostedZones`, `route53:ListResourceRecordSets`, `route53:ChangeResourceRecordSets`
- vCenter credentials (for `show`, `sync`, and `--vm-name` lookups)
- VMware Tools running on target VMs (for IP resolution)

## Route 53 Zones

The script auto-detects these zones:

| Zone | Type | ID |
|---|---|---|
| `vci.devcluster.openshift.com` | Forward (A records) | `Z02490921DFJ6JI2BB78G` |
| `28.10.in-addr.arpa` | Reverse (PTR records) | `Z01222901D7JB64E72IXM` |

## Environment Variables

```bash
# Single vCenter
export VC_HOST="vc.vci.devcluster.openshift.com"

# Multiple vCenters (comma-separated)
export VC_HOSTS="vc1.example.com,vc2.example.com"

export VC_USER="ai@vsphere.local"
export VC_PASSWORD="your-vcenter-password"
export HTTPS_PROXY="http://127.0.0.1:9999"
export AWS_PROFILE="ci"
export AWS_REGION="us-east-1"
```

## Usage

```
python3 dns_register.py {show,register,delete,sync} [options]
```

### Options

| Option | Default | Description |
|---|---|---|
| `--vc-host` | `$VC_HOSTS` or `$VC_HOST` | vCenter hostname (repeatable or comma-separated for multiple) |
| `--vc-user` | `$VC_USER` or `ai@vsphere.local` | vCenter username (same creds for all vCenters) |
| `--vc-password` | `$VC_PASSWORD` | vCenter password |
| `--proxy` | `$HTTPS_PROXY` | HTTPS proxy |
| `--aws-profile` | `$AWS_PROFILE` or `ci` | AWS CLI profile |
| `--aws-region` | `$AWS_REGION` or `us-east-1` | AWS region |
| `--domain` | `vci.devcluster.openshift.com` | DNS domain for A records |
| `--ip-range` | | Only process VMs in this range (e.g. `10.28.40-48.*` or `10.28.40.0/21`) |
| `--dedupe-prefix` | off | Prefix DNS hostname with vCenter name when duplicate VM names exist |
| `--first-only` | off | When duplicate VM names exist, keep only the first one found |
| `--dry-run` | off | Preview changes without applying |
| `--vm-name` | | vSphere VM name (register/delete) |
| `--hostname` | | Manual DNS hostname (register/delete) |
| `--ip` | | Manual IP address (register/delete) |
| `--raw-hostname` | off | Use hostname exactly as given, skip sanitization (register/delete — allows subdomains) |
| `--prefix` | | Filter VMs by name prefix (show/sync) |
| `--cleanup` | off | Remove stale A + PTR records not matching any current VM (sync only) |
| `--cleanup-prefix` | | Only clean up records whose hostname starts with this prefix (sync only) |
| `--watch` | off | Continuously sync on a timer (sync only) |
| `--watch-interval` | `120` | Seconds between watch cycles |

## Examples

### 1. Register a VM by name (auto-lookup IP from vCenter)

```bash
python3 dns_register.py register --vm-name my-fedora-vm \
  --vc-password 'password-here'
```

Output:
```
  A     my-fedora-vm.vci.devcluster.openshift.com.        -> 10.28.40.53
  PTR   53.40.28.10.in-addr.arpa.                         -> my-fedora-vm.vci.devcluster.openshift.com.

Done. 1 VM(s) registered.
```

### 2. Register manually (no vCenter needed)

```bash
python3 dns_register.py register --hostname myhost --ip 10.28.40.53
```

Output:
```
  A     myhost.vci.devcluster.openshift.com.              -> 10.28.40.53
  PTR   53.40.28.10.in-addr.arpa.                         -> myhost.vci.devcluster.openshift.com.

Done. 1 VM(s) registered.
```

### 3. Register a subdomain (raw hostname)

Use `--raw-hostname` to skip sanitization and create subdomain records (dots are preserved):

```bash
python3 dns_register.py register --hostname api.jimccann-dev --ip 10.28.45.15 --raw-hostname
```

Output:
```
  A     api.jimccann-dev.vci.devcluster.openshift.com.    -> 10.28.45.15
  PTR   15.45.28.10.in-addr.arpa.                         -> api.jimccann-dev.vci.devcluster.openshift.com.

Done. 1 VM(s) registered.
```

Without `--raw-hostname`, the dot would be replaced with a dash (`api-jimccann-dev`).

To remove:
```bash
python3 dns_register.py delete --hostname api.jimccann-dev --ip 10.28.45.15 --raw-hostname
```

### 4. Show DNS status for all VMs

```bash
python3 dns_register.py show --vc-password 'password-here'
```

Output:
```
VM Name                                        IP                DNS (A)          DNS (PTR)
-----------------------------------------------------------------------------------------------
  my-fedora-vm                                   10.28.40.53       OK               OK
  test-rhel9                                     10.28.40.54       MISSING          MISSING
```

### 5. Dry run before registering

```bash
python3 dns_register.py register --vm-name test-rhel9 --dry-run \
  --vc-password 'password-here'
```

Output:
```
  DRY RUN: would create A test-rhel9.vci.devcluster.openshift.com. -> 10.28.40.54
  DRY RUN: would create PTR 54.40.28.10.in-addr.arpa. -> test-rhel9.vci.devcluster.openshift.com.
```

### 6. Sync all VMs from vCenter

Register all powered-on VMs that have IPs:

```bash
python3 dns_register.py sync --vc-password 'password-here'
```

Output:
```
=== Syncing 3 VM(s) to Route 53 (vci.devcluster.openshift.com) ===

  Created A     my-fedora-vm.vci.devcluster.openshift.com.        -> 10.28.40.53
  Created PTR   53.40.28.10.in-addr.arpa.                         -> my-fedora-vm.vci.devcluster.openshift.com.
  Created A     test-rhel9.vci.devcluster.openshift.com.           -> 10.28.40.54
  Created PTR   54.40.28.10.in-addr.arpa.                         -> test-rhel9.vci.devcluster.openshift.com.

  Created: 2  Updated: 0  Unchanged: 1
```

### 7. Sync only VMs in an IP range

Only register VMs whose IP falls in a range — useful for skipping infrastructure VMs:

```bash
# Wildcard range (10.28.40.x through 10.28.48.x)
python3 dns_register.py sync --ip-range "10.28.40-48.*" --vc-password 'password-here'

# CIDR notation
python3 dns_register.py sync --ip-range "10.28.40.0/21" --vc-password 'password-here'
```

Works with `show`, `sync`, `register --vm-name`, and `delete --vm-name`.

### 8. Sync only VMs with a name prefix

```bash
python3 dns_register.py sync --prefix test --vc-password 'password-here'
```

### 9. Clean up stale records

Remove A + PTR records that no longer match any VM in vCenter:

```bash
# Dry run first — see what would be removed
python3 dns_register.py sync --cleanup --dry-run --vc-password 'password-here'

# Only clean up records starting with "testnetwork" (protects infrastructure)
python3 dns_register.py sync --cleanup --cleanup-prefix testnetwork --dry-run \
  --vc-password 'password-here'

# Apply cleanup
python3 dns_register.py sync --cleanup --cleanup-prefix testnetwork \
  --vc-password 'password-here'
```

Output:
```
  Removed stale A     testnetwork2-logical-network-segment-1.vci...  -> 10.28.40.81
  Removed stale PTR   81.40.28.10.in-addr.arpa.                     -> testnetwork2-...

  Created: 0  Updated: 0  Unchanged: 31  Cleaned: 16
```

### 10. Duplicate VM names across vCenters (uptime-based resolution)

When the same VM name exists on multiple vCenters, the script automatically resolves the conflict by querying each VM's uptime via VMware Tools (SOAP API). The VM with the **longest power-on time** wins and gets the A + PTR record. This works for both Linux and Windows VMs — the uptime is reported by the hypervisor, not the guest OS.

```bash
python3 dns_register.py sync \
  --vc-host "vc.example.com,vc2.example.com" \
  --vc-password 'password-here'
```

Output:
```
  Duplicate VM 'vm1': keeping vc.example.com (uptime 86400s)
```

For alternative duplicate handling:

**`--dedupe-prefix`** — give each duplicate a vCenter-prefixed hostname instead of picking a winner:

```bash
python3 dns_register.py sync --dedupe-prefix \
  --vc-host "vc.example.com,vc2.example.com" \
  --vc-password 'password-here'
```

- `VM1` on `vc` → `vc-vm1.vci.devcluster.openshift.com`
- `VM1` on `vc2` → `vc2-vm1.vci.devcluster.openshift.com`

Unique VM names are not affected — only duplicates get the prefix.

**`--first-only`** — skip duplicates entirely (first vCenter wins, no uptime check):

```bash
python3 dns_register.py sync --first-only \
  --vc-host "vc.example.com,vc2.example.com" \
  --vc-password 'password-here'
```

`VM1` on `vc` registers as `vm1.vci.devcluster.openshift.com`; `VM1` on `vc2` is skipped.

### 11. Watch mode (continuous sync)

```bash
python3 dns_register.py sync --watch --watch-interval 120 \
  --vc-password 'password-here'
```

Syncs every 2 minutes. Press **Ctrl+C** to stop.

### 12. Delete DNS records for a VM

```bash
python3 dns_register.py delete --vm-name my-fedora-vm \
  --vc-password 'password-here'
```

Output:
```
  Deleted A     my-fedora-vm.vci.devcluster.openshift.com. -> 10.28.40.53
  Deleted PTR   53.40.28.10.in-addr.arpa. -> my-fedora-vm.vci.devcluster.openshift.com.

Done.
```

### 13. Delete manually (no vCenter needed)

```bash
python3 dns_register.py delete --hostname myhost --ip 10.28.40.53
```

### 14. Multiple vCenters

Query VMs from multiple vCenters (same credentials used for all):

```bash
# Comma-separated
python3 dns_register.py show \
  --vc-host "vc1.example.com,vc2.example.com" \
  --vc-password 'your-password'

# Repeated flag
python3 dns_register.py sync \
  --vc-host vc1.example.com --vc-host vc2.example.com \
  --vc-password 'your-password'

# Via environment variable
export VC_HOSTS="vc1.example.com,vc2.example.com"
python3 dns_register.py sync
```

Output:
```
  Connected to vCenter: vc1.example.com
  Connected to vCenter: vc2.example.com

=== Syncing 5 VM(s) to Route 53 (vci.devcluster.openshift.com) ===
  ...
```

If one vCenter is unreachable, the script warns and continues with the others.

### 15. Using environment variables

```bash
export VC_PASSWORD='password-here'
export AWS_PROFILE=ci

python3 dns_register.py show
python3 dns_register.py sync
python3 dns_register.py sync --watch
```

## Workflow

```
vCenter                       Script                        Route 53
  |                              |                              |
  |  VM powered on, gets IP      |                              |
  |  via DHCP / VMware Tools     |                              |
  |<-----------------------------|                              |
  |                              |  register/sync:              |
  |                              |  1. query VM name + IP       |
  |                              |  2. sanitize hostname        |
  |                              |  3. upsert A record          |
  |                              |  4. upsert PTR record        |
  |                              |----------------------------->|
  |                              |                              |
  |                              |  Resolvers 10.28.32.100/101  |
  |                              |  now resolve the VM's FQDN   |
```

## VM Name to DNS Hostname

The VM name is sanitized for DNS:
- Converted to lowercase
- Special characters replaced with dashes
- Multiple dashes collapsed
- Leading/trailing dashes removed

| vSphere VM Name | DNS Hostname |
|---|---|
| `my-fedora-vm` | `my-fedora-vm` |
| `Test VM (clone)` | `test-vm-clone-` |
| `RHEL_9_Server` | `rhel-9-server` |

## Notes

- **Idempotent** — running register or sync again with the same data makes no changes.
- **UPSERT** — if an A or PTR record already exists, it is updated (not duplicated).
- **IP changes** — if a VM gets a new IP, sync detects the change and updates both records.
- **No auto-delete** — `sync` creates/updates records but does not remove records for VMs that no longer exist. Use `delete` to clean up manually.
- **TTL** — records are created with a 300-second (5 minute) TTL.
- **VMware Tools required** — the script resolves IPs via the vCenter guest networking API. VMs without VMware Tools (or open-vm-tools) are skipped.
- **Manual mode** — `register --hostname X --ip Y` and `delete --hostname X --ip Y` work without vCenter credentials.
