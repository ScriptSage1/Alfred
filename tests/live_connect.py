"""End-to-end check of Alfred's AI-tool connect, on a fresh, isolated profile (no real tool settings touched).

Real Claude Code and Codex CLIs, pointed at empty config folders (CLAUDE_CONFIG_DIR, CODEX_HOME); JSON-config tools in
a fake user profile (USERPROFILE, APPDATA, LOCALAPPDATA). Each connect also starts the server as the tool would.
Run before a release: .venv/Scripts/python tests/live_connect.py   (needs Claude Code and Codex installed;
not part of the unit tests, which never start the real CLIs)
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

repo = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, repo)
root = Path(tempfile.mkdtemp(prefix='alfred e2e é '))  # a space and a non-ASCII letter, like many user names
profile, roaming, local = root / 'User', root / 'User' / 'AppData' / 'Roaming', root / 'User' / 'AppData' / 'Local'
for folder in (roaming, local):
    folder.mkdir(parents=True)
# find the real CLIs before the profile is swapped (they are found under the real user folders)
from alfred import providers  # noqa: E402
claude, codex = providers.find_claude(), providers.find_codex()
os.environ.update(USERPROFILE=str(profile), HOME=str(profile), APPDATA=str(roaming), LOCALAPPDATA=str(local),
                  CLAUDE_CONFIG_DIR=str(root / 'claude-config'), CODEX_HOME=str(root / 'codex-home'), ALFRED_HOME=str(root / 'alfred-home'))
(root / 'claude-config').mkdir()
(root / 'codex-home').mkdir()
from alfred import connect  # noqa: E402
connect.ClaudeCode.exe = lambda self: claude
connect.Codex.exe = lambda self: codex

results = []


def report(name, ok, detail=''):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  -- ' + detail if detail else ''}")


spec = connect.server_spec()
report('entry passes this Alfred\'s data folder', spec['env'].get('ALFRED_HOME') == str(root / 'alfred-home'), json.dumps(spec['env']))

# ---- fresh profile: the JSON tools' folders as their installers leave them
(profile / '.cursor').mkdir()
(roaming / 'Code' / 'User').mkdir(parents=True)
(roaming / 'Code' / 'User' / 'settings.json').write_text('{"editor.fontSize": 14}', encoding='utf-8')
(profile / '.codeium' / 'windsurf').mkdir(parents=True)
moved = local / 'Claude-Data' / 'claude_pkg'
moved.mkdir(parents=True)
(moved / 'claude_desktop_config.json').write_text(json.dumps({'preferences': {'keep': True}}), encoding='utf-8')
(roaming / 'Claude').mkdir()
(roaming / 'Claude' / '.user-data-left.claude_pkg').write_text('', encoding='utf-8')
(profile / '.gemini').mkdir()
(profile / '.gemini' / 'settings.json').write_text(json.dumps({'theme': 'Default'}), encoding='utf-8')

found = {t.id for t in connect.found_tools()}
report('every installed tool is found', {'claude_code', 'codex', 'claude_desktop', 'cursor', 'vscode', 'windsurf', 'gemini'} <= found, str(sorted(found)))
report('VS Code Insiders (not installed) is not found', 'vscode_insiders' not in found)

for tool in connect.found_tools():
    try:
        tool.connect()  # adds the entry, reads it back, starts the server from another folder, MCP handshake
        status = tool.status()
        report(f'{tool.name}: connect + server check', status['state'] == 'connected', status['detail'])
    except Exception as error:
        report(f'{tool.name}: connect + server check', False, repr(error))

# ---- what the tools themselves see
env = dict(os.environ)
out = subprocess.run([claude, 'mcp', 'list'], capture_output=True, text=True, encoding='utf-8', errors='replace', env=env,
                     cwd=str(root), timeout=120).stdout
line = next((l for l in out.splitlines() if l.startswith('alfred')), '')
report('claude mcp list (from another folder): Connected', 'Connected' in line and 'Failed' not in line, line.strip())
out = subprocess.run([codex, 'mcp', 'list'], capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=120).stdout
report('codex mcp list shows alfred', 'alfred' in out, ' '.join(out.split())[:160])
desktop = json.loads((moved / 'claude_desktop_config.json').read_text(encoding='utf-8'))
report('Claude Desktop: preferences kept, entry added', desktop.get('preferences') == {'keep': True} and 'alfred' in desktop.get('mcpServers', {}))
report('Claude Desktop: nothing written to the folder it left', not (roaming / 'Claude' / 'claude_desktop_config.json').exists())
vscode = json.loads((roaming / 'Code' / 'User' / 'mcp.json').read_text(encoding='utf-8'))
report('VS Code: servers.alfred with type stdio', vscode['servers']['alfred'].get('type') == 'stdio')
report('VS Code: settings.json untouched', (roaming / 'Code' / 'User' / 'settings.json').read_text(encoding='utf-8') == '{"editor.fontSize": 14}')
gemini = json.loads((profile / '.gemini' / 'settings.json').read_text(encoding='utf-8'))
report('Gemini CLI: theme kept, entry added', gemini.get('theme') == 'Default' and 'alfred' in gemini['mcpServers'])

# ---- update after Alfred moves (an old entry), JSON with comments, remove
cursor = profile / '.cursor' / 'mcp.json'
data = json.loads(cursor.read_text(encoding='utf-8'))
data['mcpServers']['alfred']['command'] = r'C:\old\Alfred\.venv\Scripts\pythonw.exe'
cursor.write_text(json.dumps(data), encoding='utf-8')
report('Cursor: an old entry shows as Update', connect.tool('cursor').status()['state'] == 'outdated')
connect.tool('cursor').connect()
report('Cursor: Update fixes it', connect.tool('cursor').status()['state'] == 'connected')
jsonc = '{\n  // mine\n  "servers": {}\n}\n'
(roaming / 'Code' / 'User' / 'mcp.json').write_text(jsonc, encoding='utf-8')
try:
    connect.tool('vscode').connect()
    report('VS Code: a file with comments is refused', False)
except connect.ConnectError as error:
    report('VS Code: a file with comments is refused, unchanged',
           (roaming / 'Code' / 'User' / 'mcp.json').read_text(encoding='utf-8') == jsonc, str(error)[:80])
for tool_id in ('claude_code', 'codex', 'claude_desktop', 'cursor', 'windsurf', 'gemini'):
    tool = connect.tool(tool_id)
    tool.disconnect()
    report(f'{tool.name}: Remove', tool.status()['state'] == 'not_added')
report('Claude Desktop: preferences still kept after Remove',
       json.loads((moved / 'claude_desktop_config.json').read_text(encoding='utf-8')).get('preferences') == {'keep': True})

print(f'\n{sum(results)}/{len(results)} passed   (profile: {root})')
sys.exit(0 if all(results) else 1)
