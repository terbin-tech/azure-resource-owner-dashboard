// Copy to main.bicepparam (gitignored) and fill in. Never commit it.
// https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/parameter-files
using 'main.bicep'

param location = '<region>'
param resourceGroupName = 'resource-owner-dashboard'
param scanSubscriptionIds = ['00000000-0000-0000-0000-000000000001']
param alertEmail = ''
param scheduleEnabled = false
param authClientId = ''
