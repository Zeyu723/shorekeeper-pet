@echo off
set "PET_DIR=C:\Users\zeyu\AppData\Local\hermes\shorekeeper-pet"
set "PET_PY=C:\Users\zeyu\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"

if not exist "%PET_PY%" set "PET_PY=py -3"

cd /d "%PET_DIR%"
"%PET_PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name "守岸人桌宠" ^
  --add-data "assets\shorekeeper-laying.png;assets" ^
  --add-data "actions;actions" ^
  app.py
if errorlevel 1 pause & exit /b 1
echo.
echo Built: dist\守岸人桌宠.exe
pause
