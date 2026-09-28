#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

: "${BLUEPRINT:?set BLUEPRINT to the local blueprint file}"
: "${OVA:?set OVA to the local installer OVA file}"
: "${OVF:?set OVF to the local ovftool zip}"
: "${BUCKET:?set BUCKET to the deployment S3 bucket}"
: "${REGION:?set REGION to the AWS region}"
: "${STACK_NAME:?set STACK_NAME}"
: "${AZ:?set AZ to the runner/EVS availability zone}"
: "${SECRET_NAME:?set SECRET_NAME to the depot secret name}"

[[ -f "$BLUEPRINT" ]] || { echo "Blueprint not found: $BLUEPRINT" >&2; exit 1; }
OVA_KEY=${OVA##*/}
OVF_KEY=${OVF##*/}

aws s3 cp "$BLUEPRINT" "s3://${BUCKET}/blueprint.yaml" --region "$REGION"

for file in "$OVA" "$OVF"; do
  key=${file##*/}
  if ! aws s3api head-object --bucket "$BUCKET" --key "$key" --region "$REGION" >/dev/null 2>&1; then
    aws s3 cp "$file" "s3://${BUCKET}/" --region "$REGION"
  fi
done

aws s3 cp evs-deployment-orchestrator.yaml \
  "s3://${BUCKET}/evs-deployment-orchestrator.yaml" \
  --region "$REGION"

TEMPLATE_URL="https://s3.${REGION}.amazonaws.com/${BUCKET}/evs-deployment-orchestrator.yaml"

aws cloudformation create-stack \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --template-url "$TEMPLATE_URL" \
  --capabilities CAPABILITY_IAM \
  --parameters \
    "ParameterKey=BlueprintKey,ParameterValue=s3://${BUCKET}/blueprint.yaml" \
    "ParameterKey=OvaKey,ParameterValue=s3://${BUCKET}/${OVA_KEY}" \
    "ParameterKey=OvfKey,ParameterValue=s3://${BUCKET}/${OVF_KEY}" \
    "ParameterKey=AvailabilityZone,ParameterValue=${AZ}" \
    "ParameterKey=DepotSecretName,ParameterValue=${SECRET_NAME}"
