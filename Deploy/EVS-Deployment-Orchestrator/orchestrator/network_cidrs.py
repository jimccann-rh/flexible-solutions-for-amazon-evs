"""CIDR allocation and address derivation for EVS deployments."""

import ipaddress
from collections.abc import Iterable, Mapping

VLAN_ROLES = (
    "vmkManagement",
    "vmManagement",
    "nsxUplink",
    "vMotion",
    "vSan",
    "vTep",
    "edgeVTep",
    "hcx",
    "expansionVlan1",
    "expansionVlan2",
)


def _ipv4_network(cidr: str) -> ipaddress.IPv4Network:
    network = ipaddress.ip_network(cidr, strict=True)
    if not isinstance(network, ipaddress.IPv4Network):
        raise TypeError(f"IPv4 CIDR required, got {cidr!r}")
    return network


def _evs_subnet(cidr: str) -> ipaddress.IPv4Network:
    network = _ipv4_network(cidr)
    if not 16 <= network.prefixlen <= 28:
        raise ValueError(f"EVS subnet prefix must be between /16 and /28: {cidr!r}")
    return network


def allocate_subnets(
    vpc_cidr: str,
    subnet_cidr_bits: int,
    external_reserved_cidrs: Iterable[str] = (),
) -> dict:
    """Allocate bootstrap subnets then the ten EVS VLANs in stable order."""
    vpc = _ipv4_network(vpc_cidr)
    if not 16 <= vpc.prefixlen <= 28:
        raise ValueError(f"VPC prefix must be between /16 and /28: {vpc_cidr!r}")
    if type(subnet_cidr_bits) is not int or subnet_cidr_bits < 1:
        raise ValueError("subnet_cidr_bits must be a positive integer")

    subnet_prefix = vpc.prefixlen + subnet_cidr_bits
    if subnet_prefix > 28:
        raise ValueError("resulting subnet prefix must not exceed /28")
    blocks = list(vpc.subnets(new_prefix=subnet_prefix))
    if len(blocks) < len(VLAN_ROLES) + 2:
        raise ValueError("VPC does not contain enough subnet blocks for EVS")

    reserved = [_ipv4_network(cidr) for cidr in external_reserved_cidrs]
    bootstrap = blocks[:2]
    if any(block.overlaps(item) for block in bootstrap for item in reserved):
        raise ValueError("external reservation overlaps a bootstrap subnet")

    vlans = {}
    for block in blocks[2:]:
        if any(block.overlaps(item) for item in reserved):
            continue
        vlans[VLAN_ROLES[len(vlans)]] = str(block)
        if len(vlans) == len(VLAN_ROLES):
            break
    if len(vlans) != len(VLAN_ROLES):
        raise ValueError("VPC does not contain enough unreserved subnet blocks for EVS")

    return {
        "service_access": str(bootstrap[0]),
        "public": str(bootstrap[1]),
        "vlans": vlans,
    }


def validate_route_ranges(vpc_cidr: str, overlay_cidr: str, tgw_aggregate_cidr: str) -> None:
    """Require disjoint IPv4 VPC/overlay ranges inside the TGW aggregate."""
    vpc = _ipv4_network(vpc_cidr)
    overlay = _ipv4_network(overlay_cidr)
    aggregate = _ipv4_network(tgw_aggregate_cidr)
    if vpc.overlaps(overlay):
        raise ValueError("VPC and overlay CIDRs must not overlap")
    if not aggregate.supernet_of(vpc) and aggregate != vpc:
        raise ValueError("TGW aggregate must contain the VPC CIDR")
    if not aggregate.supernet_of(overlay) and aggregate != overlay:
        raise ValueError("TGW aggregate must contain the overlay CIDR")


def build_network_plan(network: Mapping[str, object]) -> dict:
    """Validate a blueprint network block and return its allocation plan."""
    if not isinstance(network, Mapping):
        raise TypeError("network configuration must be a mapping")
    required = (
        "vpc_cidr",
        "subnet_cidr_bits",
        "overlay_cidr",
        "tgw_aggregate_cidr",
        "external_reserved_cidrs",
    )
    missing = [key for key in required if key not in network]
    if missing:
        raise ValueError(f"network configuration missing: {', '.join(missing)}")

    reserved = network["external_reserved_cidrs"]
    if isinstance(reserved, (str, bytes)) or not isinstance(reserved, Iterable):
        raise TypeError("network.external_reserved_cidrs must be a sequence of CIDRs")

    vpc_cidr = network["vpc_cidr"]
    overlay_cidr = network["overlay_cidr"]
    aggregate_cidr = network["tgw_aggregate_cidr"]
    validate_route_ranges(vpc_cidr, overlay_cidr, aggregate_cidr)
    return allocate_subnets(vpc_cidr, network["subnet_cidr_bits"], reserved)


def ip_at_offset(cidr: str, offset: int) -> str:
    """Return a usable host at an offset from an IPv4 network address."""
    network = _ipv4_network(cidr)
    if type(offset) is not int or not 0 < offset < network.num_addresses - 1:
        raise ValueError(f"host offset {offset!r} is not usable in {cidr!r}")
    return str(network.network_address + offset)


def installer_network_settings(network_config: Mapping[str, object]) -> dict[str, object]:
    """Derive the VCF Installer's fixed host, gateway, mask, and DNS values."""
    plan = build_network_plan(network_config)
    management_cidr = plan["vlans"]["vmManagement"]
    management_network = _ipv4_network(management_cidr)
    return {
        "installer_ip": ip_at_offset(management_cidr, 12),
        "gateway": ip_at_offset(management_cidr, 1),
        "netmask": str(management_network.netmask),
        "dns_servers": resolver_ips(plan["service_access"]),
    }


def resolver_ips(service_access_cidr: str) -> tuple[str, str]:
    return ip_at_offset(service_access_cidr, 4), ip_at_offset(service_access_cidr, 5)


def edge_tep_pool(cidr: str) -> tuple[str, str]:
    network = _evs_subnet(cidr)
    return ip_at_offset(cidr, 6), str(network.broadcast_address - 1)


def vsp_pool(cidr: str) -> tuple[str, str]:
    network = _evs_subnet(cidr)
    last_host_offset = network.num_addresses - 2
    if last_host_offset >= 100:
        start, end = 80, 100
    elif last_host_offset >= 60:
        start, end = 40, 60
    else:
        raise ValueError(f"subnet is too small for the VSP address pool: {cidr!r}")
    return ip_at_offset(cidr, start), ip_at_offset(cidr, end)
