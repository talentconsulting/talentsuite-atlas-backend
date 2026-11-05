#!/usr/bin/env bash
# Gives a deployed agent's own identity the Foundry User role on the Foundry account.
#
# Usage: grant-agent-session-access.sh <agent-name>
# Needs: AZURE_TENANT_ID, TARGET_SUBSCRIPTION_ID (not AZURE_SUBSCRIPTION_ID, which breaks azd auth token), AZURE_RESOURCE_GROUP, AZURE_AI_ACCOUNT_NAME,
#        AZURE_AI_PROJECT_ENDPOINT, and an `azd auth login` session for the deploy identity.
# Set DRY_RUN=1 to print what it would assign without assigning it.
#
# Agents that call other agents delete each call's hosted session when it returns, so idle sessions
# don't fill the subscription's concurrent-session quota. Deleting a session needs a Foundry data-plane
# role, and Foundry creates each agent's identity on its first deploy with no role at all, so this runs
# after every deploy. Only callers need it: workflows and manifest orchestrators.
#
# The assignment has a fixed name, so a redeploy is a no-op. A failure only warns, because the deploy
# itself succeeded; without the role the agent still works but its sessions are left to expire.
set -uo pipefail
agent="$1"
case "$agent" in
  *-workflow|*-manifest-orchestrator) ;;
  *) echo "${agent} does not call other agents, so it needs no session access."; exit 0 ;;
esac
FOUNDRY_USER_ROLE_ID=53ca6127-db72-4b80-b1b0-d745d6d5456d

warn() { echo "::warning::${agent}: $1 Its hosted sessions will not be deleted until it has Foundry User on ${AZURE_AI_ACCOUNT_NAME}."; exit 0; }
token() { azd auth token --scope "$1" --tenant-id "$AZURE_TENANT_ID" -o json 2>/dev/null | jq -r '.token // empty'; }

data_token=$(token https://ai.azure.com/.default)
[ -n "$data_token" ] || warn "could not get a token for the Foundry project."
principal=$(curl -fsS -H "Authorization: Bearer $data_token" "${AZURE_AI_PROJECT_ENDPOINT%/}/agents/${agent}?api-version=v1" \
  | jq -r '.versions.latest.instance_identity.principal_id // empty')
[ -n "$principal" ] || warn "could not read the agent's identity."

scope="/subscriptions/${TARGET_SUBSCRIPTION_ID}/resourceGroups/${AZURE_RESOURCE_GROUP}/providers/Microsoft.CognitiveServices/accounts/${AZURE_AI_ACCOUNT_NAME}"
name=$(python3 -c 'import sys, uuid; print(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(sys.argv[1:])))' "$scope" "$principal" "$FOUNDRY_USER_ROLE_ID")
url="https://management.azure.com${scope}/providers/Microsoft.Authorization/roleAssignments/${name}?api-version=2022-04-01"
body=$(jq -cn --arg role "/subscriptions/${TARGET_SUBSCRIPTION_ID}/providers/Microsoft.Authorization/roleDefinitions/${FOUNDRY_USER_ROLE_ID}" --arg principal "$principal" \
  '{properties: {roleDefinitionId: $role, principalId: $principal, principalType: "ServicePrincipal"}}')
if [ "${DRY_RUN:-}" = 1 ]; then
  echo "Would assign Foundry User on ${AZURE_AI_ACCOUNT_NAME} to ${agent} (${principal}) as ${name}."
  exit 0
fi

arm_token=$(token https://management.azure.com/.default)
[ -n "$arm_token" ] || warn "could not get a token for Azure Resource Manager."
response=$(mktemp)
status=$(curl -sS -o "$response" -w '%{http_code}' -X PUT -H "Authorization: Bearer $arm_token" -H "Content-Type: application/json" -d "$body" "$url")
case "$status" in
  200|201) echo "Gave ${agent} (${principal}) Foundry User on ${AZURE_AI_ACCOUNT_NAME}." ;;
  409) echo "${agent} (${principal}) already has Foundry User on ${AZURE_AI_ACCOUNT_NAME}." ;;
  403) warn "the deploy identity may not assign Foundry User (see DEPLOYMENT.md, 'Agent identities')." ;;
  *) warn "assigning the role returned HTTP ${status}: $(jq -r '.error.message // empty' "$response" 2>/dev/null | head -c 300)" ;;
esac
exit 0
