@echo off
REM Inicia el Sistema de Gestion de Mantenimiento de Lift Rental y abre el navegador.
cd /d "%~dp0"
python -c "import flask, pulp" 2>nul || python -m pip install -r requirements.txt
start "" http://127.0.0.1:5000
python app.py
pause
