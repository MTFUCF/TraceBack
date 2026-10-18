param(
  [Parameter(Mandatory=$false)][string]$SubscriptionId = $env:TB_SUBSCRIPTION_ID,
  [Parameter(Mandatory=$false)][string]$ResourceGroup = $env:TB_RESOURCE_GROUP,
  [Parameter(Mandatory=$false)][string]$DelegatedAppRegName = $env:TB_DELEGATED_APP_REG_NAME,
  [switch]$KeepAppRegistration
)
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($SubscriptionId)) { throw 'Set TB_SUBSCRIPTION_ID or pass -SubscriptionId.' }
if ([string]::IsNullOrWhiteSpace($ResourceGroup)) { throw 'Set TB_RESOURCE_GROUP or pass -ResourceGroup.' }
if (-not $ResourceGroup.StartsWith('tb-')) { throw 'Refusing to delete a resource group that does not start with tb-.' }
az account set --subscription $SubscriptionId
az group delete --name $ResourceGroup --yes --no-wait
if (-not $KeepAppRegistration -and -not [string]::IsNullOrWhiteSpace($DelegatedAppRegName)) {
  $apps = az ad app list --display-name $DelegatedAppRegName -o json | ConvertFrom-Json
  foreach ($app in $apps) { az ad app delete --id $app.appId }
}
