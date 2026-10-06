# NSX Gateway Firewall — Block Internet Per VM

Block internet access for specific VMs using the NSX T1 Gateway Firewall. VMs can be blocked by IP directly, or by tagging them in vCenter and running `sync`.

## Why Gateway Firewall (not DFW)

AWS EVS runs ESXi hosts in **ENS_INTERRUPT** mode, which disables the Distributed Firewall datapath. The T1 Gateway Firewall runs on the **edge nodes** instead, so it works regardless of host switch mode.

## How It Works

1. The T1 gateway firewall is enabled
2. An IP group (`nointernet-vms`) holds the list of blocked VM IPs
3. A gateway policy with two rules:
   - **Allow-Private-Networks** — allows traffic to RFC1918 (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
   - **Drop-Internet** — drops all other traffic (logged)

**Two ways to manage blocked VMs:**
- **By IP** — `block --ip` / `unblock --ip` for direct control
- **By vCenter tag** — tag VMs with `nointernet` in vCenter, then run `sync` to resolve IPs and update the firewall

## Prerequisites

- Python 3 with `requests` installed (`pip install -r requirements.txt`)
- NSX Manager admin credentials
- vCenter admin credentials (only needed for `sync` and `show` tag info)
- VMware Tools running on VMs (for IP resolution during `sync`)

## Environment Variables

```bash
export NSX_HOST="nsx.vci.devcluster.openshift.com"
export NSX_USER="admin"
export NSX_PASSWORD="your-nsx-password"
export VC_HOST="vc.vci.devcluster.openshift.com"
export VC_USER="administrator@vsphere.local"
export VC_PASSWORD="your-vcenter-password"
export HTTPS_PROXY="http://127.0.0.1:9999"
```

## Usage

```
python3 nsx_tag_firewall.py {setup,block,unblock,sync,show,teardown} [options]
```

### Options

| Option | Default | Description |
|---|---|---|
| `--nsx-host` | `$NSX_HOST` | NSX Manager hostname |
| `--nsx-user` | `$NSX_USER` or `admin` | NSX username |
| `--nsx-password` | `$NSX_PASSWORD` | NSX password (required) |
| `--vc-host` | `$VC_HOST` | vCenter hostname (for sync) |
| `--vc-user` | `$VC_USER` | vCenter username (for sync) |
| `--vc-password` | `$VC_PASSWORD` | vCenter password (for sync) |
| `--proxy` | `$HTTPS_PROXY` | HTTPS proxy |
| `--t1-id` | `$NSX_T1_ID` (auto) | Tier-1 gateway ID |
| `--tag-category` | `network-policy` | vCenter tag category |
| `--tag-name` | `nointernet` | vCenter tag name |
| `--loop` | off | Retry sync until all tagged VMs report an IP |
| `--loop-interval` | `10` | Seconds between retries |
| `--loop-timeout` | `600` | Max seconds to wait before giving up (10 min) |
| `--watch` | off | Continuously sync on a timer (Ctrl+C to stop) |
| `--watch-interval` | `60` | Seconds between watch cycles |

## Examples

### 1. Initial setup

```bash
python3 nsx_tag_firewall.py setup --nsx-password 'password-here'
```

### 2. Block by IP (no vCenter needed)

```bash
python3 nsx_tag_firewall.py block --ip 10.28.40.53 --nsx-password 'password-here'
```

### 3. Unblock by IP

```bash
python3 nsx_tag_firewall.py unblock --ip 10.28.40.53 --nsx-password 'password-here'
```

### 4. Block by vCenter tag (recommended workflow)

Tag one or more VMs in vCenter with the `nointernet` tag (under category `network-policy`), then sync:

```bash
python3 nsx_tag_firewall.py sync \
  --nsx-password 'password-here' \
  --vc-password 'password-here'
```

Output:
```
=== VMs tagged 'network-policy/nointernet' ===
  testnetwork-logical-network-segment-1   10.28.40.53

  + blocked 10.28.40.53

Synced. 1 IP(s) now blocked.
```

Remove the vCenter tag and sync again to unblock:
```bash
# Remove tag in vCenter UI, then:
python3 nsx_tag_firewall.py sync \
  --nsx-password 'password-here' \
  --vc-password 'password-here'
```

Output:
```
No VMs tagged with 'network-policy/nointernet' in vCenter
Cleared 1 previously blocked IP(s)
```

### 5. Sync with loop (wait for VM IPs)

If a VM was just powered on or VMware Tools hasn't started yet, use `--loop` to retry until all tagged VMs report an IP:

```bash
python3 nsx_tag_firewall.py sync --loop \
  --nsx-password 'password-here' \
  --vc-password 'password-here'
```

Output:
```
=== VMs tagged 'network-policy/nointernet' ===
  testnetwork-logical-network-segment-1   (no IP found — VMware Tools running?)

Waiting for 1 VM(s) to report IP... retrying in 10s (9m50s remaining, Ctrl+C to stop)

=== VMs tagged 'network-policy/nointernet' ===
  testnetwork-logical-network-segment-1   10.28.40.53

  + blocked 10.28.40.53

Synced. 1 IP(s) now blocked.
```

Custom interval and timeout:
```bash
# Check every 30s, give up after 5 minutes
python3 nsx_tag_firewall.py sync --loop --loop-interval 30 --loop-timeout 300 \
  --nsx-password 'password-here' \
  --vc-password 'password-here'
```

Press **Ctrl+C** to stop waiting early — it will proceed with whatever IPs are available. If no VMs have IPs when the timeout is reached, any previously blocked IPs are removed from the firewall rules.

### 6. Watch mode (continuous sync)

Run as a daemon-style watcher that syncs every 60 seconds:

```bash
python3 nsx_tag_firewall.py sync --watch \
  --nsx-password 'password-here' \
  --vc-password 'password-here'
```

Custom interval (every 30 seconds) and combined with `--loop`:
```bash
python3 nsx_tag_firewall.py sync --watch --watch-interval 30 --loop \
  --nsx-password 'password-here' \
  --vc-password 'password-here'
```

Press **Ctrl+C** to stop. Tag or untag VMs in vCenter at any time — the next cycle picks up the change. If a tagged VM is powered off, its IP is automatically removed from the blocked list so new VMs that receive that IP aren't incorrectly blocked.

### 7. Show status (with vCenter tag info)

```bash
python3 nsx_tag_firewall.py show \
  --nsx-password 'password-here' \
  --vc-password 'password-here'
```

Output:
```
=== Blocked VMs (no internet) ===
  10.28.40.53

=== vCenter tagged 'network-policy/nointernet' ===
  testnetwork-logical-network-segment-1   10.28.40.53           synced

=== Gateway Policy: Block Internet Access ===
  Allow-Private-Networks            action=ALLOW   enabled
  Drop-Internet                     action=DROP    enabled
```

### 8. Show status (without vCenter — NSX only)

```bash
python3 nsx_tag_firewall.py show --nsx-password 'password-here'
```

### 9. Tear down

```bash
python3 nsx_tag_firewall.py teardown --nsx-password 'password-here'
python3 nsx_tag_firewall.py teardown --disable-firewall --nsx-password 'password-here'
```

### 10. Using environment variables

```bash
export NSX_PASSWORD='password-here'
export VC_PASSWORD='password-here'

python3 nsx_tag_firewall.py setup
python3 nsx_tag_firewall.py sync       # sync from vCenter tags
python3 nsx_tag_firewall.py show
python3 nsx_tag_firewall.py sync       # re-sync after tag changes
```

## Workflow

```
vCenter                          Script                         NSX
  │                                │                              │
  │  Admin tags VM "nointernet"    │                              │
  ├───────────────────────────────>│                              │
  │                                │  sync: lookup tagged VMs     │
  │                                │  resolve VM IPs via Tools    │
  │                                │  update nointernet-vms group │
  │                                ├─────────────────────────────>│
  │                                │                              │  Gateway firewall
  │                                │                              │  blocks internet
  │                                │                              │  for those IPs
```

## Testing

After blocking a VM:

```bash
# Should be BLOCKED
ping 8.8.8.8
curl https://google.com

# Should still WORK
ping 10.28.32.100
ping 10.28.40.1
```

## Notes

- **`sync` replaces the blocked list** — it reads all tagged VMs and sets the group to exactly those IPs. Manually blocked IPs (via `block --ip`) will be removed if those VMs aren't also tagged.
- **Power state checked** — `sync` and `show` check if each tagged VM is powered on. Powered-off or suspended VMs are skipped, and their IPs are removed from the blocked list (prevents blocking a new VM that inherits the same IP).
- **Watch survives power-off** — `--watch` keeps looping even when all tagged VMs are powered off. It removes their IPs from the firewall and picks them back up when they power on again.
- **VMware Tools required** — `sync` resolves IPs via the vCenter guest networking API, which requires VMware Tools (or open-vm-tools) running in the VM.
- **vCenter credentials are optional** — `block`/`unblock`/`setup`/`teardown` only need NSX credentials. vCenter is only used by `sync` and for tag info in `show`.
- The gateway policy is visible in the NSX UI under **Security > Gateway Firewall > Gateway Specific Rules**.
- Dropped packets are logged — check edge node syslog for troubleshooting.
