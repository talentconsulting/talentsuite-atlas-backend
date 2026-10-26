// The existing dev environment. Names match what was created by hand in talent-day-rg.
using 'main.bicep'

param accountName = 'talent-day-foundry'
param projectName = 'talent-day-proj-default'
param logAnalyticsName = 'DefaultWorkspace-4eb4fc59-6e00-4fc2-86b3-e586e3987f67'
param appInsightsName = 'talent-day-proj-default-appinsights-7400'
param appInsightsConnectionName = 'talent-day-proj-default-appinsights-7400-1709'
param projectDescription = 'Default project created with the resource'
param tags = {
  Owner: 'rgparkins'
}

// Roles were assigned by hand here; see DEPLOYMENT.md.
param createRoleAssignments = false
param deployPrincipalId = readEnvironmentVariable('DEPLOY_PRINCIPAL_ID', '')

param githubAppId = readEnvironmentVariable('ATLAS_GITHUB_APP_ID', '')
param githubAppPrivateKey = readEnvironmentVariable('ATLAS_GITHUB_APP_PRIVATE_KEY', '')
