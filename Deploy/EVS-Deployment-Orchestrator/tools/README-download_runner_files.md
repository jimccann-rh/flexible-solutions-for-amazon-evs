# download_runner_files.py

Download the runner's `config.json` and `edge_cluster_spec.json` through AWS Systems Manager Run Command.

## Prerequisites

- Python 3 and AWS CLI configured with credentials for the target account.
- OpenSSL on the computer running the script and on the SSM-managed Linux runner.
- The runner is online in Systems Manager and has `bash`, `tar`, and `base64`.
- IAM access to `ssm:SendCommand` and `ssm:GetCommandInvocation` for the runner.

## Usage

Run from the repository root. Provide the instance ID, AWS region, and an output directory; `--profile` is optional and defaults to the AWS CLI's normal credential resolution.

```bash
python3 Deploy/EVS-Deployment-Orchestrator/tools/download_runner_files.py \
  --instance-id i-0123456789abcdef0 \
  --region us-east-1 \
  --profile my-profile \
  --output-dir "$HOME/evs-runner-files"
```

The output directory will contain:

```text
$HOME/evs-runner-files/config.json
$HOME/evs-runner-files/edge_cluster_spec.json
```

The files are read from these runner paths:

```text
/opt/evs/src/Deploy/EVS-Deployment-Orchestrator/orchestrator/evs_environment/config.json
/opt/evs/src/Deploy/EVS-Deployment-Orchestrator/orchestrator/vcf_deployment/edge_cluster_spec.json
```

The script refuses to overwrite either output file. Choose a new output directory or move the existing files before retrying.

## Transfer security and limits

The script creates a temporary local RSA key and sends only its public certificate to the runner. The runner streams the requested files into an AES-256-GCM encrypted archive; SSM command output contains ciphertext, not the file contents. The local private key and decrypted temporary archive are removed when the script exits.

On POSIX, a newly created output directory has mode `0700`, and downloaded files have mode `0600`. Existing output-directory permissions are left unchanged.

SSM command output is limited, so the script stops if the encrypted payload exceeds 22 KB. If the files grow beyond that limit, use an S3-based transfer instead.

## Troubleshooting

- **Access denied:** confirm the caller has `ssm:SendCommand` and `ssm:GetCommandInvocation` permissions.
- **Runner unavailable:** confirm the instance is online in Systems Manager.
- **OpenSSL or command missing:** install OpenSSL locally and ensure the runner has OpenSSL, Bash, tar, and base64.
- **File not found:** confirm the runner has completed deployment and generated both files.
