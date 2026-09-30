import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "download_runner_files.py"
sys.path.insert(0, str(SCRIPT.parent))
import download_runner_files as downloader  # noqa: E402


class DownloadRunnerFilesTests(unittest.TestCase):
    def test_help_documents_required_runner_and_output_arguments(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--instance-id", result.stdout)
        self.assertIn("--region", result.stdout)
        self.assertIn("--output-dir", result.stdout)

    def test_refuses_to_overwrite_existing_download(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "config.json"
            existing.write_text("keep me")
            with patch.object(downloader, "download_files", side_effect=AssertionError):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        downloader.main([
                            "--instance-id", "i-1234567890abcdef0",
                            "--region", "us-east-1",
                            "--output-dir", directory,
                        ])

            self.assertEqual(existing.read_text(), "keep me")

    @unittest.skipUnless(
        os.name == "posix" and shutil.which("openssl") and shutil.which("bash"),
        "POSIX, Bash, and OpenSSL are required",
    )
    def test_downloads_encrypted_files_without_exposing_content_in_command_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "runner"
            paths = {
                "config.json": remote / "opt/evs/orchestrator/config.json",
                "edge_cluster_spec.json": remote / "opt/evs/orchestrator/edge_cluster_spec.json",
            }
            contents = {"config.json": b"config-secret", "edge_cluster_spec.json": b"edge-data"}
            for name, path in paths.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents[name])
            remote_files = {name: str(path.relative_to("/")) for name, path in paths.items()}
            output = root / "downloaded"
            ssm_output = ""

            def fake_aws(args, region, profile):
                nonlocal ssm_output
                if args[1] == "send-command":
                    parameters = json.loads(args[args.index("--parameters") + 1])
                    remote_command = shlex.split(parameters["commands"][0])
                    response = subprocess.run(remote_command, capture_output=True, check=True)
                    ssm_output = response.stdout.decode()
                    return {"Command": {"CommandId": "test-command"}}
                return {"Status": "Success", "StandardOutputContent": ssm_output}

            printed = io.StringIO()
            real_which = shutil.which

            def find_tool(name):
                return real_which(name) or "/fake/aws"
            with (
                patch.dict(downloader.REMOTE_FILES, remote_files),
                patch.object(downloader, "aws_json", side_effect=fake_aws),
                patch.object(downloader.shutil, "which", side_effect=find_tool),
                redirect_stdout(printed),
            ):
                result = downloader.main([
                    "--instance-id", "i-1234567890abcdef0",
                    "--region", "us-east-1",
                    "--output-dir", str(output),
                ])

            self.assertEqual(result, 0)
            self.assertEqual((output / "config.json").read_bytes(), contents["config.json"])
            self.assertEqual((output / "edge_cluster_spec.json").read_bytes(), contents["edge_cluster_spec.json"])
            self.assertEqual((output / "config.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual((output / "edge_cluster_spec.json").stat().st_mode & 0o777, 0o600)
            self.assertNotIn("config-secret", ssm_output + printed.getvalue())
            self.assertNotIn("edge-data", ssm_output + printed.getvalue())


if __name__ == "__main__":
    unittest.main()
