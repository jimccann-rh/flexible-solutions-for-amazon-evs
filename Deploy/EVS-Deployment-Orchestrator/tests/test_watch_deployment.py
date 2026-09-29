import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
WATCHER = PROJECT_DIR / "watch-deployment.sh"


class WatchDeploymentTests(unittest.TestCase):
    def test_once_reports_stack_environment_hosts_stage_and_logs_read_only(self):
        self.assertTrue(WATCHER.is_file(), "watch-deployment.sh has not been created")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bindir = root / "bin"
            bindir.mkdir()
            fake_aws = bindir / "aws"
            fake_aws.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "args = sys.argv[1:]\n"
                "with open(os.environ['AWS_CALLS_LOG'], 'a') as f:\n"
                "    f.write(json.dumps(args) + '\\n')\n"
                "if args[:2] == ['cloudformation', 'describe-stacks']:\n"
                "    print(json.dumps({'Stacks': [{'StackId': 'arn:aws:cloudformation:us-east-1:123456789012:stack/test-stack/abcd1234-aaaa', 'StackStatus': 'CREATE_COMPLETE', 'Outputs': [{'OutputKey': 'RunnerInstanceId', 'OutputValue': 'i-123'}]}]}))\n"
                "elif args[:2] == ['cloudformation', 'list-stacks']:\n"
                "    print(json.dumps({'StackSummaries': [{'StackName': 'test-stack-amazon-evs-9-1-0-0-infrastructure', 'StackStatus': 'CREATE_IN_PROGRESS'}, {'StackName': 'unrelated-stack', 'StackStatus': 'UPDATE_COMPLETE'}]}))\n"
                "elif args[:2] == ['evs', 'get-environment']:\n"
                "    print(json.dumps({'environment': {'environmentName': 'my-vcf-env', 'environmentState': 'CREATED', 'stateDetails': 'ready'}}))\n"
                "elif args[:2] == ['evs', 'list-environment-hosts']:\n"
                "    print('esxi01  CREATED\\nesxi02  CREATED')\n"
                "elif args[:2] == ['ec2', 'describe-tags']:\n"
                "    print('RUNNING')\n"
                "elif args[:2] == ['logs', 'filter-log-events']:\n"
                "    group = args[args.index('--log-group-name') + 1]\n"
                "    print('latest ' + group.rsplit('/', 1)[-1] + ' log event')\n"
                "else:\n"
                "    sys.exit('unexpected AWS call: ' + repr(args))\n"
            )
            fake_aws.chmod(0o755)
            call_log = root / "aws-calls.jsonl"
            env = os.environ.copy()
            env.update(
                STACK_NAME="test-stack",
                REGION="us-east-1",
                ENVIRONMENT_ID="env-test",
                AWS_CALLS_LOG=str(call_log),
                PATH=f"{bindir}:{env['PATH']}",
            )
            result = subprocess.run(
                ["bash", str(WATCHER), "--once"],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for text in (
                "CREATE_COMPLETE",
                "test-stack-amazon-evs-9-1-0-0-infrastructure",
                "CREATE_IN_PROGRESS",
                "my-vcf-env",
                "esxi01",
                "RUNNING",
                "bootstrap log event",
                "orchestrator log event",
            ):
                self.assertIn(text, result.stdout)
            self.assertNotIn("unrelated-stack", result.stdout)

            calls = [json.loads(line) for line in call_log.read_text().splitlines()]
            self.assertEqual(
                {(args[0], args[1]) for args in calls},
                {
                    ("cloudformation", "describe-stacks"),
                    ("cloudformation", "list-stacks"),
                    ("evs", "get-environment"),
                    ("evs", "list-environment-hosts"),
                    ("ec2", "describe-tags"),
                    ("logs", "filter-log-events"),
                },
            )

    def test_stack_status_is_reported_before_evs_environment_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bindir = Path(temp_dir) / "bin"
            bindir.mkdir()
            fake_aws = bindir / "aws"
            fake_aws.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "args = sys.argv[1:]\n"
                "if args[:2] == ['cloudformation', 'describe-stacks']:\n"
                "    print(json.dumps({'Stacks': [{'StackId': 'arn:aws:cloudformation:us-east-1:123456789012:stack/test-stack/abcd1234-aaaa', 'StackStatus': 'CREATE_IN_PROGRESS', 'Outputs': []}]}))\n"
                "elif args[:2] == ['cloudformation', 'list-stacks']:\n"
                "    print(json.dumps({'StackSummaries': []}))\n"
                "elif args[:2] == ['evs', 'list-environments']:\n"
                "    print(json.dumps({'environmentSummaries': []}))\n"
                "elif args[:2] == ['logs', 'filter-log-events']:\n"
                "    print('None')\n"
                "else:\n"
                "    sys.exit('unexpected AWS call: ' + repr(args))\n"
            )
            fake_aws.chmod(0o755)
            env = os.environ.copy()
            env.update(
                STACK_NAME="test-stack",
                REGION="us-east-1",
                ENVIRONMENT_NAME="my-vcf-env",
                PATH=f"{bindir}:{env['PATH']}",
            )
            env.pop("ENVIRONMENT_ID", None)
            result = subprocess.run(
                ["bash", str(WATCHER), "--once"],
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CREATE_IN_PROGRESS", result.stdout)
        self.assertIn("my-vcf-env: not found yet", result.stdout)


if __name__ == "__main__":
    unittest.main()
