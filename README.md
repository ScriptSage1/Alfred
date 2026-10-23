# Alfred

A Spotlight-style search for Windows. Press **Ctrl+Alt+L** anywhere to open it; **Esc** hides it.

Alfred searches the folders you track — documents, PDFs, slides, spreadsheets, text files, and the
text inside images and videos — by keyword and by meaning. Everything runs on your PC.

## Install (from a clone)

1. Install Python 3.11 or newer.
2. For search by meaning and picture descriptions, install [Ollama](https://ollama.com) and run
   `ollama pull embeddinggemma-2` and `ollama pull gemma3:4b`.
   Without it, keyword search still works.
3. Double-click `Setup-Alfred.cmd`. It creates `.venv`, installs the requirements and starts Alfred.

On its first launch Alfred adds itself to your Windows Startup folder, so from then on it starts
hidden in the tray when you sign in (and starts Ollama in the background). No administrator rights
are needed. Turn this off or back on under **Settings → Start Alfred when you sign in**; once you
turn it off, Alfred leaves it off.

Start Alfred by hand any time with `Alfred.cmd` (or `python -m alfred`). Starting it again while it
runs just opens the panel.

## Tracking folders

Open **Settings** (the gear, or Ctrl+4) → **Tracked folders** → **Track folder…**, or click one of the
suggested folders (Downloads, Pictures, Documents, Desktop, Videos). A tracked folder is indexed right
away and then watched: new, changed and deleted files are picked up automatically. Click the folder
count to see each folder's status; the × stops tracking a folder (its files leave the index; nothing
on disk is touched).

For privacy Alfred won't track a whole drive, your whole user folder, Windows, Program Files or
AppData, and it skips hidden files, dot-folders, symlinks and files that look like keys or passwords.

## What it reads

| Files | How | Cited as |
|---|---|---|
| .txt .md .csv .json .yaml .html .log … | text | lines 12–40 |
| .docx | paragraphs and tables | paragraphs 3–9 |
| .pptx | each slide and its speaker notes | slide 4 |
| .xlsx | each sheet, row by row | Budget, rows 1–30 |
| .pdf | text per page; scanned pages through OCR | page 2 |
| .png .jpg .webp .bmp .gif .tiff | OCR (RapidOCR, local) | image text |
| .mp4 .mkv .mov .avi .webm … | OCR on a frame every 10 s (up to 120 frames) | at 01:23 |

Pictures (and one frame of each video) are also described by a local Ollama vision model
(**gemma3:4b**; `ollama pull gemma3:4b`): a caption, a short description and tags, shown under the
picture in the preview and labelled as an AI description. So "kid on a bench in autumn" finds the
photo. Each picture is described once (again only if it changes or you switch model); turn it off
under **Settings → Images & videos → Describe images with AI**. Photos without text stay findable by
their file name. Old `.doc/.ppt/.xls` files are
listed as unsupported; save them in the newer format to search them.

Search combines SQLite FTS5 (BM25) keyword matches with EmbeddingGemma 2 vectors through
reciprocal-rank fusion.

## Ask Alfred and chat history

Ask Alfred (Ctrl+2 or Tab) chats with a model that runs in Ollama on your PC — pick any installed
chat model from the model button at the top right (e.g. `gemma3:4b`, `llama3.2`). Answers from your
documents are the next feature; until then Ask Alfred says so.

Chats are saved as one JSON file each in `%LOCALAPPDATA%\Alfred\Chats` (change the folder, open it,
or delete all chats under **Settings → Chats**). The history panel (Ctrl+H) lists them by day; hover a
chat to rename or delete it (with Undo), and click one to reopen and continue it. Turning **Save chat
history** off stops saving new chats; nothing already saved is deleted. The chats folder is never
tracked or searched.

## Shortcut

The default is **Ctrl+Alt+L**. If another app already uses it, Alfred falls back to Ctrl+Alt+K, then
Ctrl+Alt+A, and tells you which one it picked. Change it in **Settings**. Without a free shortcut
Alfred still opens from its tray icon.

## Command line

```
python -m alfred --track "C:\Users\me\Documents\Reports"
python -m alfred --search "budget approved"
python -m alfred --status
```

These talk to the running Alfred.

## Development

```
.venv\Scripts\python -m unittest discover -s tests -v
```

`alfred/rag.py` (answers from your documents) is still a placeholder that reports "coming next". More components to start at sign-in go in `TASKS` in `alfred/startup.py`.

Settings, the index and logs live in `%LOCALAPPDATA%\Alfred` (override with `ALFRED_HOME`).
