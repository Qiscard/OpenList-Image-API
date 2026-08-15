$ErrorActionPreference = "Stop"

$base = "http://127.0.0.1:8792"
$passed = 0
$failed = 0

function Post-Json($path, $body) {
    $json = $body | ConvertTo-Json -Compress -Depth 5
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $resp = Invoke-RestMethod -Uri "$base$path" -Method Post -ContentType "application/json" -Body $bytes -TimeoutSec 10
    return $resp
}

function Get-Json($path) {
    $resp = Invoke-RestMethod -Uri "$base$path" -Method Get -TimeoutSec 10
    return $resp
}

function Ok($label) {
    $script:passed++
    Write-Host "  PASS: $label"
}

function Fail($label, $detail="") {
    $script:failed++
    Write-Host "  FAIL: $label $detail"
}

Write-Host "=== Step 1: Get status ==="
try {
    $status = Get-Json "/api/status"
    Write-Host "  Index: $($status.image_count) images, dirs: $($status.directory_count)"
    Ok "status reachable"
} catch {
    Fail "get status" $_
    Write-Host "Server not reachable. Is the preview server running on port 8792?"
    exit 1
}

# Use known paths from mock index (avoid /api/images/random which needs image resolution)
$path1 = "/wallpapers/wallpapers_001.svg"
$path2 = "/abstract/abstract_002.svg"
$path3 = "/portraits/portraits_001.svg"

Write-Host ""
Write-Host "=== Step 2: Vote LIKE on $path1 ==="
try {
    $result = Post-Json "/api/tagging/vote" @{path=$path1; type="like"; value=$true}
    Write-Host "  Result: likes=$($result.likes) dislikes=$($result.dislikes)"
    if ($result.likes -ge 1) { Ok "like vote registered" }
    else { Fail "like vote" $result }
} catch {
    Fail "like vote" $_
}

Write-Host ""
Write-Host "=== Step 3: Vote LIKE again with value=false (toggle off) on $path1 ==="
try {
    $result = Post-Json "/api/tagging/vote" @{path=$path1; type="like"; value=$false}
    Write-Host "  Result: likes=$($result.likes)"
    if ($result.likes -eq 0) { Ok "like toggled off" }
    else { Fail "toggle off" $result }
} catch {
    Fail "toggle off" $_
}

Write-Host ""
Write-Host "=== Step 4: Vote LIKE (re-enable) on $path1 ==="
try {
    $result = Post-Json "/api/tagging/vote" @{path=$path1; type="like"; value=$true}
    Write-Host "  Result: likes=$($result.likes)"
    if ($result.likes -eq 1) { Ok "like re-enabled" }
    else { Fail "re-enable" $result }
} catch {
    Fail "re-enable" $_
}

Write-Host ""
Write-Host "=== Step 5: Vote DISLIKE on $path1 (should cancel like) ==="
try {
    $result = Post-Json "/api/tagging/vote" @{path=$path1; type="dislike"; value=$true}
    Write-Host "  Result: likes=$($result.likes) dislikes=$($result.dislikes)"
    if ($result.likes -eq 0 -and $result.dislikes -eq 1) { Ok "dislike cancels like" }
    else { Fail "dislike cancels like" $result }
} catch {
    Fail "dislike cancels like" $_
}

Write-Host ""
Write-Host "=== Step 6: Add category to $path2 ==="
try {
    $result = Post-Json "/api/tagging/vote" @{path=$path2; type="category"; category="wallpapers"; value=$true}
    Write-Host "  Categories: $($result.categories)"
    if ($result.categories -contains "wallpapers") { Ok "category added" }
    else { Fail "add category" $result }
} catch {
    Fail "add category" $_
}

Write-Host ""
Write-Host "=== Step 7: Add custom category to $path2 ==="
try {
    $result = Post-Json "/api/tagging/vote" @{path=$path2; type="category"; category="custom-test"; value=$true}
    Write-Host "  Categories: $($result.categories)"
    if ($result.categories -contains "custom-test") { Ok "custom category added" }
    else { Fail "add custom category" $result }
} catch {
    Fail "add custom category" $_
}

Write-Host ""
Write-Host "=== Step 8: Remove category from $path2 ==="
try {
    $result = Post-Json "/api/tagging/vote" @{path=$path2; type="category"; category="wallpapers"; value=$false}
    Write-Host "  Categories: $($result.categories)"
    if ($result.categories -notcontains "wallpapers") { Ok "category removed" }
    else { Fail "remove category" $result }
} catch {
    Fail "remove category" $_
}

Write-Host ""
Write-Host "=== Step 9: Get tag stats ==="
try {
    $encoded = [uri]::EscapeDataString("$path1,$path2")
    $result = Get-Json "/api/tagging/stats?paths=$encoded"
    Write-Host "  Stats: $($result | ConvertTo-Json -Compress)"
    Ok "stats returned"
} catch {
    Fail "get stats" $_
}

Write-Host ""
Write-Host "=== Step 10: Get all categories ==="
try {
    $result = Get-Json "/api/tagging/categories"
    Write-Host "  Categories: $($result.categories)"
    Ok "categories returned"
} catch {
    Fail "get categories" $_
}

Write-Host ""
Write-Host "=== Step 11: Invalid vote type (should 400) ==="
try {
    $result = Post-Json "/api/tagging/vote" @{path=$path1; type="invalid"; value=$true}
    Fail "invalid vote type should fail" $result
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    if ($code -eq 400) { Ok "invalid vote type rejected" }
    else { Fail "invalid vote type" "code=$code" }
}

Write-Host ""
Write-Host "=== Step 12: Missing path (should 400) ==="
try {
    $result = Post-Json "/api/tagging/vote" @{type="like"; value=$true}
    Fail "missing path should fail" $result
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    if ($code -eq 400) { Ok "missing path rejected" }
    else { Fail "missing path" "code=$code" }
}

Write-Host ""
Write-Host "=== Step 13: Non-indexed path (should 400) ==="
try {
    $result = Post-Json "/api/tagging/vote" @{path="/nonexistent/image.svg"; type="like"; value=$true}
    Fail "non-indexed path should fail" $result
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    if ($code -eq 400) { Ok "non-indexed path rejected" }
    else { Fail "non-indexed path" "code=$code" }
}

Write-Host ""
Write-Host ("=" * 50)
Write-Host "RESULTS: $passed passed, $failed failed"
if ($failed -eq 0) {
    Write-Host "ALL TESTS PASSED"
} else {
    Write-Host "SOME TESTS FAILED"
    exit 1
}
