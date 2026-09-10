@echo off
cd /d "%~dp0"

:: 启动后端
start "Backend" cmd /k ".venv\Scripts\python -m backend.main"

:: 启动前端
start "Frontend" cmd /k "npm run dev"

:: 等待前端启动
timeout /t 3 /nobreak >nul

:: 打开浏览器
start "" "http://localhost:5173"

