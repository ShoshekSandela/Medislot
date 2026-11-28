[CmdletBinding()]
param(
    [string]$RoleName = "MediSlot-GitHubActions-Role",
    [string]$PolicyName = "MediSlot-GitHubActions-RolePolicy",
    [string]$Region = "ap-southeast-2"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PolicyPath = Join-Path $ProjectRoot "iam/github-actions-deployment-policy.json"

if (-not (Test-Path $PolicyPath)) {
    throw "Policy file not found: $PolicyPath"
}

# Validate JSON locally before calling AWS.
try {
    $null = Get-Content -Raw -Path $PolicyPath | ConvertFrom-Json
}
catch {
    throw "Invalid JSON in $PolicyPath. $($_.Exception.Message)"
}

# Confirm the caller is using the intended AWS account.
$identity = aws sts get-caller-identity --output json --region $Region | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) {
    throw "Unable to identify the active AWS CLI identity. Check your AWS credentials."
}
if ($identity.Account -ne "292341339141") {
    throw "Wrong AWS account. Expected 292341339141, got $($identity.Account)."
}

Write-Host "Applying $PolicyPath to inline policy '$PolicyName' on role '$RoleName'..." -ForegroundColor Cyan
aws iam put-role-policy `
    --role-name $RoleName `
    --policy-name $PolicyName `
    --policy-document "file://$PolicyPath" `
    --region $Region

if ($LASTEXITCODE -ne 0) {
    throw "AWS rejected the policy update. Confirm this identity can perform iam:PutRolePolicy on $RoleName."
}

Write-Host "Policy update submitted successfully." -ForegroundColor Green
Write-Host "Verifying the inline policy attachment..."
aws iam get-role-policy `
    --role-name $RoleName `
    --policy-name $PolicyName `
    --region $Region `
    --query "PolicyDocument.Statement[*].Sid"

if ($LASTEXITCODE -ne 0) {
    throw "Policy was submitted, but verification failed."
}
