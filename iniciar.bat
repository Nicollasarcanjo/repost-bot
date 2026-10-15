@echo off
title Repost Bot
cd /d "%~dp0"

if not exist ".env" (
    echo Arquivo .env nao encontrado. Copie .env.example para .env e preencha.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Criando ambiente Python...
    py -3 -m venv .venv || python -m venv .venv
)

echo Instalando/atualizando dependencias...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
rem yt-dlp e gallery-dl quebram quando os sites mudam; atualiza a cada inicio
".venv\Scripts\python.exe" -m pip install -q -U yt-dlp gallery-dl

:loop
".venv\Scripts\python.exe" bot.py
echo.
echo O bot parou. Reiniciando em 15 segundos... (feche a janela para encerrar)
timeout /t 15 /nobreak >nul
goto loop
