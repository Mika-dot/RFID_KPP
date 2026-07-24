@echo off
rem FINAL ASCII config. No BOM.
for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"

set "SQL_DRIVER=ODBC Driver 18 for SQL Server"
set "SQL_SERVER=SRV-SQL4.MKM.LAN"
set "SQL_DATABASE=1CTgSend"
set "SQL_USER=TgSendUser"
set "SQL_PASSWORD=Shu_uc3i"
set "COMMON_DB_CONN=Driver={%SQL_DRIVER%};Server=%SQL_SERVER%;Database=%SQL_DATABASE%;Uid=%SQL_USER%;Pwd=%SQL_PASSWORD%;Encrypt=yes;TrustServerCertificate=yes;"

set "RFID_READER_IP=172.31.128.170"
set "RFID_READER_PORT=8888"

set "SRC_SERVER=srv-rg.mkm.lan"
set "SRC_DATABASE=RusGuardDB"
set "SRC_USERNAME=rgreader"
set "SRC_PASSWORD=user-1234"
set "SRC_DRIVER=ODBC Driver 18 for SQL Server"
set "SRC_TRUST_CERT=yes"

set "DST_SERVER=%SQL_SERVER%"
set "DST_DATABASE=%SQL_DATABASE%"
set "DST_USERNAME=%SQL_USER%"
set "DST_PASSWORD=%SQL_PASSWORD%"
set "DST_DRIVER=%SQL_DRIVER%"
set "DST_TRUST_CERT=yes"

set "RFID_CAMERA_USER=Akim"
set "RFID_CAMERA_PASS=MylenE12"
set "RFID_RTSP_0=rtsp://Akim:MylenE12@10.192.2.11:554/Streaming/Channels/101"
set "RFID_RTSP_1=rtsp://Akim:MylenE12@10.192.2.12:554/Streaming/Channels/101"
set "RFID_CAMERA_0_ID=0"
set "RFID_CAMERA_1_ID=1"

set "KPP_WEB_HOST=0.0.0.0"
set "KPP_WEB_PORT=5050"
set "KPP_AI_BASE_URL=http://spider-nest.mkm.lan:49572"
set "KPP_AI_FALLBACK_BASE_URL=http://172.31.0.153:49572"
set "KPP_AI_MODEL=openai/gpt-oss-20b"
