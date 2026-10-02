@echo off
rem Copy to D:\PerimeterHA\secrets.local.cmd; keep outside Git.
rem Call after existing deploy\config_v3.cmd.
set "PERIMETER_HA_TOKEN=CHANGE_TO_THE_SAME_LONG_RANDOM_TOKEN_ON_ALL_THREE_NODES"
set "PERIMETER_HA_SQL=%KPP_CONN_STR%"
rem For migrate use a separate authorized migration login locally.
