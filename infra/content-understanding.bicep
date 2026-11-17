targetScope = 'resourceGroup'

@minLength(3)
@maxLength(55)
@description('Globally unique name for the dedicated Content Understanding Foundry resource.')
param accountName string

@description('Supported Foundry region for the Content Understanding REST API.')
@allowed([
  'australiaeast'
  'canadacentral'
  'eastus'
  'eastus2'
  'japaneast'
  'southcentralus'
  'southeastasia'
  'swedencentral'
  'uksouth'
  'westeurope'
  'westus'
  'westus3'
])
param location string = 'southcentralus'

@description('Azure AI Search system-assigned managed identity principal ID.')
param searchPrincipalId string

param tags object = {}

var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource contentUnderstanding 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  tags: union(tags, {
    workload: 'content-understanding'
  })
  properties: {
    customSubDomainName: accountName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
    allowProjectManagement: true
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: contentUnderstanding
  name: '${accountName}-project'
  location: location
  tags: union(tags, {
    workload: 'content-understanding'
  })
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: '${accountName}-project'
    description: 'Content Understanding resource for the Developer Docs grounding demo.'
  }
}

resource searchRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(contentUnderstanding.id, searchPrincipalId, cognitiveServicesUserRoleId)
  scope: contentUnderstanding
  properties: {
    principalId: searchPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalType: 'ServicePrincipal'
  }
}

output accountId string = contentUnderstanding.id
output accountName string = contentUnderstanding.name
output subdomainUrl string = 'https://${accountName}.services.ai.azure.com'
output projectName string = project.name
