# Сборка CEF-окна

Исходник окна: `app/desktop/src/Program.cs`. HTML-интерфейс: `app/web`. API: `app/src/rpts/desktop.py`; исполнитель фоновых задач: `app/tools/desktop_worker.py`.

На Windows с .NET 8 SDK запусти PowerShell: `app/desktop/build.ps1`. Скрипт выполняет dotnet publish для win-x64 и восстанавливает CefSharp 154.0.340 из NuGet. Программа использует саму себя как дочерний процесс CEF; отдельный subprocess EXE не требуется.

Обычный запуск выполняется через RPTS.bat/RPTS.vbs: bootstrap указывает путь к переносимому .NET runtime и запускает CEF, который запускает Python API. Прямой запуск вложенного RPTS.Desktop.exe без DOTNET_ROOT может потребовать установленного системного .NET.

Сервис слушает случайный порт только 127.0.0.1. Сессионный ключ передаётся окну через stdin/stdout-процесс запуска и URL fragment, затем удаляется из адреса. Запросы API требуют bearer token; Origin/Host проверяются. Произвольные shell-команды из HTML не исполняются. Внешняя навигация и всплывающие окна запрещены.

Офлайн-тесты: `runtime/python/python.exe app/tests/test_desktop.py`. Они работают на временных проектах и не запускают загрузку модели. Для браузерных тестов разработчику отдельно нужен Playwright и Chromium; комплект пользователя их не содержит.
