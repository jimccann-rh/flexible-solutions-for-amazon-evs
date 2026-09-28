import ipaddress
import re
import subprocess
import sys
import unittest
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "spec-generator"))
sys.path.insert(1, str(PROJECT_DIR / "orchestrator"))

import builder as standalone_builder  # noqa: E402
import cidr as standalone_cidr  # noqa: E402
from evs_environment.edge_cluster_spec import EdgeClusterSpec  # noqa: E402
from network_cidrs import (  # noqa: E402
    allocate_subnets,
    build_network_plan,
    edge_tep_pool,
    installer_network_settings,
    ip_at_offset,
    resolver_ips,
    validate_route_ranges,
    vsp_pool,
)


class CloudFormationLoader(yaml.SafeLoader):
    pass


def _construct_intrinsic(loader, suffix, node):
    if isinstance(node, ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    elif isinstance(node, MappingNode):
        value = loader.construct_mapping(node, deep=True)
    else:
        raise TypeError(f"unsupported CloudFormation YAML node: {node!r}")
    key = "Ref" if suffix == "Ref" else f"Fn::{suffix}"
    return {key: value}


CloudFormationLoader.add_multi_constructor("!", _construct_intrinsic)


class NetworkCidrTests(unittest.TestCase):
    def test_build_network_plan_validates_and_allocates_the_network_block(self):
        plan = build_network_plan({
            "vpc_cidr": "10.28.32.0/21",
            "subnet_cidr_bits": 4,
            "overlay_cidr": "10.28.40.0/21",
            "tgw_aggregate_cidr": "10.28.32.0/20",
            "external_reserved_cidrs": ["10.28.38.0/25"],
        })
        self.assertEqual(plan["service_access"], "10.28.32.0/25")
        self.assertEqual(plan["vlans"]["vmManagement"], "10.28.33.128/25")
        self.assertEqual(ip_at_offset(plan["vlans"]["vmkManagement"], 11), "10.28.33.11")
        self.assertEqual(ip_at_offset(plan["vlans"]["vmManagement"], 10), "10.28.33.138")

        with self.assertRaises(ValueError):
            build_network_plan({"vpc_cidr": "10.28.32.0/21"})
        with self.assertRaises(ValueError):
            build_network_plan({
                "vpc_cidr": "10.28.32.0/21",
                "subnet_cidr_bits": 4,
                "overlay_cidr": "10.28.34.0/24",
                "tgw_aggregate_cidr": "10.28.32.0/20",
                "external_reserved_cidrs": [],
            })

    def test_cloudformation_subnets_use_host_bits_and_preserve_relative_allocator_bits(self):
        path = Path(__file__).resolve().parents[1] / "evs-deployment-orchestrator.yaml"
        template = yaml.load(path.read_text(), Loader=CloudFormationLoader)
        parameters = template["Parameters"]

        self.assertIn("VpcCidr", parameters)
        self.assertEqual(parameters["VpcCidr"]["Default"], "10.28.32.0/21")
        self.assertEqual(parameters["SubnetHostBits"]["Default"], 7)
        self.assertEqual(parameters["SubnetHostBits"]["AllowedValues"], [6, 7])
        self.assertEqual(parameters["ExternalReservedCidrs"]["Default"], "10.28.38.0/25")
        self.assertNotIn("CidrPrefix", parameters)
        self.assertEqual(template["Resources"]["Vpc"]["Properties"]["CidrBlock"], {"Ref": "VpcCidr"})

        for subnet, index in (("ServiceAccessSubnet", 0), ("PublicSubnet", 1)):
            with self.subTest(subnet=subnet):
                self.assertEqual(
                    template["Resources"][subnet]["Properties"]["CidrBlock"],
                    {"Fn::Select": [
                        index,
                        {"Fn::Cidr": [{"Ref": "VpcCidr"}, 2, {"Ref": "SubnetHostBits"}]},
                    ]},
                )

        for vpc_cidr, host_bits, expected in (
            ("10.28.32.0/21", 7, ("10.28.32.0/25", "10.28.32.128/25")),
            ("10.28.32.0/22", 6, ("10.28.32.0/26", "10.28.32.64/26")),
        ):
            with self.subTest(vpc_cidr=vpc_cidr, host_bits=host_bits):
                vpc = ipaddress.ip_network(vpc_cidr)
                subnet_prefix = 32 - host_bits
                actual = tuple(str(block) for block in list(vpc.subnets(new_prefix=subnet_prefix))[:2])
                relative_bits = subnet_prefix - vpc.prefixlen
                plan = allocate_subnets(vpc_cidr, relative_bits)
                self.assertEqual(actual, expected)
                self.assertEqual((plan["service_access"], plan["public"]), expected)

        self.assertIn(
            '"subnet_cidr_bits": 32 - int("${SubnetHostBits}") - int("${VpcCidr}".split("/")[-1]),',
            path.read_text(),
        )

    def test_installer_addresses_follow_the_allocated_subnet_size(self):
        default = installer_network_settings({
            "vpc_cidr": "10.28.32.0/21",
            "subnet_cidr_bits": 4,
            "overlay_cidr": "10.28.40.0/21",
            "tgw_aggregate_cidr": "10.28.32.0/20",
            "external_reserved_cidrs": ["10.28.38.0/25"],
        })
        self.assertEqual(default, {
            "installer_ip": "10.28.33.140",
            "gateway": "10.28.33.129",
            "netmask": "255.255.255.128",
            "dns_servers": ("10.28.32.4", "10.28.32.5"),
        })

        small = installer_network_settings({
            "vpc_cidr": "10.28.32.0/22",
            "subnet_cidr_bits": 4,
            "overlay_cidr": "10.28.40.0/21",
            "tgw_aggregate_cidr": "10.28.32.0/20",
            "external_reserved_cidrs": [],
        })
        self.assertEqual(small["installer_ip"], "10.28.32.204")
        self.assertEqual(small["gateway"], "10.28.32.193")
        self.assertEqual(small["netmask"], "255.255.255.192")

    def test_edge_cluster_pool_ends_at_the_last_usable_tep_host(self):
        for cidr, expected_end in (
            ("10.28.36.0/25", "10.28.36.126"),
            ("10.28.36.0/26", "10.28.36.62"),
        ):
            with self.subTest(cidr=cidr):
                builder = EdgeClusterSpec(
                    Path("edge-cluster.json"),
                    {"initialVlans": {"edgeVTep": {"cidr": cidr}}},
                )
                self.assertEqual(builder._build_ip_pool()["rangeEnd"], expected_end)

    def test_standalone_vsp_pool_matches_core_for_each_subnet_size(self):
        for cidr, expected in (
            ("10.28.33.128/25", ("10.28.33.208", "10.28.33.228")),
            ("10.28.33.128/26", ("10.28.33.168", "10.28.33.188")),
        ):
            with self.subTest(cidr=cidr):
                self.assertEqual(standalone_cidr.vsp_pool(cidr), expected)
                self.assertEqual(vsp_pool(cidr), expected)
                spec = standalone_builder._build_vsp_cluster_spec({
                    "hostnames": {},
                    "fqdn": "example.test",
                    "vlan_cidrs": {"vmManagement": cidr},
                })
                self.assertEqual(
                    spec["ipv4Pool"]["ipRange"],
                    {"startIpAddress": expected[0], "endIpAddress": expected[1]},
                )

        with self.assertRaises(ValueError):
            standalone_cidr.vsp_pool("10.28.33.128/27")
        with self.assertRaises(ValueError):
            standalone_builder._build_vsp_cluster_spec({
                "hostnames": {}, "fqdn": "example.test", "vlan_cidrs": {},
            })

    def test_default_plan_matches_the_documented_role_map(self):
        plan = allocate_subnets("10.28.32.0/21", 4, ["10.28.38.0/25"])

        self.assertEqual(plan["service_access"], "10.28.32.0/25")
        self.assertEqual(plan["public"], "10.28.32.128/25")
        self.assertEqual(plan["vlans"], {
            "vmkManagement": "10.28.33.0/25",
            "vmManagement": "10.28.33.128/25",
            "nsxUplink": "10.28.34.0/25",
            "vMotion": "10.28.34.128/25",
            "vSan": "10.28.35.0/25",
            "vTep": "10.28.35.128/25",
            "edgeVTep": "10.28.36.0/25",
            "hcx": "10.28.36.128/25",
            "expansionVlan1": "10.28.37.0/25",
            "expansionVlan2": "10.28.37.128/25",
        })

    def test_smaller_vpc_fits_and_reservations_are_skipped(self):
        plan = allocate_subnets("10.28.32.0/22", 4)
        allocated = [plan["service_access"], plan["public"], *plan["vlans"].values()]
        self.assertEqual(len(set(allocated)), 12)
        self.assertTrue(all(cidr.endswith("/26") for cidr in allocated))

        skipped = allocate_subnets("10.28.32.0/21", 4, ["10.28.33.0/25"])
        self.assertEqual(skipped["vlans"]["vmkManagement"], "10.28.33.128/25")

        for reserved in ("10.28.32.0/25", "10.28.32.128/25"):
            with self.subTest(reserved=reserved), self.assertRaises(ValueError):
                allocate_subnets("10.28.32.0/21", 4, [reserved])

        with self.assertRaises(ValueError):
            allocate_subnets("10.28.32.0/22", 3)

    def test_route_ranges_must_be_disjoint_and_inside_the_aggregate(self):
        validate_route_ranges("10.28.32.0/21", "10.28.40.0/21", "10.28.32.0/20")

        with self.assertRaises(ValueError):
            validate_route_ranges("10.28.32.0/21", "10.28.34.0/24", "10.28.32.0/20")
        with self.assertRaises(ValueError):
            validate_route_ranges("10.28.32.0/21", "10.28.40.0/21", "10.28.32.0/21")

    def test_resolver_and_offsets_stay_on_usable_hosts(self):
        self.assertEqual(resolver_ips("10.28.32.0/25"), ("10.28.32.4", "10.28.32.5"))
        self.assertEqual(ip_at_offset("10.28.33.128/26", 35), "10.28.33.163")
        with self.assertRaises(ValueError):
            ip_at_offset("10.28.33.128/26", 63)
        with self.assertRaises(ValueError):
            ip_at_offset("10.28.33.128/26", 0)

    def test_edge_tep_and_vsp_ranges_fit_each_supported_subnet(self):
        self.assertEqual(edge_tep_pool("10.28.36.0/25"), ("10.28.36.6", "10.28.36.126"))
        self.assertEqual(edge_tep_pool("10.28.36.0/26"), ("10.28.36.6", "10.28.36.62"))
        self.assertEqual(vsp_pool("10.28.33.128/25"), ("10.28.33.208", "10.28.33.228"))
        self.assertEqual(vsp_pool("10.28.33.128/26"), ("10.28.33.168", "10.28.33.188"))
        with self.assertRaises(ValueError):
            edge_tep_pool("10.28.36.0/29")
        with self.assertRaises(ValueError):
            vsp_pool("10.28.33.128/27")

    def test_noncanonical_or_unsupported_subnet_inputs_fail(self):
        for cidr, bits in (("10.28.32.1/21", 4), ("10.28.32.0/21", 8), ("bad-cidr", 4)):
            with self.subTest(cidr=cidr, bits=bits), self.assertRaises(ValueError):
                allocate_subnets(cidr, bits)
        with self.assertRaises(TypeError):
            allocate_subnets("2001:db8::/32", 4)


    def test_all_options_blueprint_documents_orchestrator_repo_url(self):
        path = Path(__file__).resolve().parents[1] / "blueprints/custom.all-options.example.yaml"
        text = path.read_text()
        self.assertIn(
            'orchestrator_repo_url: "https://github.com/jimccann-rh/flexible-solutions-for-amazon-evs.git"',
            text,
        )

    def test_runner_clones_blueprint_selected_repo_after_download(self):
        path = Path(__file__).resolve().parents[1] / "evs-deployment-orchestrator.yaml"
        template = yaml.load(path.read_text(), Loader=CloudFormationLoader)
        user_data = template["Resources"]["RunnerInstance"]["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"]
        script = user_data[0] if isinstance(user_data, list) else user_data
        download = 'retry aws s3 cp "s3://$BLUEPRINT_BUCKET/$BLUEPRINT_OBJ_KEY" ./blueprint.yaml'
        parse_url = 'ORCHESTRATOR_REPO_URL=$(python3.11 -c'
        validate_url = 'case "$ORCHESTRATOR_REPO_URL" in'
        clone = 'retry git clone "$ORCHESTRATOR_REPO_URL" src'
        copy_blueprint = "cp ./blueprint.yaml src/Deploy/EVS-Deployment-Orchestrator/orchestrator/blueprint.yaml"
        enter_orchestrator = "cd src/Deploy/EVS-Deployment-Orchestrator/orchestrator"

        self.assertIn(download, script)
        self.assertIn(parse_url, script)
        self.assertIn(validate_url, script)
        self.assertIn(clone, script)
        self.assertIn(copy_blueprint, script)
        self.assertIn(enter_orchestrator, script)
        self.assertLess(script.index("retry python3.11 -m pip install --quiet pyyaml"), script.index(parse_url))
        self.assertLess(script.index(download), script.index(parse_url))
        self.assertLess(script.index(parse_url), script.index(validate_url))
        self.assertLess(script.index(validate_url), script.index(clone))
        self.assertLess(script.index(clone), script.index(copy_blueprint))
        self.assertLess(script.index(copy_blueprint), script.index(enter_orchestrator))
        self.assertIn('get("orchestrator_repo_url", "")', script)
        self.assertIn("https://github.com/*", script)
        self.assertIn('*) fail "blueprint.orchestrator_repo_url must be an HTTPS GitHub URL"', script)
        self.assertNotIn("https://github.com/aws/solutions-for-amazon-evs.git", script)

    def test_runner_userdata_is_valid_shell_after_cloudformation_substitution(self):
        path = Path(__file__).resolve().parents[1] / "evs-deployment-orchestrator.yaml"
        template = yaml.load(path.read_text(), Loader=CloudFormationLoader)
        user_data = template["Resources"]["RunnerInstance"]["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"]
        script = user_data[0] if isinstance(user_data, list) else user_data
        script = re.sub(r"\$\{[^}]+\}", "placeholder", script)
        result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
