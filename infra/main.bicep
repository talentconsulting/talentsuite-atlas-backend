// The Foundry resources the hosted agents run in: an AI Services account with one project, the model
// deployments the agents call, Application Insights for traces, the project connections the agents read
// their GitHub credentials from, the usage ledger the agents record model usage in for billing, an optional
// monthly cost budget, and the roles the GitHub Actions deploy identity needs.
//
// Deploy into an existing resource group in incremental mode. Resources this template does not declare
// (for example other model deployments on the account) are left alone.
targetScope = 'resourceGroup'

@description('Region for every resource.')
param location string = resourceGroup().location

@description('Foundry (AI Services) account name. Also its custom subdomain, so it must be globally unique.')
param accountName string

@description('Foundry project the agents are deployed into.')
param projectName string

@description('Project description shown in the Foundry portal.')
param projectDescription string = 'Hosted agents for TalentSuite Atlas.'

@description('Tags applied to every resource.')
param tags object = {}

@description('Log Analytics workspace behind Application Insights.')
param logAnalyticsName string

@description('Application Insights component connected to the project.')
param appInsightsName string

@description('Name of the project connection to Application Insights.')
param appInsightsConnectionName string = '${projectName}-appinsights'

@description('Model deployments the agents use. gpt-4o is the default model; user-journeys uses gpt-4.1.')
param modelDeployments array = [
  {
    name: 'gpt-4o'
    format: 'OpenAI'
    model: 'gpt-4o'
    version: '2024-11-20'
    sku: 'GlobalStandard'
    capacity: 2990
  }
  {
    name: 'gpt-4.1'
    format: 'OpenAI'
    model: 'gpt-4.1'
    version: '2025-04-14'
    sku: 'GlobalStandard'
    capacity: 980
  }
]

@description('Object ID of the service principal GitHub Actions deploys agents with. Empty skips its role assignments.')
param deployPrincipalId string = ''

@description('Create the role assignments. Turn off for an environment whose roles were assigned by hand, because Azure rejects a second assignment of the same role under a different name.')
param createRoleAssignments bool = true

@description('Atlas GitHub App ID for the atlas-github-app connection. The connection is created only when the ID and key are both set.')
param githubAppId string = ''

@secure()
@description('Atlas GitHub App private key (PEM). Literal \\n sequences are accepted by the agents.')
param githubAppPrivateKey string = ''

@description('Storage account holding the usage ledger table. Globally unique, 3-24 lowercase letters and digits.')
param usageStorageName string = take('${toLower(replace(accountName, '-', ''))}usage', 24)

@description('Expiry of the add-only SAS the agents write usage with. Each provisioning run issues a new one.')
param usageSasExpiry string = dateTimeAdd(utcNow(), 'P1Y')

@description('Monthly cost budget for the resource group, in the billing currency. 0 skips the budget.')
param budgetAmount int = 0

@description('First day of the budget\'s first month (yyyy-MM-01). Azure rejects a change to it, so keep it fixed per environment.')
param budgetStartDate string = '2026-10-01'

@description('Who is emailed when the budget reaches 80% and 100% of actual spend, or is forecast to pass 100%.')
param budgetContactEmails array = []

// Built-in role definition IDs.
var foundryProjectManagerRoleId = 'eadc314b-1a2d-4efa-be10-5d325db5065e'
var azureAiDeveloperRoleId = '64702f94-c441-49e6-a78b-ef80e0188fee'
var readerRoleId = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: accountName
    allowProjectManagement: true
    publicNetworkAccess: 'Enabled'
    networkAcls: { defaultAction: 'Allow' }
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: projectName
    description: projectDescription
  }
}

// One at a time, and after the project: the account rejects concurrent changes, including a project
// being created while a model is deployed.
@batchSize(1)
resource deployments 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = [for deployment in modelDeployments: {
  parent: account
  dependsOn: [project]
  name: deployment.name
  sku: {
    name: deployment.sku
    capacity: deployment.capacity
  }
  properties: {
    model: {
      format: deployment.format
      name: deployment.model
      version: deployment.version
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}]

resource appInsightsConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: appInsightsConnectionName
  properties: {
    category: 'AppInsights'
    authType: 'ApiKey'
    target: appInsights.id
    isSharedToAll: false
    credentials: { key: appInsights.properties.ConnectionString }
    metadata: {
      ApiType: 'Azure'
      ResourceId: appInsights.id
    }
  }
}

resource githubAppConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = if (!empty(githubAppId) && !empty(githubAppPrivateKey)) {
  parent: project
  name: 'atlas-github-app'
  properties: {
    category: 'CustomKeys'
    authType: 'CustomKeys'
    target: '_'
    isSharedToAll: false
    credentials: {
      keys: {
        app_id: githubAppId
        private_key: githubAppPrivateKey
      }
    }
  }
}

// The usage ledger: one row per model call, per tenant and month, read by the billing report.
resource usageStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: usageStorageName
  location: location
  tags: union(tags, { Purpose: 'atlas-usage-ledger' })
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    // The agents write with a SAS, which is signed with the account key.
    allowSharedKeyAccess: true
  }
}

resource usageTableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: usageStorage
  name: 'default'
}

resource usageTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: usageTableService
  name: 'usage'
}

// Add-only: an agent can record usage but not read, change or delete any.
var usageTableSas = usageStorage.listServiceSas('2023-05-01', {
  canonicalizedResource: '/table/${usageStorageName}/${usageTable.name}'
  signedPermission: 'a'
  signedProtocol: 'https'
  signedExpiry: usageSasExpiry
}).serviceSasToken

resource usageLedgerConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: 'atlas-usage-ledger'
  properties: {
    category: 'CustomKeys'
    authType: 'CustomKeys'
    target: '_'
    isSharedToAll: false
    credentials: {
      keys: {
        table_url: '${usageStorage.properties.primaryEndpoints.table}${usageTable.name}?${usageTableSas}'
      }
    }
  }
}

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = if (budgetAmount > 0 && !empty(budgetContactEmails)) {
  name: '${resourceGroup().name}-monthly'
  properties: {
    category: 'Cost'
    amount: budgetAmount
    timeGrain: 'Monthly'
    timePeriod: { startDate: budgetStartDate }
    notifications: {
      actual80: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: budgetContactEmails
      }
      actual100: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Actual'
        contactEmails: budgetContactEmails
      }
      forecast100: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: budgetContactEmails
      }
    }
  }
}

// The project's identity reads the traces it writes.
resource projectReadsAppInsights 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (createRoleAssignments) {
  scope: appInsights
  name: guid(appInsights.id, project.id, readerRoleId)
  properties: {
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', readerRoleId)
  }
}

// The deploy identity creates agent versions in the project and invokes them in smoke tests.
resource deployerManagesProjects 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (createRoleAssignments && !empty(deployPrincipalId)) {
  scope: account
  name: guid(account.id, deployPrincipalId, foundryProjectManagerRoleId)
  properties: {
    principalId: deployPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryProjectManagerRoleId)
  }
}

resource deployerDevelopsAi 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (createRoleAssignments && !empty(deployPrincipalId)) {
  name: guid(resourceGroup().id, deployPrincipalId, azureAiDeveloperRoleId)
  properties: {
    principalId: deployPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', azureAiDeveloperRoleId)
  }
}

@description('Value for the AZURE_AI_PROJECT_ENDPOINT GitHub secret.')
output projectEndpoint string = 'https://${accountName}.services.ai.azure.com/api/projects/${projectName}'
output accountName string = account.name
output projectName string = project.name
output usageStorageName string = usageStorage.name
