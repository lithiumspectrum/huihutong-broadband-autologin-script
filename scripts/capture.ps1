<#
.SYNOPSIS
    Dorm Network Authentication Capture Script
.DESCRIPTION
    Run this script while connected to the dorm network to capture all data
    needed to verify the Huihutong auto-login script.
    Results are saved to capture_<timestamp> folder.
.NOTES
    Environment: Windows 10/11 (requires curl.exe)
    Usage:
      1. Connect to dorm WiFi (do NOT authenticate, stay logged out)
      2. Open PowerShell
      3. Run: powershell -ExecutionPolicy Bypass -File capture.ps1
      4. If you have OpenID: powershell -ExecutionPolicy Bypass -File capture.ps1 -OpenId "your_open_id"
#>

param(
    [string]$OpenId = "",
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Continue"

# ==============================================================================
# Init
# ==============================================================================

if (-not $OutputDir) {
    $OutputDir = ".\capture_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$OutputDir = (Resolve-Path $OutputDir).Path

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $ts = Get-Date -Format "HH:mm:ss"
    $line = "[$ts] [$Level] $Message"
    Write-Host $line
    Add-Content -Path "$OutputDir\capture_log.txt" -Value $line
}

function Save-Output {
    param([string]$Name, [string]$Content)
    $file = "$OutputDir\$Name"
    $Content | Out-File -FilePath $file -Encoding UTF8
    Write-Log "Saved: $Name"
}

function Invoke-Curl {
    param(
        [string]$Url,
        [int]$Timeout = 10,
        [switch]$HeadersOnly,
        [switch]$FollowRedirects,
        [string]$ExtraArgs = ""
    )
    $curlArgs = @("-s", "--max-time", $Timeout, "-w", "`n---HTTP_CODE:%{http_code}---")
    if ($HeadersOnly) { $curlArgs += @("-I") }
    if (-not $FollowRedirects) { $curlArgs += @("-L", "--max-redirs", "0") }
    if ($ExtraArgs) { $curlArgs += $ExtraArgs }
    $curlArgs += $Url
    $result = & curl.exe @curlArgs 2>&1
    return ($result -join "`n")
}

Write-Log "========================================"
Write-Log "  Dorm Network Authentication Capture"
Write-Log "  Output dir: $OutputDir"
Write-Log "  Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Log "========================================"
Write-Host ""

# ==============================================================================
# 1. Network basic info
# ==============================================================================
Write-Log "Step 1/7: Capture network basic info"

$ipconfig = ipconfig /all
Save-Output "01_ipconfig.txt" $ipconfig

$routePrint = route print
Save-Output "02_route_print.txt" $routePrint

try {
    $arpTable = arp -a
    Save-Output "03_arp_table.txt" ($arpTable -join "`n")
} catch {
    Write-Log "ARP table fetch failed" "WARN"
}

# Detect gateway IP
$gateway = (ipconfig | Select-String "Default Gateway" | Select-Object -First 1)
$gatewayIp = ""
if ($gateway -match "(\d+\.\d+\.\d+\.\d+)") {
    $gatewayIp = $Matches[1]
    Write-Log "Gateway: $gatewayIp"
}

# Detect local IP
$localIpLine = (ipconfig | Select-String "IPv4" | Select-Object -First 1)
if ($localIpLine -match "(\d+\.\d+\.\d+\.\d+)") {
    $localIp = $Matches[1]
    Write-Log "Local IP: $localIp"
}

Write-Host ""

# ==============================================================================
# 2. DNS resolution test
# ==============================================================================
Write-Log "Step 2/7: DNS resolution test"

$dnsTargets = @(
    "api.215123.cn",
    "connect.rom.miui.com",
    "www.baidu.com",
    "10.10.16.101"
)

$dnsResults = @()
foreach ($target in $dnsTargets) {
    Write-Log "  Resolving: $target"
    $nsResult = nslookup $target 2>&1
    $dnsResults += "=== $target ==="
    $dnsResults += ($nsResult -join "`n")
    $dnsResults += ""
}

Save-Output "04_dns_resolution.txt" ($dnsResults -join "`n")

# Also test with PowerShell Resolve-DnsName
$psDnsResults = @()
foreach ($target in $dnsTargets) {
    try {
        $resolved = Resolve-DnsName $target -ErrorAction Stop | Format-List | Out-String
        $psDnsResults += "=== $target (PowerShell) ===`n$resolved"
    } catch {
        $psDnsResults += "=== $target (PowerShell) ===`nFailed: $_"
    }
}
Save-Output "04b_dns_powershell.txt" ($psDnsResults -join "`n")

Write-Host ""

# ==============================================================================
# 3. Captive Portal detection
# ==============================================================================
Write-Log "Step 3/7: Captive Portal detection"

$portalCheckUrl = "http://connect.rom.miui.com/generate_204"
Write-Log "  Check URL: $portalCheckUrl"

# No redirect follow
$portalResp = Invoke-Curl -Url $portalCheckUrl -Timeout 10
Save-Output "05_portal_check_no_redirect.txt" $portalResp

# Parse status code
$statusCode = ""
if ($portalResp -match "HTTP_CODE:(\d+)") {
    $statusCode = $Matches[1]
    Write-Log "  HTTP status: $statusCode"
}

# Parse Location header
$redirectUrl = ""
if ($portalResp -match "(?i)location:\s*(.+)") {
    $redirectUrl = $Matches[1].Trim()
    Write-Log "  Redirect URL: $redirectUrl"
} elseif ($statusCode -eq "200") {
    # 200 response, maybe HTML contains redirect
    $htmlMatch = [regex]::Match($portalResp, "location\.href='([^']+)'")
    if ($htmlMatch.Success) {
        $redirectUrl = $htmlMatch.Groups[1].Value
        Write-Log "  HTML redirect URL: $redirectUrl"
    }
}

# Also test other common captive portal check URLs
$otherCheckUrls = @(
    "http://neverssl.com",
    "http://detectportal.firefox.com/canonical.html",
    "http://www.msftconnecttest.com/connecttest.txt"
)
$otherResults = @()
foreach ($url in $otherCheckUrls) {
    Write-Log "  Check alt URL: $url"
    $resp = Invoke-Curl -Url $url -Timeout 10
    $otherResults += "=== $url ===`n$resp`n"
}
Save-Output "05b_portal_check_other.txt" ($otherResults -join "`n")

Write-Host ""

# ==============================================================================
# 4. Redirect chain capture
# ==============================================================================
Write-Log "Step 4/7: Redirect chain capture"

$redirectChain = @()
$currentUrl = $redirectUrl
$maxRedirects = 10
$redirectCount = 0

while ($currentUrl -and $redirectCount -lt $maxRedirects) {
    $redirectCount++
    Write-Log "  Following redirect #$redirectCount : $currentUrl"

    $resp = Invoke-Curl -Url $currentUrl -Timeout 10
    $redirectChain += "=== Redirect #$redirectCount ==="
    $redirectChain += "URL: $currentUrl"
    $redirectChain += $resp
    $redirectChain += ""

    # Try to extract next redirect from response
    $nextRedirect = ""
    $locMatch = [regex]::Match($resp, "(?i)location:\s*(.+)")
    if ($locMatch.Success) {
        $nextRedirect = $locMatch.Groups[1].Value.Trim()
        # Handle relative URLs
        if ($nextRedirect -match "^/web-app/" -or $nextRedirect -match "^/ac/") {
            $nextRedirect = "https://api.215123.cn$nextRedirect"
        }
    } else {
        $hrefMatch = [regex]::Match($resp, "location\.href='([^']+)'")
        if ($hrefMatch.Success) {
            $nextRedirect = $hrefMatch.Groups[1].Value
        }
    }

    if ($nextRedirect -and $nextRedirect -ne $currentUrl) {
        $currentUrl = $nextRedirect
    } else {
        # Also try to get full HTML content
        $htmlResp = & curl.exe -s --max-time 10 $currentUrl 2>&1
        $redirectChain += "=== Full HTML content ==="
        $redirectChain += ($htmlResp -join "`n")
        break
    }
}

Save-Output "06_redirect_chain.txt" ($redirectChain -join "`n")

Write-Host ""

# ==============================================================================
# 5. Portal login page HTML capture
# ==============================================================================
Write-Log "Step 5/7: Portal login page HTML capture"

# Try to access gateway directly
if ($gatewayIp) {
    Write-Log "  Try gateway: http://$gatewayIp"
    $gatewayResp = & curl.exe -s --max-time 10 -L "http://$gatewayIp" 2>&1
    Save-Output "07_gateway_page.html" ($gatewayResp -join "`n")
}

# Try to access 10.10.16.101:8080 (portal address from script)
Write-Log "  Try portal: http://10.10.16.101:8080"
$portalPageResp = & curl.exe -s --max-time 10 -L "http://10.10.16.101:8080" 2>&1
Save-Output "08_portal_page.html" ($portalPageResp -join "`n")

# If redirect URL exists, get its full HTML
if ($redirectUrl) {
    Write-Log "  Get full page from redirect target: $redirectUrl"
    $portalHtml = & curl.exe -s --max-time 10 -L $redirectUrl 2>&1
    Save-Output "09_redirect_page.html" ($portalHtml -join "`n")
}

Write-Host ""

# ==============================================================================
# 6. API endpoint test
# ==============================================================================
Write-Log "Step 6/7: Huihutong API endpoint test"

# Test certificateLogin endpoint with dummy OpenID
$apiUrl = "https://api.215123.cn/web-app/auth/certificateLogin?openId=test_dummy_id"
Write-Log "  Test API: $apiUrl"
$apiResp = Invoke-Curl -Url $apiUrl -Timeout 15
Save-Output "10_api_test.txt" $apiResp

# Also test without params
$apiBaseUrl = "https://api.215123.cn/web-app/auth/certificateLogin"
Write-Log "  Test API (no params): $apiBaseUrl"
$apiBaseResp = Invoke-Curl -Url $apiBaseUrl -Timeout 15
Save-Output "10b_api_base_test.txt" $apiBaseResp

# Test OAuth endpoint
$oauthUrl = "https://api.215123.cn/ac/auth/oauthRedirect?serviceName=chinaTelecom"
Write-Log "  Test OAuth: $oauthUrl"
$oauthResp = Invoke-Curl -Url $oauthUrl -Timeout 15
Save-Output "11_oauth_test.txt" $oauthResp

Write-Host ""

# ==============================================================================
# 7. Full login flow test (if OpenID provided)
# ==============================================================================
Write-Log "Step 7/7: Full login flow test"

if ($OpenId) {
    Write-Log "  Testing full login flow with OpenID: $OpenId"

    # Step 7a: Get SA Token
    $tokenUrl = "https://api.215123.cn/web-app/auth/certificateLogin?openId=$OpenId"
    Write-Log "  7a: Get SA Token"
    $tokenResp = Invoke-Curl -Url $tokenUrl -Timeout 15
    Save-Output "12a_get_sa_token.txt" $tokenResp

    # Parse token
    $saToken = ""
    $tokenMatch = [regex]::Match($tokenResp, '"token"\s*:\s*"([^"]+)"')
    if ($tokenMatch.Success) {
        $saToken = $tokenMatch.Groups[1].Value
        Write-Log "  Got SA Token: $saToken"
    } else {
        Write-Log "  Failed to parse SA Token from response" "WARN"
    }

    # Step 7b: Check network status
    Write-Log "  7b: Check current network status"
    $checkResp = Invoke-Curl -Url $portalCheckUrl -Timeout 10
    Save-Output "12b_check_status.txt" $checkResp

    # Parse redirect URL
    $loginRedirectUrl = ""
    $checkLocMatch = [regex]::Match($checkResp, "(?i)location:\s*(.+)")
    if ($checkLocMatch.Success) {
        $loginRedirectUrl = $checkLocMatch.Groups[1].Value.Trim()
    } else {
        $checkHrefMatch = [regex]::Match($checkResp, "location\.href='([^']+)'")
        if ($checkHrefMatch.Success) {
            $loginRedirectUrl = $checkHrefMatch.Groups[1].Value
        }
    }

    if ($loginRedirectUrl -and $saToken) {
        # Step 7c: Get OAuth redirect
        Write-Log "  7c: Get OAuth redirect: $loginRedirectUrl"
        $oauthRedirResp = & curl.exe -s --max-time 10 -I $loginRedirectUrl 2>&1
        Save-Output "12c_oauth_redirect.txt" ($oauthRedirResp -join "`n")

        # Parse OAuth Location
        $oauthLocation = ""
        $oauthLocMatch = [regex]::Match(($oauthRedirResp -join "`n"), "(?i)location:\s*(.+)")
        if ($oauthLocMatch.Success) {
            $oauthLocation = $oauthLocMatch.Groups[1].Value.Trim()
        }

        if ($oauthLocation) {
            # Build pre-login URL
            $queryString = ""
            $qsMatch = [regex]::Match($oauthLocation, "\?(.+)")
            if ($qsMatch.Success) {
                $queryString = $qsMatch.Groups[1].Value
            }
            $preLoginUrl = "https://api.215123.cn/ac/auth/oauthRedirect?$queryString&serviceName=chinaTelecom"
            Write-Log "  7d: Execute login: $preLoginUrl"
            $preLoginResp = & curl.exe -s --max-time 15 -H "satoken: $saToken" $preLoginUrl 2>&1
            Save-Output "12d_pre_login.txt" ($preLoginResp -join "`n")

            # Parse final login URL
            $finalLoginUrl = ""
            $dataMatch = [regex]::Match(($preLoginResp -join "`n"), '"data"\s*:\s*"([^"]+)"')
            if ($dataMatch.Success) {
                $finalLoginUrl = $dataMatch.Groups[1].Value
            }

            if ($finalLoginUrl) {
                Write-Log "  7e: Final login URL: $finalLoginUrl"
                $finalResp = & curl.exe -s --max-time 15 -I $finalLoginUrl 2>&1
                Save-Output "12e_final_login.txt" ($finalResp -join "`n")
            } else {
                Write-Log "  7e: No final login URL found in response" "WARN"
            }
        } else {
            Write-Log "  7c: No OAuth Location found" "WARN"
        }
    } else {
        if (-not $loginRedirectUrl) {
            Write-Log "  No redirect detected, may already be online" "INFO"
        }
        if (-not $saToken) {
            Write-Log "  No SA Token obtained, API may have changed" "WARN"
        }
    }

    # Step 7f: Verify network status after login
    Start-Sleep -Seconds 2
    Write-Log "  7f: Verify network status after login"
    $afterLoginResp = Invoke-Curl -Url $portalCheckUrl -Timeout 10
    Save-Output "12f_after_login_check.txt" $afterLoginResp

} else {
    Write-Log "  No OpenID provided, skipping full login flow test"
    Write-Log "  Please follow these steps to get OpenID:"
    Write-Host ""
    Write-Host "  ==============================================="
    Write-Host "  How to get OpenID (browser capture):"
    Write-Host "  ==============================================="
    Write-Host "  1. Open browser, press F12 for DevTools"
    Write-Host "  2. Switch to Network tab"
    Write-Host "  3. Check 'Preserve log'"
    Write-Host "  4. Navigate to any HTTP site (e.g. http://baidu.com)"
    Write-Host "  5. Page should redirect to Huihutong portal"
    Write-Host "  6. Scan QR code with WeChat to authenticate"
    Write-Host "  7. In Network panel, search for 'openId' or 'openid'"
    Write-Host "  8. Find the request containing openId parameter"
    Write-Host "  9. Copy the openId value"
    Write-Host "  10. Re-run: .\capture.ps1 -OpenId 'your_openId'"
    Write-Host "  ==============================================="
    Write-Host ""

    $instructions = @"
How to get OpenID (browser capture):

1. Open browser (Chrome/Edge), press F12 for DevTools
2. Switch to Network tab
3. Check 'Preserve log' option
4. Type http://baidu.com in address bar and press Enter
5. Page should auto-redirect to Huihutong auth portal
6. Scan QR code on portal page with WeChat to authenticate
7. Go back to DevTools Network panel
8. Type 'openId' or 'openid' in search/filter box
9. Find the request containing openId parameter
   - May be in URL params: ?openId=xxxxxxxx
   - Or in request body / response body
10. Copy the openId value
11. Re-run this script with OpenID:
    powershell -ExecutionPolicy Bypass -File capture.ps1 -OpenId "your_openId"

Notes:
- OpenID is usually an alphanumeric string
- It is bound to your WeChat account
- Each OpenID corresponds to one user
- If OpenID expires, need to re-capture
"@
    Save-Output "12_openid_instructions.txt" $instructions
}

Write-Host ""

# ==============================================================================
# Summary
# ==============================================================================
Write-Log "========================================"
Write-Log "  Capture complete!"
Write-Log "========================================"
Write-Log "Output dir: $OutputDir"
Write-Log ""
Write-Log "File list:"
$files = Get-ChildItem $OutputDir -File | Sort-Object Name
foreach ($f in $files) {
    $size = "{0:N1}" -f ($f.Length / 1KB)
    Write-Log "  $($f.Name)  (${size} KB)"
}
Write-Log ""
Write-Log "Next steps:"
Write-Log "  1. Switch back to phone hotspot"
Write-Log "  2. Provide contents of $OutputDir folder to AI for analysis"
Write-Log "  3. If you got OpenID, provide it to AI as well"
Write-Log ""

Write-Host ""
Write-Host "Capture complete! All data saved to: $OutputDir" -ForegroundColor Green
Write-Host "Please switch back to phone hotspot and provide the folder contents to AI." -ForegroundColor Yellow
