param(
  [Parameter(Mandatory=$false)][string]$TenantId = $env:TB_TENANT_ID,
  [Parameter(Mandatory=$false)][string]$SubscriptionId = $env:TB_SUBSCRIPTION_ID,
  [Parameter(Mandatory=$false)][string]$Location = $(if ($env:TB_LOCATION) { $env:TB_LOCATION } else { 'eastus2' }),
  [Parameter(Mandatory=$false)][string]$ResourceGroup = $env:TB_RESOURCE_GROUP,
  [Parameter(Mandatory=$false)][string]$LawResourceGroup = $env:TB_LAW_RESOURCE_GROUP,
  [Parameter(Mandatory=$false)][string]$LawName = $env:TB_LAW_NAME,
  [Parameter(Mandatory=$false)][string]$Prefix = $(if ($env:TB_PREFIX) { $env:TB_PREFIX } else { 'tb' }),
  [Parameter(Mandatory=$false)][string]$Suffix = $(if ($env:TB_SUFFIX) { $env:TB_SUFFIX } else { 'rig' }),
  [Parameter(Mandatory=$false)][string]$ModelName = $(if ($env:TB_MODEL_NAME) { $env:TB_MODEL_NAME } else { 'gpt-4o-mini' }),
  [Parameter(Mandatory=$false)][string]$ModelVersion = $(if ($env:TB_MODEL_VERSION) { $env:TB_MODEL_VERSION } else { '2024-07-18' }),
  [Parameter(Mandatory=$false)][string]$ModelSkuName = $(if ($env:TB_MODEL_SKU_NAME) { $env:TB_MODEL_SKU_NAME } else { 'GlobalStandard' }),
  [Parameter(Mandatory=$false)][int]$ModelCapacity = $(if ($env:TB_MODEL_CAPACITY) { [int]$env:TB_MODEL_CAPACITY } else { 1 }),
  [Parameter(Mandatory=$false)][string]$TestTargetUser = $env:TB_TEST_TARGET_USER,
  [switch]$SkipImageBuild
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Require-Value([string]$Name, [string]$Value) {
  if ([string]::IsNullOrWhiteSpace($Value)) { throw "Set $Name or pass -$($Name.TrimStart('TB_'))." }
}
function Invoke-AzJson([string[]]$AzArgs) {
  $out = & az @AzArgs -o json
  if ($LASTEXITCODE -ne 0) { throw "az $($AzArgs -join ' ') failed" }
  if ([string]::IsNullOrWhiteSpace($out)) { return $null }
  return $out | ConvertFrom-Json
}
function Invoke-AzTsv([string[]]$AzArgs) {
  $out = & az @AzArgs -o tsv
  if ($LASTEXITCODE -ne 0) { throw "az $($AzArgs -join ' ') failed" }
  return ($out | Select-Object -First 1)
}
function Ensure-DiagnosticSetting([string]$ResourceId, [string]$Name, [string[]]$WantedLogCategories, [switch]$Metrics) {
  $cats = Invoke-AzJson @('monitor','diagnostic-settings','categories','list','--resource',$ResourceId)
  $supportedLogs = @($cats.value | Where-Object { $_.categoryType -eq 'Logs' } | ForEach-Object { $_.name })
  $supportedMetrics = @($cats.value | Where-Object { $_.categoryType -eq 'Metrics' } | ForEach-Object { $_.name })
  $selectedLogs = @($WantedLogCategories | Where-Object { $supportedLogs -contains $_ } | ForEach-Object { @{ category=$_; enabled=$true } })
  $logArgs = @()
  if ($selectedLogs.Count -gt 0) { $logArgs = @('--logs', ($selectedLogs | ConvertTo-Json -Depth 5 -Compress)) }
  $metricArgs = @()
  if ($Metrics -and ($supportedMetrics -contains 'AllMetrics')) { $metricArgs = @('--metrics', '[{"category":"AllMetrics","enabled":true}]') }
  if (($logArgs.Count + $metricArgs.Count) -eq 0) { return @() }
  $created = Invoke-AzJson (@('monitor','diagnostic-settings','create','--resource',$ResourceId,'--name',$Name,'--workspace',$script:LawResourceId) + $logArgs + $metricArgs)
  return $created
}
function Get-GraphPermissionId([string]$Value, [string]$Kind) {
  $graphSp = Invoke-AzJson @('ad','sp','show','--id','00000003-0000-0000-c000-000000000000')
  if ($Kind -eq 'Scope') { return (($graphSp.oauth2PermissionScopes | Where-Object { $_.value -eq $Value -and $_.isEnabled }).id | Select-Object -First 1) }
  return (($graphSp.appRoles | Where-Object { $_.value -eq $Value -and $_.isEnabled }).id | Select-Object -First 1)
}
function Ensure-DelegatedApp([string]$DisplayName) {
  $existing = Invoke-AzJson @('ad','app','list','--display-name',$DisplayName)
  if (@($existing).Count -gt 0) { $app = @($existing)[0] } else { $app = Invoke-AzJson @('ad','app','create','--display-name',$DisplayName,'--sign-in-audience','AzureADMyOrg') }
  $appId = $app.appId
  $objectId = $app.id
  $app = Invoke-AzJson @('ad','app','show','--id',$appId)
  $scopeId = Get-GraphPermissionId 'User.ReadBasic.All' 'Scope'
  $existingScope = @($app.api.oauth2PermissionScopes | Where-Object { $_.value -eq 'access_as_user' } | Select-Object -First 1)
  if (@($existingScope).Count -gt 0 -and $existingScope[0].id) { $scopeGuid = $existingScope[0].id } else { $scopeGuid = [guid]::NewGuid().Guid }
  $identifierUri = "api://$appId"
  $bodyObj = @{
    identifierUris = @($identifierUri)
    api = @{
      oauth2PermissionScopes = @(@{
        adminConsentDescription = 'Allow the application to access the TraceBack test-rig MCP server as the signed-in user.'
        adminConsentDisplayName = 'Access TraceBack test-rig MCP server as user'
        id = $scopeGuid
        isEnabled = $true
        type = 'User'
        userConsentDescription = 'Allow the application to access the TraceBack test-rig MCP server as you.'
        userConsentDisplayName = 'Access TraceBack test-rig MCP server as you'
        value = 'access_as_user'
      })
    }
    requiredResourceAccess = @(@{ resourceAppId='00000003-0000-0000-c000-000000000000'; resourceAccess=@(@{ id=$scopeId; type='Scope' }) })
  }
  $body = $bodyObj | ConvertTo-Json -Depth 20 -Compress
  $bodyPath = Join-Path 'testrig' '.delegated-app-patch.json'
  $body | Set-Content -Path $bodyPath -Encoding UTF8
  try { $null = Invoke-AzJson @('rest','--method','PATCH','--url',"https://graph.microsoft.com/v1.0/applications/$objectId",'--headers','Content-Type=application/json','--body',"@$bodyPath") } finally { Remove-Item -Path $bodyPath -Force -ErrorAction SilentlyContinue }
  $null = & az ad sp create --id $appId -o none 2>$null
  return Invoke-AzJson @('ad','app','show','--id',$appId)
}

Require-Value 'TB_TENANT_ID' $TenantId
Require-Value 'TB_SUBSCRIPTION_ID' $SubscriptionId
Require-Value 'TB_LAW_RESOURCE_GROUP' $LawResourceGroup
Require-Value 'TB_LAW_NAME' $LawName
if ([string]::IsNullOrWhiteSpace($ResourceGroup)) { $ResourceGroup = "$Prefix-$Suffix-rg" }
if (-not $ResourceGroup.StartsWith('tb-')) { throw 'ResourceGroup must start with tb-.' }

az account set --subscription $SubscriptionId
$acct = Invoke-AzJson @('account','show')
if ($acct.tenantId -ne $TenantId) { throw "Active tenant $($acct.tenantId) does not match TenantId $TenantId." }
$script:LawResourceId = Invoke-AzTsv @('monitor','log-analytics','workspace','show','--resource-group',$LawResourceGroup,'--workspace-name',$LawName,'--query','id')

$modelRows = Invoke-AzJson @('cognitiveservices','model','list','--location',$Location,'--subscription',$SubscriptionId,'--query',"[?model.name=='$ModelName' && model.version=='$ModelVersion']")
if (-not $modelRows -or @($modelRows).Count -eq 0) { throw "$ModelName $ModelVersion is not listed in $Location." }
$skuNames = @($modelRows | ForEach-Object { $_.model.skus } | ForEach-Object { $_.name })
if ($skuNames -notcontains $ModelSkuName) { throw "$ModelName $ModelVersion does not support SKU $ModelSkuName in $Location." }
$quota = Invoke-AzJson @('cognitiveservices','usage','list','--location',$Location,'--subscription',$SubscriptionId,'--query',"[?name.value=='OpenAI.$ModelSkuName.$ModelName'] | [0]")
if (-not $quota) { throw "No quota row for OpenAI.$ModelSkuName.$ModelName in $Location." }
$available = [double]$quota.limit - [double]$quota.currentValue
if ($available -lt $ModelCapacity) { throw "Insufficient quota for $ModelName in $Location. Available=$available, required=$ModelCapacity." }

$accountName = "$Prefix-foundry-$Suffix"
$projectName = "$Prefix-project-$Suffix"
$modelDeploymentName = "$Prefix-gpt-4o-mini-$Suffix"
$appInsightsName = "$Prefix-appi-$Suffix"
$acaEnvName = "$Prefix-aca-env-$Suffix"
$delegatedAppName = "$Prefix-mcp-delegated-$Suffix"
$appOnlyAppName = "$Prefix-mcp-apponly-$Suffix"
$delegatedAppRegName = "$Prefix-mcp-delegated-appreg-$Suffix"
$miName = "$Prefix-mcp-apponly-mi-$Suffix"
$acrName = (($Prefix + 'acr' + $Suffix) -replace '[^A-Za-z0-9]', '').ToLowerInvariant()
if ($acrName.Length -gt 50) { $acrName = $acrName.Substring(0,50) }
$imageTag = "$acrName.azurecr.io/tb-mcp-server:$(Get-Date -Format yyyyMMddHHmmss)"
$diagName = "$Prefix-diag-$Suffix"

$tags = @('purpose=trackback-test-rig',"owner=$($acct.user.name)",'costCapUsdMonthly=100')
$null = Invoke-AzJson (@('group','create','--name',$ResourceGroup,'--location',$Location,'--tags') + $tags)

$appi = Invoke-AzJson (@('monitor','app-insights','component','create','--app',$appInsightsName,'--location',$Location,'--resource-group',$ResourceGroup,'--workspace',$script:LawResourceId,'--application-type','web','--tags') + $tags)
$appiId = Invoke-AzTsv @('monitor','app-insights','component','show','--app',$appInsightsName,'--resource-group',$ResourceGroup,'--query','id')
$appiConn = Invoke-AzTsv @('monitor','app-insights','component','show','--app',$appInsightsName,'--resource-group',$ResourceGroup,'--query','connectionString')

$foundryExisting = & az cognitiveservices account show --name $accountName --resource-group $ResourceGroup -o json 2>$null
if ($LASTEXITCODE -eq 0 -and $foundryExisting) {
  $foundry = $foundryExisting | ConvertFrom-Json
} else {
  $tagObj = @{ purpose='trackback-test-rig'; owner=$acct.user.name; costCapUsdMonthly='100' }
  $foundryBody = @{ location=$Location; kind='AIServices'; sku=@{ name='S0' }; properties=@{ customSubDomainName=$accountName; publicNetworkAccess='Enabled'; allowProjectManagement=$true }; tags=$tagObj } | ConvertTo-Json -Depth 20 -Compress
  $foundryBodyPath = Join-Path 'testrig' '.foundry-account.json'
  $foundryBody | Set-Content -Path $foundryBodyPath -Encoding UTF8
  try {
    $foundry = Invoke-AzJson @('rest','--method','PUT','--url',"https://management.azure.com/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.CognitiveServices/accounts/${accountName}?api-version=2025-06-01",'--headers','Content-Type=application/json','--body',"@$foundryBodyPath")
  } finally { Remove-Item -Path $foundryBodyPath -Force -ErrorAction SilentlyContinue }
}
$foundryId = Invoke-AzTsv @('cognitiveservices','account','show','--name',$accountName,'--resource-group',$ResourceGroup,'--query','id')
$proj = Invoke-AzJson @('cognitiveservices','account','project','create','--name',$accountName,'--project-name',$projectName,'--resource-group',$ResourceGroup,'--location',$Location,'--display-name',$projectName)
$projectId = Invoke-AzTsv @('cognitiveservices','account','project','show','--name',$accountName,'--project-name',$projectName,'--resource-group',$ResourceGroup,'--query','id')
$null = Invoke-AzJson @('cognitiveservices','account','deployment','create','--name',$accountName,'--resource-group',$ResourceGroup,'--deployment-name',$modelDeploymentName,'--model-name',$ModelName,'--model-version',$ModelVersion,'--model-format','OpenAI','--sku-name',$ModelSkuName,'--sku-capacity',"$ModelCapacity")

$delegatedApp = Ensure-DelegatedApp $delegatedAppRegName
$mi = Invoke-AzJson (@('identity','create','--name',$miName,'--resource-group',$ResourceGroup,'--location',$Location,'--tags') + $tags)
$miId = Invoke-AzTsv @('identity','show','--name',$miName,'--resource-group',$ResourceGroup,'--query','id')
$miClientId = Invoke-AzTsv @('identity','show','--name',$miName,'--resource-group',$ResourceGroup,'--query','clientId')
$miPrincipalId = Invoke-AzTsv @('identity','show','--name',$miName,'--resource-group',$ResourceGroup,'--query','principalId')

$acr = Invoke-AzJson (@('acr','create','--name',$acrName,'--resource-group',$ResourceGroup,'--location',$Location,'--sku','Basic','--admin-enabled','false','--tags') + $tags)
$acrId = Invoke-AzTsv @('acr','show','--name',$acrName,'--resource-group',$ResourceGroup,'--query','id')
$acrLogin = Invoke-AzTsv @('acr','show','--name',$acrName,'--resource-group',$ResourceGroup,'--query','loginServer')
if (-not $SkipImageBuild) { az acr build --registry $acrName --image $imageTag --no-logs 'testrig\mcp-server' | Write-Host }

$envExisting = & az containerapp env show --name $acaEnvName --resource-group $ResourceGroup -o json 2>$null
if ($LASTEXITCODE -eq 0 -and $envExisting) { $env = $envExisting | ConvertFrom-Json } else { $env = Invoke-AzJson (@('containerapp','env','create','--name',$acaEnvName,'--resource-group',$ResourceGroup,'--location',$Location,'--logs-destination','azure-monitor','--tags') + $tags) }
$envId = Invoke-AzTsv @('containerapp','env','show','--name',$acaEnvName,'--resource-group',$ResourceGroup,'--query','id')

$commonSecrets = @("appinsights-connection-string=$appiConn")
$delegatedExisting = & az containerapp show --name $delegatedAppName --resource-group $ResourceGroup -o json 2>$null
if ($LASTEXITCODE -eq 0 -and $delegatedExisting) { $delegated = $delegatedExisting | ConvertFrom-Json } else { $delegated = Invoke-AzJson (@('containerapp','create','--name',$delegatedAppName,'--resource-group',$ResourceGroup,'--environment',$acaEnvName,'--image',$imageTag,'--ingress','external','--target-port','8000','--min-replicas','0','--max-replicas','1','--cpu','0.25','--memory','0.5Gi','--system-assigned','--registry-server',$acrLogin,'--registry-identity','system','--secrets') + $commonSecrets + @('--env-vars','TB_AUTH_MODE=delegated','TB_GRAPH_SCOPES=User.ReadBasic.All',"TB_TENANT_ID=$TenantId","TB_CLIENT_ID=$($delegatedApp.appId)",'APPLICATIONINSIGHTS_CONNECTION_STRING=secretref:appinsights-connection-string','OTEL_SERVICE_NAME=tb-mcp-delegated')) }
$appOnlyExisting = & az containerapp show --name $appOnlyAppName --resource-group $ResourceGroup -o json 2>$null
if ($LASTEXITCODE -eq 0 -and $appOnlyExisting) { $appOnly = $appOnlyExisting | ConvertFrom-Json } else { $appOnly = Invoke-AzJson (@('containerapp','create','--name',$appOnlyAppName,'--resource-group',$ResourceGroup,'--environment',$acaEnvName,'--image',$imageTag,'--ingress','external','--target-port','8000','--min-replicas','0','--max-replicas','1','--cpu','0.25','--memory','0.5Gi','--system-assigned','--user-assigned',$miId,'--registry-server',$acrLogin,'--registry-identity','system','--secrets') + $commonSecrets + @('--env-vars','TB_AUTH_MODE=application','TB_GRAPH_SCOPES=https://graph.microsoft.com/.default',"TB_MANAGED_IDENTITY_CLIENT_ID=$miClientId",'APPLICATIONINSIGHTS_CONNECTION_STRING=secretref:appinsights-connection-string','OTEL_SERVICE_NAME=tb-mcp-apponly')) }
$delegatedId = Invoke-AzTsv @('containerapp','show','--name',$delegatedAppName,'--resource-group',$ResourceGroup,'--query','id')
$appOnlyId = Invoke-AzTsv @('containerapp','show','--name',$appOnlyAppName,'--resource-group',$ResourceGroup,'--query','id')
$delegatedPrincipalId = Invoke-AzTsv @('containerapp','show','--name',$delegatedAppName,'--resource-group',$ResourceGroup,'--query','identity.principalId')
$appOnlySystemPrincipalId = Invoke-AzTsv @('containerapp','show','--name',$appOnlyAppName,'--resource-group',$ResourceGroup,'--query','identity.principalId')
foreach ($principalObjectId in @($delegatedPrincipalId,$appOnlySystemPrincipalId)) {
  if ($principalObjectId) { az role assignment create --assignee-object-id $principalObjectId --assignee-principal-type ServicePrincipal --role AcrPull --scope $acrId -o none 2>$null }
}

$foundryDiag = Ensure-DiagnosticSetting $foundryId $diagName @('Audit','RequestResponse','AzureOpenAIRequestUsage') -Metrics
$projectDiag = Ensure-DiagnosticSetting $projectId $diagName @('Audit','Trace') -Metrics
$envDiag = Ensure-DiagnosticSetting $envId $diagName @('ContainerAppHTTPLogs','ContainerAppConsoleLogs','ContainerAppSystemLogs') -Metrics
$delegatedDiag = Ensure-DiagnosticSetting $delegatedId $diagName @() -Metrics
$appOnlyDiag = Ensure-DiagnosticSetting $appOnlyId $diagName @() -Metrics

$delegatedFqdn = Invoke-AzTsv @('containerapp','show','--name',$delegatedAppName,'--resource-group',$ResourceGroup,'--query','properties.configuration.ingress.fqdn')
$appOnlyFqdn = Invoke-AzTsv @('containerapp','show','--name',$appOnlyAppName,'--resource-group',$ResourceGroup,'--query','properties.configuration.ingress.fqdn')
$delegatedScale = Invoke-AzJson @('containerapp','show','--name',$delegatedAppName,'--resource-group',$ResourceGroup,'--query','properties.template.scale')
$appOnlyScale = Invoke-AzJson @('containerapp','show','--name',$appOnlyAppName,'--resource-group',$ResourceGroup,'--query','properties.template.scale')

$healthDelegated = Invoke-RestMethod -Uri "https://$delegatedFqdn/health" -TimeoutSec 60
$healthAppOnly = Invoke-RestMethod -Uri "https://$appOnlyFqdn/health" -TimeoutSec 60

$tables = @()
try {
  $query = "union withsource=TableName isfuzzy=true ContainerAppHTTPLogs,ContainerAppConsoleLogs,ContainerAppSystemLogs,AzureDiagnostics,AzureMetrics,AppRequests,AppDependencies,AppTraces | where TimeGenerated > ago(30m) | summarize Rows=count() by TableName | order by TableName asc"
  $tables = Invoke-AzJson @('monitor','log-analytics','query','--workspace',$script:LawResourceId,'--analytics-query',$query)
} catch { $tables = @(@{ warning = $_.Exception.Message }) }

$output = [ordered]@{
  generatedUtc = (Get-Date).ToUniversalTime().ToString('o')
  tenantId = $TenantId
  subscriptionId = $SubscriptionId
  location = $Location
  resourceGroup = $ResourceGroup
  logAnalyticsWorkspaceResourceId = $script:LawResourceId
  quota = @{ model=$ModelName; version=$ModelVersion; sku=$ModelSkuName; current=$quota.currentValue; limit=$quota.limit; available=$available; deployedCapacity=$ModelCapacity }
  resources = @{
    applicationInsights = @{ name=$appInsightsName; id=$appiId }
    foundryAccount = @{ name=$accountName; id=$foundryId }
    foundryProject = @{ name=$projectName; id=$projectId }
    modelDeployment = @{ name=$modelDeploymentName; model=$ModelName; version=$ModelVersion; sku=$ModelSkuName; capacity=$ModelCapacity }
    containerAppsEnvironment = @{ name=$acaEnvName; id=$envId }
    acr = @{ name=$acrName; id=$acrId; loginServer=$acrLogin; image=$imageTag }
    delegatedContainerApp = @{ name=$delegatedAppName; id=$delegatedId; fqdn=$delegatedFqdn; health=$healthDelegated; scale=$delegatedScale }
    appOnlyContainerApp = @{ name=$appOnlyAppName; id=$appOnlyId; fqdn=$appOnlyFqdn; health=$healthAppOnly; scale=$appOnlyScale }
    appOnlyManagedIdentity = @{ name=$miName; id=$miId; clientId=$miClientId; principalId=$miPrincipalId }
    delegatedAppRegistration = @{ displayName=$delegatedAppRegName; appId=$delegatedApp.appId; objectId=$delegatedApp.id; requestedGraphDelegatedPermission='User.ReadBasic.All'; exposedScope='access_as_user' }
  }
  diagnosticSettings = @{
    name=$diagName
    foundryAccount=$foundryDiag
    foundryProject=$projectDiag
    containerAppsEnvironment=$envDiag
    delegatedContainerApp=$delegatedDiag
    appOnlyContainerApp=$appOnlyDiag
  }
  observedTables = $tables
  humanSteps = @(
    "Grant admin consent for app registration '$delegatedAppRegName' ($($delegatedApp.appId)) to Microsoft Graph delegated permission User.ReadBasic.All.",
    "Grant Microsoft Graph application permission User.ReadBasic.All to managed identity '$miName' (client ID $miClientId, object/principal ID $miPrincipalId).",
    "Configure Copilot/Foundry to call https://$delegatedFqdn/mcp for delegated mode and https://$appOnlyFqdn/mcp for app-only mode."
  )
}
$output | ConvertTo-Json -Depth 100 | Set-Content -Path 'testrig\deployment-state.json' -Encoding UTF8
Write-Output ($output | ConvertTo-Json -Depth 100)

