Get-Process python -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path -like "*python*" } |
    Stop-Process -Force
Write-Host "Python processes stopped. If you have other Python applications running, restart only the adapter as needed."
