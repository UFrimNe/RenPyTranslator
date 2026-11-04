# Сторонние компоненты и сервисы

Движок ИИ, модели и unrpyc не входят в этот архив. Сведения о лицензиях компонентов находятся в их исходных репозиториях и скачанных файлах LICENSE.

- [Ren’Py](https://github.com/renpy/renpy) — формат скриптов и правила совместимости идентификаторов перевода. Проверка игры не запускает её код.
- [unrpyc](https://github.com/CensoredUsername/unrpyc) — дополнительное чтение и декомпиляция скомпилированных скриптов в памяти. Устанавливается из меню; копия и её лицензия находятся в app/vendor/unrpyc.
- [llama.cpp](https://github.com/ggml-org/llama.cpp) — движок локального ИИ. Мастер скачивает опубликованные Windows-сборки.
- GGUF-модели — скачиваются по адресам, указанным в src/rpts/local_ai.py. Условия использования определяются репозиторием выбранной модели.
- Google Cloud Translation — дополнительный сервис с собственным API-ключом пользователя.
- Google Translate — дополнительный неофициальный веб-метод. Доступность и ограничения определяются сервисом; при остановке сохраняется прогресс перевода.


## CEF Desktop 11.19

- CefSharp.WinForms.NETCore / Common.NETCore 154.0.340, BSD: https://github.com/cefsharp/CefSharp . License included in app/desktop/bin/cefsharp-LICENSE.
- Chromium Embedded Framework 154.0.34 (Windows x64): official NuGet chromiumembeddedframework.runtime and chromiumembeddedframework.runtime.win-x64 maintained by CefSharp. License included in app/desktop/bin/chromiumembeddedframework-LICENSE.txt.
- .NET 8 Windows x64 runtime and Windows Desktop runtime: https://dotnet.microsoft.com/ . Original license/third-party notices retained in runtime/dotnet.
- CPython 3.13 / 3.14 Windows x64 embeddable package: https://www.python.org/ftp/python/3.14.10/ . Original license retained in runtime/python.
- Microsoft Visual C++ v14 x64 Redistributable: official https://aka.ms/vc14/vc_redist.x64.exe . Its installer displays Microsoft license terms; no changes to installer bytes.

Runtime weights and llama.cpp are not embedded in this archive; their existing download, validation, licenses and installation flows are retained.


## Flaticon Uicons — 11.21

Uicons by Flaticon: https://www.flaticon.com/uicons . Official npm package @flaticon/flaticon-uicons 3.3.1, Regular Rounded family. Only 19 used glyphs were converted into 24px SVG outlines for embedded offline UI use. Source attribution is visible in the launcher. Full supplied license: FLATICON-LICENSE.txt; mapping/provenance: ICON_SOURCES.json. No CDN, web font or npm runtime is required. The icon artwork is supplied under the Flaticon License, separately from this application's code and other components.
