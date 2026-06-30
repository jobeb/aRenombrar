Dim python, projectDir, shell
Set shell = CreateObject("WScript.Shell")

python = "C:\Users\Jose\AppData\Local\Programs\Python\Python313\python.exe"
projectDir = "D:\Proyectos\aRenombrar\aRenombrar"

' Instalar dependencias si faltan
shell.Run """" & python & """ -m pip install customtkinter Pillow requests --quiet", 0, True

' Lanzar la aplicacion
shell.CurrentDirectory = projectDir
shell.Run """" & python & """ """ & projectDir & "\main.py""", 0, False
