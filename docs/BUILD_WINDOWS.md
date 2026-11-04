# Собрать полную Windows-версию

Сборка доступна из клона репозитория и из отдельного source ZIP. Полный результат
содержит корневой RPTS.exe, CEF, Python, .NET, BAT-файлы, установщик и исходники.
Игры, модели, пользовательские настройки и базы в сборку не попадают.

## Что установить

| Компонент | Где получить |
| --- | --- |
| Windows 10/11 x64 | Сборка выполняется на Windows |
| Python 3.11+ x64 | https://www.python.org/downloads/windows/ — добавить в PATH |
| .NET 8 SDK | https://dotnet.microsoft.com/download/dotnet/8.0 — именно SDK |
| Visual Studio Build Tools | https://visualstudio.microsoft.com/visual-cpp-build-tools/ — Desktop development with C++, MSVC x64 и Windows SDK |
| Inno Setup 6 | https://jrsoftware.org/isdl.php — нужен только для Setup EXE |
| Git | Необязателен для source ZIP; нужен для клонирования и идентификатора коммита |

При первой сборке нужен интернет: NuGet, официальные Python/.NET и установщик VC Runtime.
GitHub-токен для локальной сборки не нужен.

## Запуск

Распакуй исходники и дважды нажми `Build.bat`. Либо из терминала в корне:

```bat
Build.bat
```

Команда проверит Python и SDK, запустит исходные тесты, соберёт CEF и стандартный
диалог папки, создаст native RPTS.exe со статическим CRT, загрузит переносимые
среды и упакует полный ZIP, source ZIP и установщик через Inno Setup.
Среды скачиваются во временную папку Windows; RUNNER_TEMP не требуется.

| Результат в dist | Назначение |
| --- | --- |
| RenPyTranslator_11_28_win64.zip | Полная portable-программа; запуск RPTS.exe |
| RenPyTranslator_11.28_Setup_win64.exe | Полная программа в одном установщике; ярлыки и удаление |
| RenPyTranslator_11_28_source.zip | Исходники без бинарных сред и пользовательских данных |
| update.json | Манифест автоматического обновления |
| SHA256SUMS.txt | Контрольные суммы файлов |

## Дополнительные варианты

```bat
Build.bat -NoInstaller
Build.bat -SkipRuntime
Build.bat -NoInstaller -SkipRuntime -Python C:\Python313\python.exe
```

`-SkipRuntime` сохраняет уже подготовленные среды; при отсутствии файлов упаковка
завершится ошибкой. `-NoInstaller` не требует Inno Setup. `-SkipTests` допустим для
локальной повторной сборки; опубликованные CI-сборки всегда проходят проверки.

PowerShell-вариант: `./app/tools/build-release.ps1`. Отдельная сборка CEF:
`./app/desktop/build.ps1`. Исходники EXE: `app/launcher`; Inno Setup: `app/installer/RPTS.iss`.

## Выпуск на GitHub

В Actions используется тот же build-release.ps1, что и в Build.bat. Публикация
происходит только из main, после тестов, проверки архива и тестовой установки
с удалением. Все файлы сначала загружаются в draft и сверяются по размеру/SHA-256.
Только затем релиз с тегом v<версия> становится публичным и latest.

Для следующего выпуска увеличь app/VERSION.txt, обнови версию пакета/видимую
версию и docs/releases/v<версия>.md. Не перезаписывай опубликованный релиз.
В dev сохраняется проверенный Actions artifact без дополнительных release-тегов.
