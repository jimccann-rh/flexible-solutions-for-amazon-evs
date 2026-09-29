import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_DIR / "runme-byovpc.sh"
BYOVPC_VALUES = {
    "VPC_ID": "vpc-12345678",
    "RUNNER_SUBNET": "subnet-runner",
    "SERVICE_SUBNET": "subnet-service",
    "SERVICE_RT": "rtb-service",
    "PUBLIC_SUBNET": "subnet-public",
}


class RunmeByovpcTests(unittest.TestCase):
    def run_script(self, root, missing=None):
        bindir = root / "bin"
        bindir.mkdir()
        shutil.copy2(SCRIPT, root / "runme-byovpc.sh")
        shutil.copy2(
            PROJECT_DIR / "evs-deployment-orchestrator.yaml",
            root / "evs-deployment-orchestrator.yaml",
        )
        for name in ("blueprint.yaml", "installer.ova", "ovftool.zip"):
            (root / name).write_text("test fixture")

        call_log = root / "aws-calls.jsonl"
        fake_aws = bindir / "aws"
        fake_aws.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "with open(os.environ['AWS_ARGS_LOG'], 'a') as log:\n"
            "    log.write(json.dumps(args) + '\\n')\n"
            "if args[:2] == ['s3api', 'head-object']:\n"
            "    sys.exit(255)\n"
        )
        fake_aws.chmod(0o755)

        env = os.environ.copy()
        env.update(
            BLUEPRINT="blueprint.yaml",
            OVA="installer.ova",
            OVF="ovftool.zip",
            BUCKET="test-bucket",
            REGION="us-east-1",
            STACK_NAME="test-stack",
            AZ="us-east-1a",
            SECRET_NAME="test-secret",
            AWS_ARGS_LOG=str(call_log),
            PATH=f"{bindir}:{env['PATH']}",
            **BYOVPC_VALUES,
        )
        for variable in missing or ():
            env.pop(variable)

        result = subprocess.run(
            ["bash", str(root / "runme-byovpc.sh")],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        calls = (
            [json.loads(line) for line in call_log.read_text().splitlines()]
            if call_log.exists()
            else []
        )
        return result, calls

    def test_requires_every_byo_vpc_value_before_aws_calls(self):
        for variable in BYOVPC_VALUES:
            with self.subTest(variable=variable), tempfile.TemporaryDirectory() as temp_dir:
                result, calls = self.run_script(Path(temp_dir), missing=[variable])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"set {variable}", result.stderr)
                self.assertEqual(calls, [])

    def test_passes_byo_vpc_values_to_cloudformation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result, calls = self.run_script(Path(temp_dir))
            self.assertEqual(result.returncode, 0, result.stderr)

        create_args = next(
            args for args in calls if args[:2] == ["cloudformation", "create-stack"]
        )
        for parameter in (
            "ParameterKey=ExistingVpcId,ParameterValue=vpc-12345678",
            "ParameterKey=ExistingRunnerSubnetId,ParameterValue=subnet-runner",
            "ParameterKey=ExistingServiceAccessSubnetId,ParameterValue=subnet-service",
            "ParameterKey=ExistingServiceAccessRouteTableId,ParameterValue=rtb-service",
            "ParameterKey=ExistingPublicSubnetId,ParameterValue=subnet-public",
        ):
            self.assertIn(parameter, create_args)


if __name__ == "__main__":
    unittest.main()
