-- Source of "Museum Signs.app". Rebuild with:
--   osacompile -o "Museum Signs.app" "launcher/Museum Signs.applescript"
set appPath to POSIX path of (path to me)
set folderPath to do shell script "dirname " & quoted form of appPath
set py to folderPath & "/.venv/bin/python"
try
	do shell script "test -x " & quoted form of py
on error
	display dialog "First start: Python packages will be installed (takes about a minute, needs internet)." buttons {"OK"} default button 1
	do shell script "cd " & quoted form of folderPath & " && PATH=/usr/local/bin:/opt/homebrew/bin:$PATH python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt"
end try
do shell script "cd " & quoted form of folderPath & " && nohup " & quoted form of py & " museum_signs.py > .last_run.log 2>&1 &"
