// Reader on one scanned subscription for the scan identity: all the scan needs to list resources.
// The principal ID arrives as a parameter, so getRoleAssignmentName() can name the assignment:
// https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/scenarios-rbac
// https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/bicep-functions-resource#getroleassignmentname
targetScope = 'subscription'

@description('Principal (object) ID of the scan identity.')
param principalId string

resource reader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: getRoleAssignmentName({
    scope: subscription().id
    principalId: principalId
    roleDefinitionId: roleDefinitions('Reader').id
  })
  properties: {
    roleDefinitionId: roleDefinitions('Reader').id
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
