Option Explicit
Dim fs, root, exe, app
Set fs = CreateObject("Scripting.FileSystemObject")
root = fs.GetParentFolderName(WScript.ScriptFullName)
exe = fs.BuildPath(root, "wails-browser\bin\PrivacyFS-Browser.exe")
If Not fs.FileExists(exe) Then
    MsgBox "Build the Wails app first. See wails-browser\README.md.", vbExclamation, "PrivacyFS Browser"
    WScript.Quit 2
End If
Set app = CreateObject("Shell.Application")
' Elevation happens only when the user explicitly runs this launcher.
app.ShellExecute exe, "", root, "runas", 1
