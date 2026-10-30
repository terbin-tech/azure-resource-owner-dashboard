// The whole deployment: the dashboard's resource group with everything in it, and Reader for the scan
// identity on each scanned subscription.
// https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/deploy-to-subscription
targetScope = 'subscription'

@description('Azure region for the resource group and everything in it.')
param location string

@description('Name of the dashboard resource group. The scan reads it from settings.json too.')
param resourceGroupName string = 'resource-owner-dashboard'

@description('IDs of the subscriptions the scan reads. The scan identity gets Reader on each.')
param scanSubscriptionIds string[]

@description('Email address the alerts go to. Empty sends no email.')
param alertEmail string = ''

@description('False switches the scheduled scan and its missed-run alert off.')
param scheduleEnabled bool = true

@description('Client ID of the app registration for sign-in. Empty leaves sign-in off.')
param authClientId string = ''

// https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/create-resource-group
resource rg 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
}

module app 'app.bicep' = {
  scope: rg
  params: {
    location: location
    alertEmail: alertEmail
    scheduleEnabled: scheduleEnabled
    authClientId: authClientId
  }
}

module readers 'reader.bicep' = [
  for id in scanSubscriptionIds: {
    scope: subscription(id)
    params: {
      principalId: app.outputs.scanPrincipalId
    }
  }
]

output resourceGroupName string = rg.name
output functionAppName string = app.outputs.functionAppName
output defaultHostName string = app.outputs.defaultHostName
output scanPrincipalId string = app.outputs.scanPrincipalId
output authPrincipalId string = app.outputs.authPrincipalId
output dataAccountName string = app.outputs.dataAccountName
output workspaceCustomerId string = app.outputs.workspaceCustomerId
