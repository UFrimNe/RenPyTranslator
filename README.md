<div align="center">

![RenPyTranslator — твои истории на русском](docs/assets/banner.svg)

# RenPyTranslator — переводчик игр Ren’Py

**Перевод визуальных новелл на русский язык с локальным ИИ.**

[![Windows build](https://github.com/UFrimNe/RenPyTranslator/actions/workflows/release.yml/badge.svg?branch=main)](https://github.com/UFrimNe/RenPyTranslator/actions/workflows/release.yml)
[![Release](https://img.shields.io/github/v/release/UFrimNe/RenPyTranslator?color=b8a0ff&label=версия)](https://github.com/UFrimNe/RenPyTranslator/releases/latest)
[![Windows](https://img.shields.io/badge/Windows-10%20%2F%2011%20x64-5865f2)](docs/USER_GUIDE.md)

[**Скачать программу**](https://github.com/UFrimNe/RenPyTranslator/releases/latest) · [Как пользоваться](docs/USER_GUIDE.md) · [Собрать самому](docs/BUILD_WINDOWS.md) · [Сообщить о проблеме](https://github.com/UFrimNe/RenPyTranslator/issues)

</div>

## Скачать и начать

| Что скачать | Для чего |
| --- | --- |
| [**Установщик EXE**](https://github.com/UFrimNe/RenPyTranslator/releases/latest/download/RenPyTranslator_11.28_Setup_win64.exe) | Установить полную программу и создать ярлык. Python и .NET уже в комплекте. |
| [**Portable ZIP**](https://github.com/UFrimNe/RenPyTranslator/releases/latest/download/RenPyTranslator_11_28_win64.zip) | Распаковать в удобную папку и открыть **RPTS.exe**. Установка не требуется. |
| [**Исходники ZIP**](https://github.com/UFrimNe/RenPyTranslator/releases/latest/download/RenPyTranslator_11_28_source.zip) | Изучить код, изменить программу или собрать свою копию через **Build.bat**. |

1. Открой программу через ярлык или `RPTS.exe`.
2. Добавь папку игры и выбери локальную модель. При первом запуске загрузятся модель и движок.
3. Начни перевод. Готовую часть можно установить и запустить игру, затем продолжить перевод позже.

**API-ключ для локального перевода не нужен.** После загрузки модели перевод работает без интернета.
Проверка и скачивание обновлений требуют подключения; ошибка проверки не мешает переводу.

> Обновление с 11.27: закрой программу и один раз распакуй новый полный ZIP в прежнюю папку с заменой файлов. Папки `data` и `runtime/local_ai` сохраняй. Старые технические теги удалены; следующие обновления доступны из нового лаунчера.

## Что умеет программа

- Карточки игр, выбранная модель, прогресс задач и библиотека готовых переводов.
- Локальный ИИ через llama.cpp: несколько моделей и настройки под их формат.
- Контекст сцены, сведения о персонажах и автоматические проверки рода, разметки и идентификаторов.
- Извлечение диалогов, телефона и интерфейса из файлов игры; захват в игре — запасной способ.
- Сборка, установка и запуск готового перевода, включая частично переведённую игру.
- Обновление из версионных GitHub Releases с проверкой SHA-256, резервной копией и откатом.
- Один диагностический ZIP с журналами, состоянием проекта и запросами ИИ.

Качество зависит от модели и исходного текста. Для проверки стиля удобно сначала перевести короткую сцену.

## Запуск и хранение

| Файл / папка | Назначение |
| --- | --- |
| `RPTS.exe` | Обычный графический запуск без консоли |
| `RPTS.bat` / `RPTS.vbs` | Альтернативный графический запуск |
| `RPTS-Console.bat` | Расширенное консольное меню |
| `RPTS-Debug.bat` | Диагностика запуска, если окно не открылось |
| `data` | Настройки, проекты, готовые переводы и диагностические ZIP |
| `runtime/local_ai` | Скачанные модели и llama.cpp |

Игры остаются в своих папках. Установщик и автообновление не включают пользовательские базы, настройки или модели в поставку.
`RPTS.exe` в portable-версии нужно оставлять рядом с `app` и `runtime`. Для распространения одним файлом предназначен установщик EXE.

## Сборка из исходников

На Windows установи Python 3.11+, .NET 8 SDK, Visual Studio Build Tools с компонентом
**Desktop development with C++** и Inno Setup 6. Затем открой **Build.bat** в корне исходников.
Готовые EXE, полный ZIP, исходники и контрольные суммы появятся в `dist`.

```bat
Build.bat
```

Без установщика: `Build.bat -NoInstaller`. Для повторной сборки с уже подготовленными средами: `Build.bat -SkipRuntime`.
[Пошаговая инструкция и требования](docs/BUILD_WINDOWS.md).

<details>
<summary><strong>Разработчикам: код, проверки и выпуск</strong></summary>

- `main` — рабочая и публичная ветка.
- `app/src/rpts` — извлечение, перевод, проверки и базы.
- `app/tools` — CLI, сервер, сборка и упаковка.
- `app/web` — интерфейс CEF; `app/desktop` — C#-контейнер.
- `app/launcher` — исходник корневого EXE; `app/installer` — исходник установщика.

```sh
python app/tools/check_source.py
```

Windows CI проверяет исходники и интерфейс, собирает все файлы и проверяет установку/удаление.
Публикация выполняется из `main` с тегом `v<версия>`. Готовый релиз повторно не перезаписывается.
Для нового релиза меняются `app/VERSION.txt` и заметки в `docs/releases`.
Сейчас используется только `main`; дополнительная ветка разработки временно убрана.

[Разработка](docs/DEVELOPMENT.md) · [Устройство обновлений](docs/UPDATES.md) · [Изменения 11.28](docs/CHANGELOG_11_28.md)

</details>

## Если возникла ошибка

В меню **Помощь и диагностика** собери ZIP и приложи его к [issue](https://github.com/UFrimNe/RenPyTranslator/issues).
Если окно не открывается — запусти `RPTS-Debug.bat`. В отчёте может быть текст игры и перевода; API-ключи скрываются.

Атрибуция иконок: [Flaticon Uicons](app/FLATICON-LICENSE.txt). Сторонние компоненты: [THIRD_PARTY.md](app/THIRD_PARTY.md).
