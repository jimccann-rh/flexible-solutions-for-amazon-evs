# Dynamic EVS CIDR Allocation — Design

## Goal

Create an isolated refactored copy of the Amazon EVS deployment repository that replaces the hard-coded `/16` VPC, `/24` subnets, and prefix-derived addresses with configurable CIDRs and subnet sizes. The existing repository checkout remains unchanged.

The initial example must match the current AWS EVS layout:

- VPC/underlay CIDR: `10.28.32.0/21`
- EVS overlay pool: `10.28.40.0/21`
- Externally managed TGW/core-router aggregate: `10.28.32.0/20`
- Subnet prefix length: `/25`, derived using four additional subnet bits from the VPC prefix
- The aggregate covers the separate underlay and overlay ranges.

CIDR and subnet size are inputs, not implementation constants. A future `/22` VPC can use `/26` subnets; validation rejects combinations that cannot fit the required allocations. TGW attachments and TGW routes remain outside this repository's responsibility.

## Network ownership and flow

1. CloudFormation creates the VPC and two subnets: Service Access and Public. The Runner instance continues to launch in the Public subnet, matching the current template. The Service Access subnet remains associated with its own route table; the Public subnet remains the NAT Gateway placement.
2. The orchestrator supplies the ten EVS VLAN CIDRs in `initialVlans`. AWS EVS creates the corresponding `DoNotDelete-EVS-*` subnet resources; the refactored code must not create those subnet resources itself.
3. The EVS VPC Route Server continues to propagate learned overlay routes into the configured VPC route tables. It does not create or manage the external TGW attachment or the TGW's aggregate route.
4. The existing `internal-routing-TG-RH` subnet is customer-managed, not an allocation owned by this code. It must not be consumed by the generated allocations.

## Configuration and deterministic allocation

Expose full CIDRs through CloudFormation parameters and `network` configuration. The Python allocator's `subnet_cidr_bits` is the number of bits added to the VPC prefix; derive the effective prefix as `vpc_cidr.prefixlen + subnet_cidr_bits`. Thus `4` yields `/25` from a `/21` VPC and `/26` from a `/22` VPC. CloudFormation's `Fn::Cidr` instead takes host bits, so bootstrap uses `SubnetHostBits` (`7` for `/25`, `6` for `/26`) and derives the relative `subnet_cidr_bits` for the downloaded blueprint. This keeps CloudFormation subnets and orchestrator allocations aligned.

Default settings:

```yaml
network:
  vpc_cidr: 10.28.32.0/21
  subnet_cidr_bits: 4
  overlay_cidr: 10.28.40.0/21
  tgw_aggregate_cidr: 10.28.32.0/20
  external_reserved_cidrs:
    - 10.28.38.0/25 # internal-routing-TG-RH; not managed by this code
```

The default plan assigns the first two subnet blocks to CloudFormation and the next ten to EVS, in this stable role order:

| Block | Role | Default CIDR |
|---:|---|---|
| 0 | Service Access | `10.28.32.0/25` |
| 1 | Public | `10.28.32.128/25` |
| 2 | `vmkManagement` | `10.28.33.0/25` |
| 3 | `vmManagement` | `10.28.33.128/25` |
| 4 | `nsxUplink` | `10.28.34.0/25` |
| 5 | `vMotion` | `10.28.34.128/25` |
| 6 | `vSan` | `10.28.35.0/25` |
| 7 | `vTep` | `10.28.35.128/25` |
| 8 | `edgeVTep` | `10.28.36.0/25` |
| 9 | `hcx` | `10.28.36.128/25` |
| 10 | `expansionVlan1` | `10.28.37.0/25` |
| 11 | `expansionVlan2` | `10.28.37.128/25` |

The known external internal-routing subnet is `10.28.38.0/25`; it remains outside the generated allocations. `external_reserved_cidrs` is configurable. The first two blocks are fixed for Service Access and Public; reject input if a reserved range overlaps either. Allocate each EVS VLAN from the next available block in role order, skipping reserved blocks, and fail if fewer than ten blocks remain.

CloudFormation uses its native `Fn::Cidr` function with `SubnetHostBits` to obtain the first two subnet blocks. Python uses the standard-library `ipaddress` module with the derived relative `subnet_cidr_bits` to validate the same ordered subnet plan and provide the ten EVS VLAN CIDRs. A runnable check must assert that both paths agree on the default `/21` + `/25` and `/22` + `/26` allocations.

## Validation and address derivation

Fail before provisioning if:

- CIDR inputs are malformed or not canonical networks.
- `subnet_cidr_bits` is not a valid subdivision of the VPC CIDR (resulting in an unsupported subnet prefix), or the VPC does not contain enough blocks for the two bootstrap and ten EVS allocations after external reservations.
- Any generated allocation overlaps another generated or configured external-reserved range.
- The overlay overlaps the VPC CIDR or either the VPC or overlay is outside the configured TGW aggregate.
- A required host or resolver address does not fit its selected subnet.

Remove the old `CidrPrefix` derivations. Use the full VPC CIDR for security-group ingress. Derive EVS VLAN CIDRs from their allocation blocks, and derive existing appliance host offsets relative to each VLAN network rather than concatenating octets. Select Route 53 Resolver addresses as the first two AWS-assignable hosts in the Service Access subnet (network address +4 and +5), and use those same addresses in DHCP options and generated deployment configuration.

Make pools fit each VLAN CIDR instead of retaining `/24`-only offsets:

- Edge TEP pool starts at network offset `+6` and ends at the last usable address in its CIDR (e.g. `+126` for `/25`, `+62` for `/26`).
- The VSP management pool keeps offsets `+80..+100` when they fit. For `/26`, use `+40..+60` (21 addresses, after the existing management allocations through `+35`). Reject a subnet size if the fixed appliance addresses or required VSP pool cannot fit.

The `/16` values used for RFC 1918 BGP filtering are unrelated to VPC sizing and remain unchanged. Example text may show a CIDR, but must not drive deployment behavior.

## Scope of the new copy

Create a sibling Git worktree at `solutions-for-amazon-evs-refactored` on branch `refactor/dynamic-evs-cidrs`. Modify that checkout only. Update the CloudFormation template, orchestrator configuration and CIDR construction, relevant spec-generator fallbacks, example blueprints, and deployment/networking documentation. Do not add TGW attachment or TGW route management.

## Verification

Add one small runnable standard-library check for the network allocator and validation. It must cover the default `/21` + `/25` role map, a `/22` + `/26` configuration, insufficient subnet capacity, overlap/out-of-range input, calculated host/resolver addresses, and Edge TEP/VSP pools for both supported subnet sizes. Also verify the generated CloudFormation subnet CIDRs match Service Access and Public block indices. Run the repository's Python lint workflow if `ruff` is available; the current baseline checkout has no test files and `ruff` is not installed.
