import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]


class RunmeTests(unittest.TestCase):
    def test_uploads_blueprint_and_only_uploads_missing_installer_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bindir = root / "bin"
            bindir.mkdir()
            shutil.copy2(PROJECT_DIR / "runme.sh", root / "runme.sh")
            shutil.copy2(
                PROJECT_DIR / "evs-deployment-orchestrator.yaml",
                root / "evs-deployment-orchestrator.yaml",
            )
            for name in ("blueprint.yaml", "installer.ova", "ovftool.zip"):
                (root / name).write_text("test fixture")

            fake_aws = bindir / "aws"
            fake_aws.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "args = sys.argv[1:]\n"
                "with open(os.environ['AWS_ARGS_LOG'], 'a') as log:\n"
                "    log.write(json.dumps(args) + '\\n')\n"
                "if args[:2] == ['s3api', 'head-object']:\n"
                "    key = args[args.index('--key') + 1]\n"
                "    sys.exit(0 if key == os.environ['EXISTING_KEY'] else 255)\n"
            )
            fake_aws.chmod(0o755)

            call_log = root / "aws-calls.jsonl"
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
                EXISTING_KEY="installer.ova",
                AWS_ARGS_LOG=str(call_log),
                PATH=f"{bindir}:{env['PATH']}",
            )
            result = subprocess.run(
                ["bash", str(root / "runme.sh")],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            calls = [json.loads(line) for line in call_log.read_text().splitlines()]
            blueprint_upload = [
                "s3", "cp", "blueprint.yaml", "s3://test-bucket/blueprint.yaml",
                "--region", "us-east-1",
            ]
            self.assertIn(blueprint_upload, calls)
            self.assertIn(["s3api", "head-object", "--bucket", "test-bucket", "--key", "installer.ova", "--region", "us-east-1"], calls)
            self.assertIn(["s3api", "head-object", "--bucket", "test-bucket", "--key", "ovftool.zip", "--region", "us-east-1"], calls)
            self.assertNotIn(["s3", "cp", "installer.ova", "s3://test-bucket/", "--region", "us-east-1"], calls)
            self.assertIn(["s3", "cp", "ovftool.zip", "s3://test-bucket/", "--region", "us-east-1"], calls)
            create_stack = next(
                index for index, args in enumerate(calls)
                if args[:2] == ["cloudformation", "create-stack"]
            )
            self.assertLess(calls.index(blueprint_upload), create_stack)
            self.assertLess(
                calls.index(["s3", "cp", "ovftool.zip", "s3://test-bucket/", "--region", "us-east-1"]),
                create_stack,
            )


if __name__ == "__main__":
    unittest.main()
