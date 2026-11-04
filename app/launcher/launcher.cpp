// Native bootstrap: no system Python or .NET is needed to start the portable application.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <string>
#include <vector>

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR arguments, int)
{
    std::vector<wchar_t> buffer(32768);
    const DWORD length = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
    if (!length || length >= buffer.size()) return 1;
    const std::wstring executable(buffer.data(), length);
    const std::wstring root = executable.substr(0, executable.find_last_of(L"\\/"));
    const std::wstring python = root + L"\\runtime\\python\\pythonw.exe";
    const std::wstring script = root + L"\\app\\tools\\launch_desktop.py";
    if (GetFileAttributesW(python.c_str()) == INVALID_FILE_ATTRIBUTES ||
        GetFileAttributesW(script.c_str()) == INVALID_FILE_ATTRIBUTES)
    {
        MessageBoxW(nullptr, L"Не найдены файлы программы.\n\nУстановите полную сборку или распакуйте весь portable ZIP.\nRPTS.exe нужно оставлять рядом с папками app и runtime.",
                    L"RenPyTranslator — запуск", MB_OK | MB_ICONERROR);
        return 1;
    }
    if (std::wstring(arguments) == L"--self-check") return 0;
    std::wstring command = L"\"" + python + L"\" \"" + script + L"\"";
    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION process{};
    if (!CreateProcessW(python.c_str(), command.data(), nullptr, nullptr, FALSE, CREATE_NO_WINDOW,
                        nullptr, root.c_str(), &startup, &process))
    {
        const std::wstring message = L"Не удалось запустить программу. Код Windows: " + std::to_wstring(GetLastError()) +
            L"\n\nДля диагностики запустите RPTS-Debug.bat из этой же папки.";
        MessageBoxW(nullptr, message.c_str(), L"RenPyTranslator — запуск", MB_OK | MB_ICONERROR);
        return 1;
    }
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    // Do not wait: the updater must be able to replace RPTS.exe after the CEF window closes.
    return 0;
}
