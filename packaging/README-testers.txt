Juno - test build for Windows 10/11 (64-bit)
================================================

Thank you for trying Juno. It is a search for your own files: press Ctrl+Alt+L anywhere to open it, Esc hides it.
Everything runs on your PC. You do not need Python or anything else installed.

Start it
1. Unzip the whole folder anywhere you like, for example into Documents or onto the Desktop
   (keep Juno.exe next to its _internal folder; do not move the exe out on its own).
2. Double-click Juno.exe.
3. Windows may say "Windows protected your PC" because this test build is not signed.
   Click "More info", then "Run anyway". This is asked once.
4. Juno asks which folders to track. Search by name, by text and by meaning all work straight away: the search
   engine (llama.cpp) and the search model (EmbeddingGemma 2) come in this folder, so nothing is downloaded.
   Finding pictures by what they show needs one more file (about 1 GB), offered in Settings -> Advanced -> Model files.

Good to know
- Juno starts when you sign in (hidden in the tray) unless you switch that off in the last setup step, or later in
  Settings -> "Start Juno when you sign in".
- Your settings and the index live in %LOCALAPPDATA%\Juno (paste that into the Explorer address bar).
  Juno never changes the files it searches.
- API keys you enter for cloud answers are kept in Windows Credential Manager under "Juno".

Sending feedback
Tell us what you tried, what you expected and what happened, and attach the log file:
  %LOCALAPPDATA%\Juno\assistant.log
(The log lists file names Juno indexed or failed to read; look through it before you send it if that matters to you.)

Using Juno from Claude Code and other AI tools (optional)
Juno can let an AI tool on this PC search your tracked folders and read the matching text, so you can ask it
about your own files. It works with tools that support MCP servers (Claude Code, Codex, Claude Desktop and others).
1. In Juno, open Settings -> AI tools. Each AI tool found on this PC (Claude Code, Codex, Claude Desktop, Cursor,
   VS Code, Windsurf, Gemini CLI) has a Connect button. Click it, then restart that tool if it is open.
2. For any other tool, click Copy next to "Any other tool" and paste the entry into its MCP settings (the program
   C:\path\to\Juno\Juno.exe  with the argument  --mcp).
3. The first time a tool asks, Windows shows "... wants to search your files through Juno. Allow?" - nothing is
   shared until you click Yes. Settings -> AI tools lists the tools you allowed and what each one asked for.
The tool only gets the passages it asks for; Juno itself sends nothing anywhere. To disconnect a tool, remove it in
Settings -> AI tools, or run:  claude mcp remove juno

Removing Juno
Double-click Uninstall-Juno.cmd in this folder. It stops Juno, removes its start-at-sign-in and Start menu
shortcuts, and then asks, one at a time, whether to also delete your settings, index and downloaded models
(%LOCALAPPDATA%\Juno), the API keys saved in Windows Credential Manager, and this folder.
The files and folders you searched with Juno are never touched.
By hand instead: quit Juno from its tray icon (right-click -> Quit Juno), delete this folder, delete
%LOCALAPPDATA%\Juno, and delete Juno.lnk from %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
