Option Explicit
Dim fs, root, python, app, quote
Set fs = CreateObject("Scripting.FileSystemObject")
root = fs.GetParentFolderName(WScript.ScriptFullName)
python = fs.BuildPath(root, ".venv\Scripts\pythonw.exe")
quote = Chr(34)
If Not fs.FileExists(python) Then
    MsgBox "Project virtual environment missing. See WORKBENCH_GUI.md.", vbExclamation, "PrivacyFS Workbench"
    WScript.Quit 2
End If
Set app = CreateObject("Shell.Application")
' UAC is requested only when the user explicitly runs this launcher.
app.ShellExecute python, "-m privacyfs.workbench", root, "runas", 1
