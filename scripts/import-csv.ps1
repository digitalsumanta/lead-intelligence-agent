param([string]$Path = "data/sample_leads.csv", [string]$ApiUrl = "http://localhost:8000")
curl.exe -f -X POST "$ApiUrl/api/leads/import" -F "file=@$Path"
