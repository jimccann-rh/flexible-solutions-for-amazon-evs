#!/usr/bin/env bash
set -euo pipefail

# Read-only deployment status and CloudWatch log monitor.
usage() {
  echo "Usage: STACK_NAME=... REGION=... $0 [--once]" >&2
  echo "Optionally set ENVIRONMENT_ID or ENVIRONMENT_NAME for EVS/host status." >&2
  echo "POLL_INTERVAL_SECONDS defaults to 600 (10 minutes)." >&2
}

once=false
case "${1:-}" in
  "") ;;
  --once) once=true ;;
  *) usage; exit 2 ;;
esac

: "${STACK_NAME:?set STACK_NAME}"
: "${REGION:?set REGION}"
POLL_INTERVAL_SECONDS=${POLL_INTERVAL_SECONDS:-600}
[[ "$POLL_INTERVAL_SECONDS" =~ ^[1-9][0-9]*$ ]] || { echo "POLL_INTERVAL_SECONDS must be a positive integer" >&2; exit 2; }

since_ms=$((($(date +%s) - 600) * 1000))

while true; do
  stack_json=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" --output json)
  IFS=$'\t' read -r stack_status stack_id runner_id < <(
    python3 -c 'import json,sys; s=json.load(sys.stdin)["Stacks"][0]; o={x["OutputKey"]:x["OutputValue"] for x in s.get("Outputs",[])}; print("\t".join((s["StackStatus"],s["StackId"],o.get("RunnerInstanceId","None"))))' <<<"$stack_json"
  )
  stack_uuid=${stack_id##*/}
  log_suffix=${stack_uuid%%-*}
  log_prefix="/evs/${STACK_NAME}-${log_suffix}"
  now_ms=$(($(date +%s) * 1000))
  printf '\n[%s] CloudFormation %s: %s\n' "$(date -u +%FT%TZ)" "$STACK_NAME" "$stack_status"
  echo 'Related CloudFormation stacks:'
  infra_stacks=$(aws cloudformation list-stacks --region "$REGION" --output json |
    python3 -c 'import json,sys; prefix=sys.argv[1]+"-amazon-evs-"; [print("{}: {}".format(s["StackName"],s["StackStatus"])) for s in json.load(sys.stdin)["StackSummaries"] if s["StackName"].startswith(prefix) and s["StackStatus"]!="DELETE_COMPLETE"]' "$STACK_NAME")
  if [[ -n "$infra_stacks" ]]; then printf '%s\n' "$infra_stacks"; else echo '  (none)'; fi
  environment_id=${ENVIRONMENT_ID:-}
  lookup_failed=false
  if [[ -z "$environment_id" && -n "${ENVIRONMENT_NAME:-}" ]]; then
    if environment_list=$(aws evs list-environments --region "$REGION" --output json); then
      environment_id=$(python3 -c 'import json,os,sys; xs=[x["environmentId"] for x in json.load(sys.stdin)["environmentSummaries"] if x["environmentName"]==os.environ["ENVIRONMENT_NAME"]]; print(xs[0] if len(xs)==1 else "")' <<<"$environment_list")
    else
      lookup_failed=true
    fi
  fi
  if [[ -n "$environment_id" ]]; then
    ENVIRONMENT_ID=$environment_id
    printf 'EVS environment %s:\n' "$environment_id"
    if ! aws evs get-environment --environment-id "$environment_id" --region "$REGION" \
      --query 'environment.{Name:environmentName,State:environmentState,Details:stateDetails}' --output table; then
      echo '  unable to read EVS environment; continuing'
    fi
    echo 'EVS hosts:'
    if ! aws evs list-environment-hosts --environment-id "$environment_id" --region "$REGION" \
      --query 'environmentHosts[].{Name:hostName,State:hostState,Details:stateDetails}' --output table; then
      echo '  unable to read EVS hosts; continuing'
    fi
  elif [[ "$lookup_failed" == true ]]; then
    printf 'EVS environment %s: lookup failed; continuing with CloudFormation/logs\n' "${ENVIRONMENT_NAME:-}"
  elif [[ -n "${ENVIRONMENT_NAME:-}" ]]; then
    printf 'EVS environment %s: not found yet\n' "$ENVIRONMENT_NAME"
  else
    echo 'EVS environment not configured; showing CloudFormation/logs only.'
  fi

  if [[ -n "$runner_id" && "$runner_id" != None ]]; then
    stage=$(aws ec2 describe-tags --region "$REGION" \
      --filters "Name=resource-id,Values=$runner_id" Name=key,Values=evs-deployment \
      --query 'Tags[0].Value' --output text 2>/dev/null || true)
    printf 'Runner %s stage: %s\n' "$runner_id" "$stage"
  fi

  for component in bootstrap orchestrator; do
    group="${log_prefix}/${component}"
    printf '%s new logs:\n' "$component"
    if ! events=$(aws logs filter-log-events --log-group-name "$group" --start-time "$since_ms" \
      --end-time "$now_ms" --region "$REGION" --query 'events[].message' --output text); then
      printf '  unable to read %s\n' "$group"
    elif [[ -n "$events" && "$events" != None ]]; then
      printf '%s\n' "$events"
    else
      echo '  (no new events)'
    fi
  done

  if "$once"; then break; fi
  # Re-read recent events next time to cover CloudWatch ingestion delay.
  since_ms=$((now_ms - 120000))
  sleep "$POLL_INTERVAL_SECONDS"
done
