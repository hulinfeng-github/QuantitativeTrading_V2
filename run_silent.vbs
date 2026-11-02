Set ws = CreateObject("WScript.Shell")
' 0 表示无黑框静默在后台常驻运行，支持开机自启
ws.Run "run_background.bat", 0, False
