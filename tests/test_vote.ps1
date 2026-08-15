Add-Type -AssemblyName System.Net.Http
$client = New-Object System.Net.Http.HttpClient
$client.Timeout = [TimeSpan]::FromSeconds(15)

$body = '{"path":"/abstract/abstract_002.svg","type":"like","value":true}'
$content = New-Object System.Net.Http.StringContent($body, [System.Text.Encoding]::UTF8, 'application/json')

try {
    $response = $client.PostAsync('http://127.0.0.1:8792/api/tagging/vote', $content).Result
    $responseText = $response.Content.ReadAsStringAsync().Result
    Write-Host "Status:" $response.StatusCode
    Write-Host "Response:" $responseText
} catch {
    Write-Host "ERROR:" $_.Exception.InnerException.Message
}
