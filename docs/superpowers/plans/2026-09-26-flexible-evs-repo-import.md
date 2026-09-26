# Flexible EVS Repository Import and Blueprint-Selected Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import the dynamic-CIDR refactor into `flexible-solutions-for-amazon-evs` and have the bootstrap runner clone the repository URL configured in its downloaded blueprint.

**Architecture:** Copy the tracked source snapshot into the existing target repository while preserving its `.git` and `LICENSE`; omit the local deployment blueprint and untracked artifacts. The runner will download the blueprint first, validate `orchestrator_repo_url`, then clone that public HTTPS URL at `main` into the existing source path.

**Tech Stack:** Git, Bash UserData, Python 3.11, PyYAML, `unittest`, CloudFormation YAML, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-26-flexible-evs-source-design.md`

## Global Constraints

- Target repo: `/home/jimccann/refactor/flexible-solutions-for-amazon-evs`, branch `main`; source snapshot: `/home/jimccann/refactor/solutions-for-amazon-evs-refactored`, branch `refactor/dynamic-evs-cidrs`.
- Preserve the target repository's `.git` history and Apache-2.0 `LICENSE`.
- Do not copy the personal `Deploy/EVS-Deployment-Orchestrator/blueprint.yaml`, `runme.sh`, OVA, ovftool archive, or untracked planning documents into the public repository.
- Set `orchestrator_repo_url` to `https://github.com/jimccann-rh/flexible-solutions-for-amazon-evs.git`; the runner clones `main` and does not use credentials.
- Do not modify or delete the running CloudFormation stack. Do not push to `aws/solutions-for-amazon-evs`; publishing to the new repository is authorized after verification.

## Review Focus

- Missing or malformed `orchestrator_repo_url` must fail before clone, not silently use the incompatible AWS upstream repository.
- The runner must not clone before the configured S3 blueprint has been downloaded and parsed.
- The URL is quoted as data to `git clone`; it is never evaluated by the shell.
- `blueprint.yaml` remains local-only; no OVA, ovftool, or `runme.sh` appears in the target repo.
- The clone path stays `/opt/evs/src/Deploy/EVS-Deployment-Orchestrator/orchestrator` so resume/destroy commands still work.

---

### Task 1: Import the tracked refactor snapshot

**Files:** Copy the tracked source tree into the target repo; preserve target `.git` and `LICENSE`.

- [ ] Confirm source and target worktrees are the expected branches and that the target contains only its initial commit plus the approved spec commit.
- [ ] Import the current committed source snapshot without copying `.git` or overwriting the target license:

```bash
SRC=/home/jimccann/refactor/solutions-for-amazon-evs-refactored
DST=/home/jimccann/refactor/flexible-solutions-for-amazon-evs
git -C "$SRC" archive HEAD | tar --exclude=LICENSE -x -C "$DST"
```

- [ ] Verify the target `.git` directory and `LICENSE` remain, the refactor's tracked files are present, and the personal blueprint/OVA/ovftool/runme files are absent. Review `git -C "$DST" status --short` and `git -C "$DST" diff --stat`.
- [ ] Commit the imported snapshot locally:

```bash
git -C "$DST" add -A
git -C "$DST" commit -m "chore: import dynamic CIDR EVS refactor"
```

### Task 2: Add the blueprint repository setting

**Files:** Modify target `Deploy/EVS-Deployment-Orchestrator/blueprints/custom.all-options.example.yaml`; modify the local-only source `Deploy/EVS-Deployment-Orchestrator/blueprint.yaml`; extend `Deploy/EVS-Deployment-Orchestrator/tests/test_network_cidrs.py`.

- [ ] Add a failing test that checks the all-commented example documents the exact key and URL:

```python
def test_all_options_blueprint_documents_orchestrator_repo_url(self):
    path = Path(__file__).resolve().parents[1] / "blueprints/custom.all-options.example.yaml"
    text = path.read_text()
    self.assertIn('orchestrator_repo_url: "https://github.com/jimccann-rh/flexible-solutions-for-amazon-evs.git"', text)
```

- [ ] Run `python -m unittest discover -s Deploy/EVS-Deployment-Orchestrator/tests -v`; confirm this test fails because the example does not yet document the URL.
- [ ] Add a commented root-level `orchestrator_repo_url` example with a note that bootstrap requires it and clones `main`. Add the same active root-level key to the existing local-only `Deploy/EVS-Deployment-Orchestrator/blueprint.yaml`; do not stage or copy that local file.
- [ ] Rerun the unittest command and confirm the new test passes; commit only target-repo files:

```bash
git add Deploy/EVS-Deployment-Orchestrator/blueprints/custom.all-options.example.yaml Deploy/EVS-Deployment-Orchestrator/tests/test_network_cidrs.py
git commit -m "docs: configure orchestrator source repository"
```

