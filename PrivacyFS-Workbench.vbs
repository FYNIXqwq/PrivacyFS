Option Explicit
Dim shell, fs, root, python, quote
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
root = fs.GetParentFolderName(WScript.ScriptFullName)
python = fs.BuildPath(root, ".venv\Scripts\pythonw.exe")
quote = Chr(34)
If Not fs.FileExists(python) Then
    MsgBox "Project virtual environment missing. See WORKBENCH_GUI.md.", vbExclamation, "PrivacyFS Workbench"
    WScript.Quit 2
End If
shell.CurrentDirectory = root
shell.Run quote & python & quote & " -m privacyfs.workbench", 1, False
