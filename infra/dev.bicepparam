// The dev environment in atlas-dev-rg, provisioned from this template.
using 'main.bicep'

param accountName = 'atlas-dev-foundry'
param projectName = 'atlas-dev-agents'
param logAnalyticsName = 'atlas-dev-logs'
param appInsightsName = 'atlas-dev-appinsights'
param tags = {
  Owner: 'rgparkins'
  Environment: 'dev'
}

param createRoleAssignments = true
param deployPrincipalId = readEnvironmentVariable('DEPLOY_PRINCIPAL_ID', '')

param githubAppId = readEnvironmentVariable('ATLAS_GITHUB_APP_ID', '')
param githubAppPrivateKey = readEnvironmentVariable('ATLAS_GITHUB_APP_PRIVATE_KEY', '')

// Monthly cost budget for the resource group, with email alerts.
param budgetAmount = 100
param budgetContactEmails = [
  'richard.parkins@talentconsulting.uk'
]
