@echo off
rem ============================================================
rem  Запуск Flask-сервера Synapse локально (Windows).
rem  Самодостаточный: при первом запуске сам создаёт .venv и
rem  ставит зависимости из requirements.txt, потом просто
rem  запускает сервер. Достаточно "скачал репозиторий ->
rem  двойной клик". Нужен только установленный Python и
rem  (на первый запуск) интернет.
rem ============================================================
setlocal
cd /d "%~dp0"

rem --- Найти Python (сначала лаунчер py, потом python) ---
set "PY=py"
where py >nul 2>nul || set "PY=python"
%PY% --version >nul 2>nul
if errorlevel 1 (
    echo [ОШИБКА] Python не найден.
    echo Установите Python 3 с https://python.org и при установке
    echo поставьте галочку "Add Python to PATH", затем запустите снова.
    pause
    exit /b 1
)

rem --- Создать .venv, если его ещё нет ---
if not exist ".venv\Scripts\python.exe" (
    echo Создаю виртуальное окружение .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ОШИБКА] Не удалось создать .venv
        pause
        exit /b 1
    )
)

rem --- Поставить зависимости, если Flask ещё не установлен ---
".venv\Scripts\python.exe" -c "import flask" >nul 2>nul
if errorlevel 1 (
    echo Устанавливаю зависимости из requirements.txt ...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ОШИБКА] Не удалось установить зависимости.
        echo Проверьте интернет-соединение и запустите снова.
        pause
        exit /b 1
    )
)

rem --- Подгрузить локальные секреты (Telegram api_id/api_hash и т.п.) ---
rem  Файл tg_credentials.bat не входит в репозиторий. Если его нет —
rem  Telegram-мост просто молчит, остальное приложение работает как обычно.
if exist "tg_credentials.bat" (
    call "tg_credentials.bat"
    echo Telegram-мост: креды загружены из tg_credentials.bat
) else (
    echo Telegram-мост: tg_credentials.bat не найден, мост отключён
)

rem --- Запуск сервера ---
echo.
echo Сервер Synapse запускается на http://localhost:5000
echo Закройте это окно, чтобы остановить сервер.
echo.
".venv\Scripts\python.exe" main.py
pause
