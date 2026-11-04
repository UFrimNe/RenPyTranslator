Option Explicit
Dim sh, fs, root, command, python
Set sh = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
root = fs.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = root
python = root & "\runtime\python\pythonw.exe"
If Not fs.FileExists(python) Then python = root & "\runtime\python\python.exe"
command = Chr(34) & python & Chr(34) & " " & Chr(34) & root & "\app\tools\launch_desktop.py" & Chr(34)
sh.Run command, 0, False
