$ErrorActionPreference = "SilentlyContinue"
$patterns = @(
  "RUN_SERVICE_LOOP",
  "rfid_to_sql_v3_2.py", "rfid_to_sql_v4.py",
  "db_sync.py", "db_sync_v2.py",
  "RTSP_yolo_DB_v2.py", "RTSP_yolo_DB_v3.py",
  "kpp_1_reliable_v2.4_full_rebuild.py", "kpp_aggregator_v3.py",
  "kpp_aggregator_v3_warehouse.py", "kpp_aggregator_v3_warehouse_v3.py",
  "kpp_reel_dashboard_v2.7_ru.py", "kpp_reel_dashboard_v3_ru.py",
  "kpp_reel_dashboard_v3_fixed.py", "RUN_AGGREGATOR_V3.cmd", "RUN_WEB_V3.cmd"
)
$me = $PID
$targets = Get-CimInstance Win32_Process | Where-Object {
  $proc = $_
  if ($proc.ProcessId -eq $me -or -not $proc.CommandLine) { return $false }
  foreach ($pattern in $patterns) {
    if ($proc.CommandLine -like "*$pattern*") { return $true }
  }
  return $false
}
$targets | Sort-Object { if ($_.Name -match "cmd") { 0 } else { 1 } } | ForEach-Object {
  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
