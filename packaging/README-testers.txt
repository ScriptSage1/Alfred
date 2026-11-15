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
- Alfred starts when you sign in (hidden in the tray) unless you switch that off in the last setup step, or later in
  Settings -> "Start Alfred when you sign in".
- Your settings and the index live in %LOCALAPPDATA%\Alfred (paste that into the Explorer address bar).
  Alfred never changes the files it searches.
- API keys you enter for cloud answers are kept in Windows Credential Manager under "Alfred".

Sending feedback
Tell us what you tried, what you expected and what happened, and attach the log file:
  %LOCALAPPDATA%\Alfred\assistant.log
(The log lists file names Alfred indexed or failed to read; look through it before you send it if that matters to you.)

Using Alfred from Claude Code and other AI tools (optional)
Alfred can let an AI tool on this PC search your tracked folders and read the matching text, so you can ask it
about your own files. It works with tools that support MCP servers (Claude Code, Codex, Claude Desktop and others).
1. In Alfred, open Settings -> AI tools. It shows one command with a Copy button, like
     claude mcp add alfred -- "C:\path\to\Alfred\Alfred.exe" --mcp
2. Paste it into a terminal and run it (that line is for Claude Code). For another tool, add an MCP server in its
   settings with the program  C:\path\to\Alfred\Alfred.exe  and the argument  --mcp
3. The first time a tool asks, Windows shows "... wants to search your files through Alfred. Allow?" - nothing is
   shared until you click Yes. Settings -> AI tools lists the tools you allowed and what each one asked for.
The tool only gets the passages it asks for; Alfred itself sends nothing anywhere. To disconnect a tool, remove it in
Settings -> AI tools, or run:  claude mcp remove alfred

Removing Alfred
Double-click Uninstall-Alfred.cmd in this folder. It stops Alfred, removes its start-at-sign-in and Start menu
shortcuts, and then asks, one at a time, whether to also delete your settings, index and downloaded models
(%LOCALAPPDATA%\Alfred), the API keys saved in Windows Credential Manager, and this folder.
The files and folders you searched with Alfred are never touched.
By hand instead: quit Alfred from its tray icon (right-click -> Quit Alfred), delete this folder, delete
%LOCALAPPDATA%\Alfred, and delete Alfred.lnk from %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
