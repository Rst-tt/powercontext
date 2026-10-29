@echo off
REM Start the API server against the WSL seekdb instance (port 12897 by default).
set SEEPDB_HOST=127.0.0.1
if "%SEEPDB_PORT%"=="" set SEEPDB_PORT=12897
if "%SEEPDB_DATABASE%"=="" set SEEPDB_DATABASE=test
if "%API_PORT%"=="" set API_PORT=8787
node server/index.mjs
