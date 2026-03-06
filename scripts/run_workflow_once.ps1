$ErrorActionPreference = "Stop"

Write-Host "Triggering workflow..."
$response = Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/workflow/trigger"
$response | ConvertTo-Json -Depth 5

Write-Host "Latest runs:"
Invoke-RestMethod -Method Get -Uri "http://localhost:8000/api/workflow/runs?limit=5" | ConvertTo-Json -Depth 5

Write-Host "Poll publish commands:"
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/publications/poll-mail" | ConvertTo-Json -Depth 5
