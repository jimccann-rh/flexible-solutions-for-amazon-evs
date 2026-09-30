#!/usr/bin/env python3
"""Download two EVS runner JSON files through SSM with encrypted output.

Requires AWS CLI access to `ssm:SendCommand` and `ssm:GetCommandInvocation`,
OpenSSL locally and on the SSM-managed Linux instance. SSM command output contains
ciphertext only. Example from repo root:
  python Deploy/EVS-Deployment-Orchestrator/tools/download_runner_files.py --instance-id i-... --region us-east-1 \
      --output-dir "$HOME/evs-runner-files" --profile my-profile
"""

import argparse
import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

REMOTE_FILES = {
    "config.json": "opt/evs/src/Deploy/EVS-Deployment-Orchestrator/orchestrator/evs_environment/config.json",
    "edge_cluster_spec.json": "opt/evs/src/Deploy/EVS-Deployment-Orchestrator/orchestrator/vcf_deployment/edge_cluster_spec.json",
}
PENDING_STATUSES = {"Pending", "InProgress", "Delayed", "Cancelling"}
# ponytail: SSM stdout is capped; use an S3 transfer if ciphertext exceeds 22 KB.
MAX_SSM_STDOUT = 22_000


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--profile")
    return parser


def run(command):
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"command failed: {command[0]}")
    return result.stdout


def aws_json(args, region, profile):
    command = ["aws"]
    if profile:
        command.extend(["--profile", profile])
    command.extend(["--region", region, *args, "--output", "json"])
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        if "InvocationDoesNotExist" in result.stderr:
            raise LookupError("SSM invocation is not available yet")
        raise RuntimeError(result.stderr.strip() or "AWS CLI command failed")
    return json.loads(result.stdout)


def make_remote_command(certificate):
    certificate_b64 = base64.b64encode(certificate.read_bytes()).decode("ascii")
    paths = shlex.join(REMOTE_FILES.values())
    script = f"""work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
printf '%s' '{certificate_b64}' | base64 -d > "$work/recipient.pem"
tar -czf - -C / {paths} | openssl cms -encrypt -binary -aes-256-gcm -outform DER "$work/recipient.pem" | base64 | tr -d '\\n' > "$work/output"
size=$(wc -c < "$work/output")
if [ "$size" -gt {MAX_SSM_STDOUT} ]; then
    echo 'encrypted payload exceeds SSM stdout limit; use an S3 transfer' >&2
    exit 1
fi
cat "$work/output"
"""
    return f"bash -euo pipefail -c {shlex.quote(script)}"


def wait_for_invocation(command_id, instance_id, region, profile):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            result = aws_json(
                [
                    "ssm",
                    "get-command-invocation",
                    "--command-id",
                    command_id,
                    "--instance-id",
                    instance_id,
                ],
                region,
                profile,
            )
        except LookupError:
            time.sleep(2)
            continue
        status = result.get("Status")
        if status == "Success":
            return result
        if status not in PENDING_STATUSES:
            detail = result.get("StandardErrorContent", "").strip()
            raise RuntimeError(f"SSM command ended with {status}: {detail}")
        time.sleep(2)
    raise RuntimeError("timed out waiting for the SSM command")


def unpack_bundle(archive_path):
    expected = set(REMOTE_FILES.values())
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        if (
            len(members) != len(expected)
            or {member.name for member in members} != expected
            or any(not member.isfile() for member in members)
        ):
            raise RuntimeError("SSM archive did not contain exactly the requested files")
        result = {}
        for name, path in REMOTE_FILES.items():
            member = archive.getmember(path)
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"could not read {path} from SSM archive")
            result[name] = source.read()
    return result


def download_files(instance_id, region, profile):
    if not shutil.which("aws"):
        raise RuntimeError("AWS CLI is required")
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError("OpenSSL is required")

    with tempfile.TemporaryDirectory(prefix="evs-runner-download-") as directory:
        work = Path(directory)
        key = work / "private.pem"
        certificate = work / "certificate.pem"
        run([
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-subj",
            "/CN=evs-runner-download",
            "-days",
            "1",
        ])
        key.chmod(0o600)

        response = aws_json(
            [
                "ssm",
                "send-command",
                "--instance-ids",
                instance_id,
                "--document-name",
                "AWS-RunShellScript",
                "--comment",
                "Download EVS files encrypted to a temporary key",
                "--parameters",
                json.dumps({"commands": [make_remote_command(certificate)]}),
            ],
            region,
            profile,
        )
        command_id = response["Command"]["CommandId"]
        result = wait_for_invocation(command_id, instance_id, region, profile)
        ciphertext = base64.b64decode(result["StandardOutputContent"].strip(), validate=True)
        encrypted = work / "files.cms"
        encrypted.write_bytes(ciphertext)
        archive = work / "files.tar.gz"
        run([
            openssl,
            "cms",
            "-decrypt",
            "-binary",
            "-inform",
            "DER",
            "-in",
            str(encrypted),
            "-recip",
            str(certificate),
            "-inkey",
            str(key),
            "-out",
            str(archive),
        ])
        return unpack_bundle(archive)


def save_files(output_dir, files):
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, content in files.items():
        path = output_dir / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(content)
        print(f"Saved {path}")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    output_dir = args.output_dir.expanduser()
    destinations = [output_dir / name for name in REMOTE_FILES]
    existing = [path for path in destinations if path.exists() or path.is_symlink()]
    if existing:
        parser.error(f"refusing to overwrite {existing[0]}")
    if output_dir.exists() and not output_dir.is_dir():
        parser.error(f"output path is not a directory: {output_dir}")

    try:
        files = download_files(args.instance_id, args.region, args.profile)
        save_files(output_dir, files)
    except (OSError, RuntimeError, ValueError, tarfile.TarError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