### Task 3: Make UserData clone the blueprint-selected repository

**Files:** Modify target `Deploy/EVS-Deployment-Orchestrator/evs-deployment-orchestrator.yaml`; extend `Deploy/EVS-Deployment-Orchestrator/tests/test_network_cidrs.py`.

- [ ] Add this failing structural test to `NetworkCidrTests`:

```python
def test_runner_clones_the_blueprint_selected_repo_after_blueprint_download(self):
    path = Path(__file__).resolve().parents[1] / "evs-deployment-orchestrator.yaml"
    template = yaml.load(path.read_text(), Loader=CloudFormationLoader)
    user_data = template["Resources"]["RunnerInstance"]["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"]
    script = user_data[0] if isinstance(user_data, list) else user_data
    download = 'retry aws s3 cp "s3://$BLUEPRINT_BUCKET/$BLUEPRINT_OBJ_KEY" ./blueprint.yaml'
    clone = 'retry git clone "$ORCHESTRATOR_REPO_URL" src'

    self.assertIn(download, script)
    self.assertIn(clone, script)
    self.assertLess(script.index(download), script.index(clone))
    self.assertIn("orchestrator_repo_url", script)
    self.assertNotIn("https://github.com/aws/solutions-for-amazon-evs.git", script)
```

- [ ] Run `python -m unittest discover -s Deploy/EVS-Deployment-Orchestrator/tests -p test_network_cidrs.py -v`; confirm it fails because the current UserData clones the hard-coded upstream before downloading the blueprint.
- [ ] Move PyYAML installation before blueprint parsing. Move the source clone block to after the S3 blueprint download. Extract the configured URL safely, fail if it is absent or does not start with `https://github.com/`, then clone the configured repository at `main` into `src`:

```bash
ORCHESTRATOR_REPO_URL=$(python3.11 -c 'import yaml; print((yaml.safe_load(open("blueprint.yaml")) or {}).get("orchestrator_repo_url", ""))') \
  || fail "could not read blueprint.orchestrator_repo_url"
case "$ORCHESTRATOR_REPO_URL" in
  https://github.com/*) ;;
  *) fail "blueprint.orchestrator_repo_url must be an HTTPS GitHub URL" ;;
esac
PINNED_COMMIT="main"
retry git clone "$ORCHESTRATOR_REPO_URL" src || fail "orchestrator source clone failed"
(cd src && git checkout --quiet "$PINNED_COMMIT") || fail "orchestrator ref checkout failed"
cd src/Deploy/EVS-Deployment-Orchestrator/orchestrator || fail "orchestrator directory missing"
echo "orchestrator commit: $(git rev-parse HEAD)"
```

- [ ] Preserve the existing `/opt/evs/src` layout and ensure the blueprint overlay leaves `orchestrator_repo_url` untouched.
- [ ] Run the targeted regression test, then the full unittest suite; confirm it proves download-before-clone and rejects regression to the AWS upstream URL.
- [ ] Commit the template and tests:

```bash
git add Deploy/EVS-Deployment-Orchestrator/evs-deployment-orchestrator.yaml Deploy/EVS-Deployment-Orchestrator/tests/test_network_cidrs.py
git commit -m "fix: clone orchestrator source from blueprint"
```

### Task 4: Document source configuration and rollout

**Files:** Modify target `Deploy/EVS-Deployment-Orchestrator/README.md`.

- [ ] Document that the uploaded blueprint must set `orchestrator_repo_url` to a publicly cloneable HTTPS GitHub repository, that bootstrap checks it before cloning `main`, and that source selection applies only to new bootstrap launches.
- [ ] Document that this does not repair or update an already-running stack; users must use a separately reviewed recovery/relaunch procedure.
- [ ] Run `git diff --check` and review the rendered text; commit:

```bash
git add Deploy/EVS-Deployment-Orchestrator/README.md
git commit -m "docs: explain blueprint-selected orchestrator source"
```

### Task 5: Full verification and authorized publication

**Files:** Review the complete target-repository diff and status.

- [ ] Run the full checks from the target repository:

```bash
python -m unittest discover -s Deploy/EVS-Deployment-Orchestrator/tests -v
ruff check Deploy/EVS-Deployment-Orchestrator --select E,F,W --ignore E501
python -m compileall -q Deploy/EVS-Deployment-Orchestrator
git diff --check
```

- [ ] Confirm the target contains the source refactor and source-selector changes, preserves its original `LICENSE`, and contains none of the personal blueprint, OVA/ovftool, or runme files. Confirm the source refactor repo remains unchanged except for its local-only blueprint URL field.
- [ ] Push only the verified target `main` branch to `origin` (the user explicitly authorized publishing to this new repo); never push the AWS upstream remote:

```bash
git push origin main
```
