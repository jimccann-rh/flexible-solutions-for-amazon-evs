"""CIDR helpers mirroring the automation's IP-derivation rules.

Given a per-pool CIDR, the automation derives the gateway and the DHCP /
static IP-pool range deterministically. These functions reproduce that
so a manually-built spec lays out identically to an automated one.
"""

import ipaddress


def first_usable(cidr):
    """First usable host address (network + 1). Used as the gateway."""
    net = ipaddress.ip_network(cidr, strict=False)
    return str(net.network_address + 1)


def tenth_host(cidr):
    """Network address + 10 — the start of the IP-pool range."""
    net = ipaddress.ip_network(cidr, strict=False)
    return str(net.network_address + 10)


def sixth_from_end(cidr):
    """Broadcast address - 5 — the end of the IP-pool range."""
    net = ipaddress.ip_network(cidr, strict=False)
    return str(net.broadcast_address - 5)


def vsp_pool(cidr):
    """Return the 21-address VSP pool, fitting `/25` and `/26` networks."""
    net = ipaddress.ip_network(cidr, strict=True)
    if net.version != 4 or not 16 <= net.prefixlen <= 28:
        raise ValueError(f"EVS IPv4 subnet required, got {cidr!r}")
    last_host_offset = net.num_addresses - 2
    if last_host_offset >= 100:
        start, end = 80, 100
    elif last_host_offset >= 60:
        start, end = 40, 60
    else:
        raise ValueError(f"subnet is too small for the VSP address pool: {cidr!r}")
    return str(net.network_address + start), str(net.network_address + end)
