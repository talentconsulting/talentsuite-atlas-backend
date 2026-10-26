// The test environment in atlas-test-rg, provisioned from this template.
using 'main.bicep'

param accountName = 'atlas-test-foundry'
param projectName = 'atlas-test-agents'
param logAnalyticsName = 'atlas-test-logs'
param appInsightsName = 'atlas-test-appinsights'
param tags = {
  Owner: 'rgparkins'
  Environment: 'test'
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
