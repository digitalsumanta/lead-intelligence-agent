param([int]$BatchSize = 10, [int]$Concurrency = 2, [string]$ApiUrl = "http://localhost:8000")
Invoke-RestMethod -Method Post -Uri "$ApiUrl/api/leads/process-batch" -ContentType "application/json" -Body (@{batch_size=$BatchSize;max_concurrency=$Concurrency}|ConvertTo-Json)
