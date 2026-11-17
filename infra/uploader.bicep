targetScope = 'resourceGroup'

@minLength(1)
@maxLength(12)
param environment string

@description('Must match the existing VNet region, which can differ from the Storage region.')
param location string

@description('Dedicated /27-or-larger subnet delegated to Microsoft.App/environments.')
param infrastructureSubnetId string

param storageAccountName string
param rawContainer string = 'raw'
param imageTag string = ''

@secure()
param uploadToken string = ''

@minValue(1)
@maxValue(134217728)
param maxUploadBytes int = 33554432

param contentUnderstandingEnabled bool = true

var tags = {
  workload: 'document-uploader'
  environment: environment
}
var appName = 'ca-uploader-${environment}'

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = {
  name: '${storage.name}/default/${rawContainer}'
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-uploader-${environment}'
  location: location
  tags: tags
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' = {
  name: 'acruploader${uniqueString(resourceGroup().id, environment)}'
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-uploader-${environment}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource managedEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: 'cae-uploader-${environment}'
  location: location
  tags: tags
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: infrastructureSubnetId
      internal: false
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource storageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(container.id, identity.id, 'blob-contributor')
  scope: container
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource pullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, 'acr-pull')
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource app 'Microsoft.App/containerApps@2025-01-01' = if (!empty(imageTag)) {
  name: appName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    environmentId: managedEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
      secrets: [
        {
          name: 'upload-token'
          value: uploadToken
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'uploader'
          image: '${registry.properties.loginServer}/${imageTag}'
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            {
              name: 'AZURE_CLIENT_ID'
              value: identity.properties.clientId
            }
            {
              name: 'UPLOADER_TOKEN'
              secretRef: 'upload-token'
            }
            {
              name: 'UPLOADER_STORAGE_ACCOUNT'
              value: storageAccountName
            }
            {
              name: 'UPLOADER_CONTAINER'
              value: rawContainer
            }
            {
              name: 'UPLOADER_MAX_BYTES'
              value: string(maxUploadBytes)
            }
            {
              name: 'UPLOADER_CU_ENABLED'
              value: toLower(string(contentUnderstandingEnabled))
            }
            {
              name: 'UPLOADER_REQUIRE_PRIVATE_STORAGE'
              value: 'true'
            }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/healthz'
                port: 8080
              }
              periodSeconds: 5
              timeoutSeconds: 2
              failureThreshold: 10
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8080
              }
              periodSeconds: 30
              timeoutSeconds: 2
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 1
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '1'
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: [
    storageRole
    pullRole
  ]
}

output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output containerAppName string = appName
output environmentName string = managedEnvironment.name
output identityId string = identity.id
output url string = !empty(imageTag) ? 'https://${app!.properties.configuration.ingress.fqdn}' : ''
