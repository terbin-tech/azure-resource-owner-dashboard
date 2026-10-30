// Everything in the dashboard's resource group: two identities, two storage accounts, monitoring,
// the Flex Consumption function app and its alerts.

@description('Azure region for every resource here.')
param location string

@description('Email address the alerts go to. Empty sends no email.')
param alertEmail string

@description('False switches the scheduled scan and its missed-run alert off.')
param scheduleEnabled bool

@description('Client ID of the app registration for sign-in. Empty leaves sign-in off.')
param authClientId string

var suffix = uniqueString(resourceGroup().id)

// Application Insights tables, which otherwise keep data 90 days:
// https://learn.microsoft.com/en-us/azure/azure-monitor/logs/data-retention-configure
var appTables = [
  'AppAvailabilityResults'
  'AppBrowserTimings'
  'AppDependencies'
  'AppEvents'
  'AppExceptions'
  'AppMetrics'
  'AppPageViews'
  'AppPerformanceCounters'
  'AppRequests'
  'AppSystemEvents'
  'AppTraces'
]

// `scan` runs the function: host storage, deployment storage, the page, the roster and telemetry.
// `auth` only signs the sign-in client assertion (a federated credential on the app registration),
// so it gets no role.
resource scan 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: 'scan'
  location: location
}

resource auth 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: 'auth'
  location: location
}

// Both accounts: Entra ID only, no shared key, no anonymous blob access, TLS 1.2.
// https://learn.microsoft.com/en-us/azure/templates/microsoft.storage/storageaccounts?pivots=deployment-language-bicep
// https://learn.microsoft.com/en-us/azure/storage/common/shared-key-authorization-prevent
resource host 'Microsoft.Storage/storageAccounts@2026-06-01' = {
  name: 'dashhost${suffix}'
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    defaultToOAuthAuthentication: true
    allowCrossTenantReplication: false
  }
}

resource hostBlobs 'Microsoft.Storage/storageAccounts/blobServices@2026-06-01' = {
  parent: host
  name: 'default'
}

resource deployments 'Microsoft.Storage/storageAccounts/blobServices/containers@2026-06-01' = {
  parent: hostBlobs
  name: 'deployments'
  properties: {
    publicAccess: 'None'
  }
}

resource data 'Microsoft.Storage/storageAccounts@2026-06-01' = {
  name: 'dashdata${suffix}'
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    defaultToOAuthAuthentication: true
    allowCrossTenantReplication: false
  }
}

// Versioning keeps the previous page; soft delete is off so an expired version is gone.
resource dataBlobs 'Microsoft.Storage/storageAccounts/blobServices@2026-06-01' = {
  parent: data
  name: 'default'
  properties: {
    isVersioningEnabled: true
    deleteRetentionPolicy: {
      enabled: false
    }
    containerDeleteRetentionPolicy: {
      enabled: false
    }
  }
}

resource site 'Microsoft.Storage/storageAccounts/blobServices/containers@2026-06-01' = {
  parent: dataBlobs
  name: 'site'
  properties: {
    publicAccess: 'None'
  }
}

resource config 'Microsoft.Storage/storageAccounts/blobServices/containers@2026-06-01' = {
  parent: dataBlobs
  name: 'config'
  properties: {
    publicAccess: 'None'
  }
}

// A previous version counts from when it was first written:
// https://learn.microsoft.com/en-us/azure/storage/blobs/lifecycle-management-policy-structure
resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2026-06-01' = {
  parent: data
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'expire-previous-versions'
          enabled: true
          type: 'Lifecycle'
          definition: {
            actions: {
              version: {
                delete: {
                  daysAfterCreationGreaterThan: 1
                }
              }
            }
            filters: {
              blobTypes: ['blockBlob']
              prefixMatch: ['site/', 'config/']
            }
          }
        }
      ]
    }
  }
}

resource workspace 'Microsoft.OperationalInsights/workspaces@2026-03-01' = {
  name: 'log-${suffix}'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    // Nothing here uses the workspace's shared keys: Application Insights ingests with Entra, the
    // alerts query as managed identities, and the Logs API takes only Entra tokens. "When set to
    // `true`, this property enforces that Microsoft Entra authentication must be used for all
    // access"; it stops the legacy Log Analytics agents and the Data Collector API, neither used here:
    // https://learn.microsoft.com/en-us/previous-versions/azure/azure-monitor/logs/azure-ad-authentication-logs
    // The current property reference ("Disable Non-AAD based Auth."):
    // https://learn.microsoft.com/en-us/azure/templates/microsoft.operationalinsights/workspaces
    features: {
      disableLocalAuth: true
    }
  }
}

// "The PUT API sets retentionInDays and totalRetentionInDays to the default value if you don't set
// non-null values":
// https://learn.microsoft.com/en-us/azure/azure-monitor/logs/data-retention-configure
resource tables 'Microsoft.OperationalInsights/workspaces/tables@2026-03-01' = [
  for table in appTables: {
    parent: workspace
    name: table
    properties: {
      retentionInDays: 30
      totalRetentionInDays: 30
    }
  }
]

