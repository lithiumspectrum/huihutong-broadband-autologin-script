<#
.SYNOPSIS
    Parse HAR file to extract OpenID and login flow
.DESCRIPTION
    Parses a browser-exported HAR (HTTP Archive) file to find:
    - OpenID from api.215123.cn requests
    - Full login flow (redirect chain, API calls, tokens)
    - All requests to broadband.215123.cn and api.215123.cn
.NOTES
    Usage:
      1. In browser F12 Network tab, after login, right-click → Save all as HAR
      2. Run: powershell -ExecutionPolicy Bypass -File parse_har.ps1 -HarFile "path\to\file.har"
#>

param(
    [Parameter(Mandatory=$true)]
    [string]$HarFile
)

$ErrorActionPreference = "Continue"

if (-not (Test-Path $HarFile)) {
    Write-Host "File not found: $HarFile" -ForegroundColor Red
    exit 1
}

Write-Host "Parsing HAR file: $HarFile" -ForegroundColor Cyan
Write-Host ""

# Read and parse HAR file
$harContent = Get-Content $HarFile -Raw -Encoding UTF8
$har = $harContent | ConvertFrom-Json

$entries = $har.log.entries
Write-Host "Total requests in HAR: $($entries.Count)" -ForegroundColor Cyan
Write-Host ""

# Target domains
$targetDomains = @(
    "api.215123.cn",
    "broadband.215123.cn",
    "10.10.16.101"
)

# ==============================================================================
# 1. Find OpenID
# ==============================================================================
Write-Host "========== Step 1: Search for OpenID ==========" -ForegroundColor Yellow

$openIdFound = ""
$openIdSources = @()

foreach ($entry in $entries) {
    $url = $entry.request.url
    $method = $entry.request.method

    # Check URL for openId parameter
    if ($url -match "openId=([^&""\s]+)") {
        $openId = $Matches[1]
        if ($openId -ne "test_dummy_id" -and $openId.Length -gt 5) {
            $openIdFound = $openId
            $openIdSources += "URL param: $method $url"
        }
    }

    # Check POST body for openId
    if ($entry.request.postData) {
        $postData = $entry.request.postData.text
        if ($postData -match "openId=([^&""\s]+)") {
            $openId = $Matches[1]
            if ($openId -ne "test_dummy_id" -and $openId.Length -gt 5) {
                $openIdFound = $openId
                $openIdSources += "POST body: $method $url -> $postData"
            }
        }
        # Also check JSON body
        if ($postData -match '"openId"\s*:\s*"([^"]+)"') {
            $openId = $Matches[1]
            if ($openId.Length -gt 5) {
                $openIdFound = $openId
                $openIdSources += "POST JSON: $method $url -> $postData"
            }
        }
    }

    # Check response body for openId
    if ($entry.response.content.text) {
        $respBody = $entry.response.content.text
        if ($respBody -match '"openId"\s*:\s*"([^"]+)"') {
            $openId = $Matches[1]
            if ($openId.Length -gt 5) {
                $openIdFound = $openId
                $openIdSources += "Response body: $method $url"
            }
        }
        if ($respBody -match "openId=([^&""\s]+)") {
            $openId = $Matches[1]
            if ($openId.Length -gt 5) {
                $openIdFound = $openId
                $openIdSources += "Response redirect: $method $url"
            }
        }
    }
}

if ($openIdFound) {
    Write-Host "FOUND OpenID: $openIdFound" -ForegroundColor Green
    Write-Host ""
    Write-Host "Sources:"
    foreach ($src in $openIdSources) {
        Write-Host "  - $src"
    }
} else {
    Write-Host "OpenID not found in HAR file." -ForegroundColor Red
    Write-Host "Make sure you completed the full login (including WeChat scan) before exporting HAR."
}
Write-Host ""

# ==============================================================================
# 2. Find all requests to target domains
# ==============================================================================
Write-Host "========== Step 2: Requests to target domains ==========" -ForegroundColor Yellow

$targetEntries = @()
foreach ($entry in $entries) {
    $url = $entry.request.url
    foreach ($domain in $targetDomains) {
        if ($url -match $domain) {
            $targetEntries += $entry
            break
        }
    }
}

Write-Host "Found $($targetEntries.Count) requests to target domains."
Write-Host ""

$flowLog = @()
$flowLog += "=== Login Flow Capture ==="
$flowLog += "Total target requests: $($targetEntries.Count)"
$flowLog += ""

$i = 0
foreach ($entry in $targetEntries) {
    $i++
    $method = $entry.request.method
    $url = $entry.request.url
    $status = $entry.response.status
    $statusText = $entry.response.statusText
    $mimeType = $entry.response.content.mimeType

    $flowLog += "--- Request #$i ---"
    $flowLog += "Method: $method"
    $flowLog += "URL: $url"
    $flowLog += "Status: $status $statusText"
    $flowLog += "Type: $mimeType"

    # Request headers
    $reqHeaders = $entry.request.headers
    $interestingReqHeaders = @("satoken", "authorization", "cookie", "content-type", "referer")
    $flowLog += "Request Headers:"
    foreach ($h in $reqHeaders) {
        $name = $h.name.ToLower()
        if ($interestingReqHeaders -contains $name) {
            $flowLog += "  $($h.name): $($h.value)"
        }
    }

    # POST data
    if ($entry.request.postData -and $entry.request.postData.text) {
        $flowLog += "Request Body:"
        $flowLog += "  $($entry.request.postData.text)"
    }

    # Response Location header (for redirects)
    $respHeaders = $entry.response.headers
    foreach ($h in $respHeaders) {
        if ($h.name -match "(?i)location") {
            $flowLog += "Response Location: $($h.value)"
        }
        if ($h.name -match "(?i)set-cookie") {
            $flowLog += "Response Set-Cookie: $($h.value)"
        }
    }

    # Response body (truncated)
    if ($entry.response.content.text) {
        $respText = $entry.response.content.text
        if ($respText.Length -gt 2000) {
            $respText = $respText.Substring(0, 2000) + "...[truncated]"
        }
        $flowLog += "Response Body:"
        $flowLog += "  $respText"
    }

    $flowLog += ""
}

# Save flow log
$outputFile = ".\har_analysis_$(Get-Date -Format 'yyyyMMdd_HHmmss').txt"
$flowLog -join "`n" | Out-File -FilePath $outputFile -Encoding UTF8

Write-Host "Login flow saved to: $outputFile" -ForegroundColor Green
Write-Host ""

# ==============================================================================
# 3. Summary
# ==============================================================================
Write-Host "========== Summary ==========" -ForegroundColor Yellow

if ($openIdFound) {
    Write-Host "OpenID: $openIdFound" -ForegroundColor Green
    Write-Host ""
    Write-Host "You can now use this OpenID with the login script."
    Write-Host "Provide this OpenID to AI to update the script configuration."
} else {
    Write-Host "OpenID not found." -ForegroundColor Red
    Write-Host ""
    Write-Host "Possible reasons:"
    Write-Host "  1. Login was not completed (need WeChat scan)"
    Write-Host "  2. OpenID is stored in a different format"
    Write-Host "  3. The authentication flow has changed"
    Write-Host ""
    Write-Host "Check the full flow log: $outputFile"
    Write-Host "Send it to AI for further analysis."
}

Write-Host ""
Write-Host "Analysis file: $outputFile" -ForegroundColor Cyan
