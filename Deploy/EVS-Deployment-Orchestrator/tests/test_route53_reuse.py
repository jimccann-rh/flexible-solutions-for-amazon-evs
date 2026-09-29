import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parents[1]
ORCHESTRATOR_DIR = PROJECT_DIR / "orchestrator"
_IMPORT_DIR = tempfile.TemporaryDirectory()
Path(_IMPORT_DIR.name, ".evs_orchestrator_deps_installed").touch()
sys.path.insert(0, str(ORCHESTRATOR_DIR))
_old_cwd = os.getcwd()
try:
    os.chdir(_IMPORT_DIR.name)
    import deploy_orchestrator
finally:
    os.chdir(_old_cwd)


class FakeRoute53:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def list_hosted_zones_by_vpc(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages.pop(0)


class AwsConfigRoute53Tests(unittest.TestCase):
    def config(self, *, create_vpc=False):
        return {
            "_bootstrap_mode": True,
            "aws": {"region": "us-east-1", "bootstrap_stack_name": "test"},
            "vpc": {
                "create": create_vpc,
                "id": "vpc-test",
                "service_access_subnet_id": "subnet-service",
                "service_access_route_table_id": "rtb-service",
                "public_subnet_id": "subnet-public",
            },
            "network": {
                "vpc_cidr": "10.28.32.0/21",
                "subnet_cidr_bits": 4,
                "overlay_cidr": "10.28.40.0/21",
                "tgw_aggregate_cidr": "10.28.32.0/20",
                "external_reserved_cidrs": ["10.28.38.0/25"],
            },
            "dns": {"fqdn": "vci.devcluster.openshift.com"},
            "evs": {"environment_name": "test", "vcf_version": "9.1.0"},
            "hostnames": {"esxi": ["esxi01", "esxi02", "esxi03"]},
            "hcx": {"enabled": False},
        }

    def run_stage(self, config, route53, *, stack_resources=None, stack_status="CREATE_COMPLETE"):
        class NoUpdates(Exception):
            pass

        class FakeCloudFormation:
            class exceptions:
                ClientError = NoUpdates

            def __init__(self):
                self.template = None
                self.describe_calls = 0
                self.update_calls = 0
                self.delete_calls = 0
                self.stack_resources = stack_resources or []

            def describe_stacks(self, **_kwargs):
                self.describe_calls += 1
                return {"Stacks": [{
                    "StackStatus": stack_status,
                    "Outputs": [
                        {"OutputKey": "SecurityGroupId", "OutputValue": "sg-test"},
                        {"OutputKey": "RouteServerEndpoint01Ip", "OutputValue": "10.28.36.10"},
                        {"OutputKey": "RouteServerEndpoint02Ip", "OutputValue": "10.28.36.11"},
                        {"OutputKey": "KeyName", "OutputValue": "key-test"},
                        {"OutputKey": "ForwardZoneId", "OutputValue": "Z-FORWARD"},
                    ],
                }]}

            def describe_stack_resources(self, **_kwargs):
                return {"StackResources": self.stack_resources}

            def update_stack(self, **kwargs):
                self.update_calls += 1
                self.template = json.loads(kwargs["TemplateBody"])
                raise NoUpdates("No updates are to be performed")

            def delete_stack(self, **_kwargs):
                self.delete_calls += 1

            def create_stack(self, **kwargs):
                self.template = json.loads(kwargs["TemplateBody"])

            def get_waiter(self, _name):
                return type("Waiter", (), {"wait": lambda *_args, **_kwargs: None})()

        cfn = FakeCloudFormation()
        self._last_cfn = cfn

        class FakeSession:
            def __init__(self, **_kwargs):
                pass

            def client(self, service):
                if service == "route53":
                    return route53
                raise AssertionError(f"unexpected AWS client: {service}")

        with (
            patch.object(deploy_orchestrator, "_aws_config_cfn", return_value=cfn),
            patch.object(deploy_orchestrator.boto3, "Session", FakeSession),
            patch.object(deploy_orchestrator.socket, "gethostbyname", return_value="10.0.0.1"),
        ):
            result = deploy_orchestrator.stage_aws_config(config, object())
        return result, cfn

    def test_byo_vpc_reuses_associated_forward_and_reverse_zones(self):
        route53 = FakeRoute53([
            {
                "HostedZoneSummaries": [{
                    "HostedZoneId": "Z-FORWARD",
                    "Name": "vci.devcluster.openshift.com.",
                }],
                "NextToken": "next-page",
            },
            {
                "HostedZoneSummaries": [
                    {"HostedZoneId": "Z-REVERSE", "Name": "28.10.in-addr.arpa."},
                    {"HostedZoneId": "Z-OTHER", "Name": "other.example."},
                ],
            },
        ])
        _, cfn = self.run_stage(self.config(), route53)

        self.assertEqual(route53.calls, [
            {"VPCId": "vpc-test", "VPCRegion": "us-east-1"},
            {"VPCId": "vpc-test", "VPCRegion": "us-east-1", "NextToken": "next-page"},
        ])
        resources = cfn.template["Resources"]
        self.assertNotIn("ForwardZone", resources)
        self.assertNotIn("ReverseZone", resources)
        self.assertEqual(resources["FwdRecord00"]["Properties"]["HostedZoneId"], "Z-FORWARD")
        self.assertEqual(resources["PtrRecord00"]["Properties"]["HostedZoneId"], "Z-REVERSE")
        self.assertEqual(cfn.template["Outputs"]["ForwardZoneId"]["Value"], "Z-FORWARD")

    def test_byo_vpc_fails_before_cloudformation_when_a_zone_is_missing(self):
        route53 = FakeRoute53([{
            "HostedZoneSummaries": [{
                "HostedZoneId": "Z-FORWARD",
                "Name": "vci.devcluster.openshift.com.",
            }],
        }])
        with self.assertRaisesRegex(RuntimeError, "28.10.in-addr.arpa"):
            self.run_stage(self.config(), route53)
        self.assertEqual(self._last_cfn.describe_calls, 0)

    def test_byo_vpc_refuses_to_remove_cloudformation_managed_zones(self):
        route53 = FakeRoute53([{
            "HostedZoneSummaries": [
                {"HostedZoneId": "Z-FORWARD", "Name": "vci.devcluster.openshift.com."},
                {"HostedZoneId": "Z-REVERSE", "Name": "28.10.in-addr.arpa."},
            ],
        }])
        owned_zone = {
            "LogicalResourceId": "ForwardZone",
            "PhysicalResourceId": "Z-OLD-FORWARD",
            "ResourceStatus": "CREATE_COMPLETE",
        }
        with self.assertRaisesRegex(RuntimeError, "ForwardZone"):
            self.run_stage(self.config(), route53, stack_resources=[owned_zone])
        self.assertEqual(self._last_cfn.update_calls, 0)

    def test_byo_vpc_refuses_to_delete_stack_that_still_owns_zones(self):
        route53 = FakeRoute53([{
            "HostedZoneSummaries": [
                {"HostedZoneId": "Z-FORWARD", "Name": "vci.devcluster.openshift.com."},
                {"HostedZoneId": "Z-REVERSE", "Name": "28.10.in-addr.arpa."},
            ],
        }])
        owned_zone = {
            "LogicalResourceId": "ReverseZone",
            "PhysicalResourceId": "Z-OLD-REVERSE",
            "ResourceStatus": "CREATE_COMPLETE",
        }
        with self.assertRaisesRegex(RuntimeError, "ReverseZone"):
            self.run_stage(
                self.config(), route53, stack_resources=[owned_zone], stack_status="ROLLBACK_FAILED",
            )
        self.assertEqual(self._last_cfn.delete_calls, 0)

    def test_new_vpc_still_creates_both_zones_without_route53_lookup(self):
        route53 = FakeRoute53([])
        _, cfn = self.run_stage(self.config(create_vpc=True), route53)
        self.assertEqual(route53.calls, [])
        self.assertIn("ForwardZone", cfn.template["Resources"])
        self.assertIn("ReverseZone", cfn.template["Resources"])


if __name__ == "__main__":
    unittest.main()