// Entra ID only, no instrumentation key ingestion:
// https://learn.microsoft.com/en-us/azure/azure-monitor/app/azure-ad-authentication
resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${suffix}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    DisableLocalAuth: true
  }
}

// Flex Consumption plan and app:
// https://learn.microsoft.com/en-us/azure/azure-functions/functions-infrastructure-as-code?pivots=flex-consumption-plan&tabs=bicep
// https://learn.microsoft.com/en-us/azure/templates/microsoft.web/sites?pivots=deployment-language-bicep
resource plan 'Microsoft.Web/serverfarms@2024-11-01' = {
  name: 'plan-${suffix}'
  location: location
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2024-11-01' = {
  name: 'resource-owner-dashboard-${suffix}'
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${scan.id}': {}
      '${auth.id}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      minTlsVersion: '1.2'
      // A deploy replaces the whole collection, so every setting lives here. None is from the
      // deprecation list:
      // https://learn.microsoft.com/en-us/azure/azure-functions/functions-app-settings#flex-consumption-plan-deprecations
      appSettings: [
        // Host storage over the scan identity, no key:
        // https://learn.microsoft.com/en-us/azure/azure-functions/how-to-create-function-azure-cli?pivots=programming-language-python#update-application-settings
        {
          name: 'AzureWebJobsStorage__accountName'
          value: host.name
        }
        {
          name: 'AzureWebJobsStorage__credential'
          value: 'managedidentity'
        }
        {
          name: 'AzureWebJobsStorage__clientId'
          value: scan.properties.clientId
        }
        // Telemetry with Entra ID:
        // https://learn.microsoft.com/en-us/azure/azure-functions/configure-monitoring?tabs=v2
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: insights.properties.ConnectionString
        }
        {
          name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING'
          value: 'ClientId=${scan.properties.clientId};Authorization=AAD'
        }
        // Sign-in uses the auth identity's client ID in place of a secret:
        // https://learn.microsoft.com/en-us/azure/app-service/configure-authentication-provider-aad#use-a-managed-identity-instead-of-a-secret
        {
          name: 'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID'
          value: auth.properties.clientId
        }
        // https://learn.microsoft.com/en-us/azure/azure-functions/disable-function
        {
          name: 'AzureWebJobs.scan.Disabled'
          value: scheduleEnabled ? 'false' : 'true'
        }
        {
          name: 'DASHBOARD_DATA_ACCOUNT'
          value: data.name
        }
        {
          name: 'DASHBOARD_SCAN_CLIENT_ID'
          value: scan.properties.clientId
        }
      ]
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${host.properties.primaryEndpoints.blob}${deployments.name}'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: scan.id
          }
        }
      }
      // 2,048 MB is the default; a second instance keeps a page request from waiting on a scan:
      // https://learn.microsoft.com/en-us/azure/azure-functions/flex-consumption-plan
      scaleAndConcurrency: {
        instanceMemoryMB: 2048
        maximumInstanceCount: 2
      }
      runtime: {
        name: 'python'
        version: '3.14'
      }
    }
  }
}

// No basic-auth publishing; deploys authenticate with Entra ID:
// https://learn.microsoft.com/en-us/azure/app-service/configure-basic-auth-disable
resource scm 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: functionApp
  name: 'scm'
  properties: {
    allow: false
  }
}

// Built-in sign-in, on once the app registration exists. Single tenant, this registration only,
// no token store, and the auth identity in place of a client secret:
// https://learn.microsoft.com/en-us/azure/app-service/configure-authentication-provider-aad
// https://learn.microsoft.com/en-us/rest/api/appservice/web-apps/update-auth-settings-v-2
resource signIn 'Microsoft.Web/sites/config@2024-11-01' = if (!empty(authClientId)) {
  parent: functionApp
  name: 'authsettingsV2'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'RedirectToLoginPage'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: authClientId
          clientSecretSettingName: 'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID'
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0'
        }
        validation: {
          defaultAuthorizationPolicy: {
            allowedApplications: [authClientId]
          }
        }
      }
    }
    login: {
      tokenStore: {
        enabled: false
      }
    }
    httpSettings: {
      requireHttps: true
    }
  }
}

