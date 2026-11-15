Alfred - test build for Windows 10/11 (64-bit)
================================================

Thank you for trying Alfred. It is a search for your own files: press Ctrl+Alt+L anywhere to open it, Esc hides it.
Everything runs on your PC. You do not need Python or anything else installed.

Start it
1. Unzip the whole folder anywhere you like, for example into Documents or onto the Desktop
   (keep Alfred.exe next to its _internal folder; do not move the exe out on its own).
2. Double-click Alfred.exe.
3. Windows may say "Windows protected your PC" because this test build is not signed.
   Click "More info", then "Run anyway". This is asked once.
4. Alfred asks which folders to track. Keyword search works straight away. Search by meaning needs the search
   engine and model (about 340 MB together); Alfred offers to download them - that is your choice.

Good to know
- Alfred adds itself to start when you sign in (hidden in the tray). Switch that off in
  Settings -> "Start Alfred when you sign in".
- Your settings and the index live in %LOCALAPPDATA%\Alfred (paste that into the Explorer address bar).
  Alfred never changes the files it searches.
- API keys you enter for cloud answers are kept in Windows Credential Manager under "Alfred".

Sending feedback
Tell us what you tried, what you expected and what happened, and attach the log file:
  %LOCALAPPDATA%\Alfred\assistant.log
(The log lists file names Alfred indexed or failed to read; look through it before you send it if that matters to you.)

Removing Alfred
1. Settings -> switch off "Start Alfred when you sign in", then quit Alfred from its tray icon (right-click -> Quit Alfred).
2. Delete the Alfred folder you unzipped.
3. To remove your settings, index and downloaded models too, delete %LOCALAPPDATA%\Alfred.
4. If you skipped step 1, also delete Alfred.lnk from
   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
