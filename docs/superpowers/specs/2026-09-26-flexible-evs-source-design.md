# Flexible EVS Repository Import and Blueprint-Selected Source

## Goal

Move the tracked contents of the dynamic-CIDR refactor into the blank `flexible-solutions-for-amazon-evs` repository and make the bootstrap runner clone its orchestrator source from the repository URL configured in the downloaded blueprint.

The new repository is `https://github.com/jimccann-rh/flexible-solutions-for-amazon-evs.git`. The deployment-specific `Deploy/EVS-Deployment-Orchestrator/blueprint.yaml` stays local and is not copied or committed to the public repository. The current bootstrap stack is not modified by these source changes.

## Repository import

- Copy the tracked working-tree snapshot from `solutions-for-amazon-evs-refactored` into `/home/jimccann/refactor/flexible-solutions-for-amazon-evs`.
- Preserve the new repository's `.git` history and Apache-2.0 `LICENSE`; make local commits on its `main` branch.
- Do not copy untracked deployment files or artifacts: the personal `blueprint.yaml`, `runme.sh`, OVA, ovftool archive, or untracked planning documents.
- Do not push until implementation verification is complete. The user authorized publishing changes to this new repository; never push to the original AWS repository.

## Blueprint source setting

Add a root-level `orchestrator_repo_url` string to:

1. the local deployment blueprint at `Deploy/EVS-Deployment-Orchestrator/blueprint.yaml` (local only), and
2. the checked-in `blueprints/custom.all-options.example.yaml` as a documented bootstrap setting.

The example value is `https://github.com/jimccann-rh/flexible-solutions-for-amazon-evs.git`. No access token or other credential is included; the repository is publicly cloneable. The bootstrap must fail with a clear error if the setting is missing or not an HTTPS GitHub URL, rather than silently falling back to the AWS upstream repository.

## Runner source flow

Today, UserData clones `https://github.com/aws/solutions-for-amazon-evs.git` at `main` before it downloads `BlueprintKey`. Change the order so that the runner:

1. installs Python and PyYAML, performs the existing S3 artifact preflight, and downloads the configured blueprint;
2. reads and validates `orchestrator_repo_url` from that blueprint;
3. clones that URL into the existing `/opt/evs/src` checkout location, checks out `main`, and logs the resolved commit SHA;
4. continues with the existing blueprint overlay, depot check, and orchestrator launch.

Keep the runner's existing source directory layout so resume and destroy commands continue to work. The blueprint overlay must preserve the new field. The source URL is data, passed quoted to `git clone`, never shell-evaluated.

## Verification

- Add a structural test for the CloudFormation UserData script proving blueprint download occurs before clone, clone reads `orchestrator_repo_url`, and the AWS upstream URL is no longer hard-coded.
- Assert the all-commented example blueprint documents the source URL field and example repository URL.
- Run the existing EVS orchestrator unittest suite, Ruff, compileall, and `git diff --check`.
- Review the target-repository diff to ensure no local OVA/ovftool files, personal blueprint, credentials, TGW resources, or EVS-owned VLAN subnet resources were copied/introduced.

## Rollout and non-goals

The repository/template change only affects future bootstrap launches. It does not change the already-running stack or replace its runner. Do not update or delete that stack as part of this change. The user will upload the corrected template and blueprint and decide separately how to recover or recreate the active deployment.

Do not create GitHub credentials, change TGW ownership, change the EVS CIDR allocation algorithm, or push to the original repository.