// Role assignments, named with guid() because the identities are made in this file:
// https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/scenarios-rbac
// Host storage needs Blob Data Owner and Table Data Contributor:
// https://learn.microsoft.com/en-us/azure/azure-functions/manage-connections?pivots=functions-auth-identity&tabs=host
resource hostBlobOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: host
  name: guid(host.id, scan.id, roleDefinitions('Storage Blob Data Owner').id)
  properties: {
    roleDefinitionId: roleDefinitions('Storage Blob Data Owner').id
    principalId: scan.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource hostTableContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: host
  name: guid(host.id, scan.id, roleDefinitions('Storage Table Data Contributor').id)
  properties: {
    roleDefinitionId: roleDefinitions('Storage Table Data Contributor').id
    principalId: scan.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// The scan writes the page to `site` and only reads the roster in `config`.
resource siteContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: site
  name: guid(site.id, scan.id, roleDefinitions('Storage Blob Data Contributor').id)
  properties: {
    roleDefinitionId: roleDefinitions('Storage Blob Data Contributor').id
    principalId: scan.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource configReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: config
  name: guid(config.id, scan.id, roleDefinitions('Storage Blob Data Reader').id)
  properties: {
    roleDefinitionId: roleDefinitions('Storage Blob Data Reader').id
    principalId: scan.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// https://learn.microsoft.com/en-us/azure/azure-monitor/app/azure-ad-authentication
resource metricsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: insights
  name: guid(insights.id, scan.id, roleDefinitions('Monitoring Metrics Publisher').id)
  properties: {
    roleDefinitionId: roleDefinitions('Monitoring Metrics Publisher').id
    principalId: scan.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// https://learn.microsoft.com/en-us/azure/azure-monitor/alerts/action-groups
resource alerts 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'alerts'
  location: 'global'
  properties: {
    groupShortName: 'dashboard'
    enabled: true
    emailReceivers: empty(alertEmail)
      ? []
      : [
          {
            name: 'email'
            emailAddress: alertEmail
            useCommonAlertSchema: true
          }
        ]
  }
}

// Log search alerts. Each query ends in `summarize <N> = count()`, which returns one row (0 when
// nothing matched), so a missing run is a 0 and not an empty result:
// https://learn.microsoft.com/en-us/kusto/query/summarize-operator
// https://learn.microsoft.com/en-us/azure/azure-monitor/alerts/alerts-create-log-alert-rule
// Query validation is skipped: each rule's identity gets its workspace role only after the rule
// exists.
resource scanFailed 'Microsoft.Insights/scheduledQueryRules@2026-03-01' = {
  name: 'scan-failed'
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    description: 'A scheduled run failed. Check the function\'s logs (30 days), fix, run the smoke test.'
    severity: 1
    enabled: true
    scopes: [workspace.id]
    evaluationFrequency: 'PT1H'
    windowSize: 'PT1H'
    skipQueryValidation: true
    criteria: {
      allOf: [
        {
          query: 'AppRequests | where Name == "scan" and Success == false | summarize Failed = count()'
          metricMeasureColumn: 'Failed'
          timeAggregation: 'Total'
          operator: 'GreaterThan'
          threshold: 0
        }
      ]
    }
    actions: {
      actionGroups: [alerts.id]
    }
  }
}

// Runs are at most 14 hours apart, so 26 hours means two missed runs. The alert window can be at
// most two days, and queries take ago() with a literal only (same alerts page as above).
resource scanMissing 'Microsoft.Insights/scheduledQueryRules@2026-03-01' = {
  name: 'scan-missing'
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    description: 'No run for about 26 hours: the schedule may be broken or switched off by hand.'
    severity: 2
    enabled: scheduleEnabled
    scopes: [workspace.id]
    evaluationFrequency: 'PT1H'
    windowSize: 'P2D'
    skipQueryValidation: true
    criteria: {
      allOf: [
        {
          query: 'AppRequests | where TimeGenerated > ago(26h) and Name == "scan" and Success == true | summarize Runs = count()'
          metricMeasureColumn: 'Runs'
          timeAggregation: 'Total'
          operator: 'LessThan'
          threshold: 1
        }
      ]
    }
    actions: {
      actionGroups: [alerts.id]
    }
  }
}

resource scanIncomplete 'Microsoft.Insights/scheduledQueryRules@2026-03-01' = {
  name: 'scan-incomplete'
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    description: 'Part of a scheduled run was incomplete. Open the page: its banners name what.'
    severity: 3
    enabled: true
    scopes: [workspace.id]
    evaluationFrequency: 'PT1H'
    windowSize: 'PT1H'
    skipQueryValidation: true
    criteria: {
      allOf: [
        {
          query: 'AppTraces | where Message startswith "run incomplete: " | summarize Incomplete = count()'
          metricMeasureColumn: 'Incomplete'
          timeAggregation: 'Total'
          operator: 'GreaterThan'
          threshold: 0
        }
      ]
    }
    actions: {
      actionGroups: [alerts.id]
    }
  }
}

// "the identity must be assigned a reader role for all workspaces that the query accesses"
// (alerts page above).
resource scanFailedReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: workspace
  name: guid(workspace.id, scanFailed.id, roleDefinitions('Reader').id)
  properties: {
    roleDefinitionId: roleDefinitions('Reader').id
    principalId: scanFailed.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource scanMissingReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: workspace
  name: guid(workspace.id, scanMissing.id, roleDefinitions('Reader').id)
  properties: {
    roleDefinitionId: roleDefinitions('Reader').id
    principalId: scanMissing.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource scanIncompleteReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: workspace
  name: guid(workspace.id, scanIncomplete.id, roleDefinitions('Reader').id)
  properties: {
    roleDefinitionId: roleDefinitions('Reader').id
    principalId: scanIncomplete.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output functionAppName string = functionApp.name
output defaultHostName string = functionApp.properties.defaultHostName
output scanPrincipalId string = scan.properties.principalId
output authPrincipalId string = auth.properties.principalId
output dataAccountName string = data.name
output workspaceCustomerId string = workspace.properties.customerId
