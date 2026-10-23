"""Entry point.

    python -m alfred                  open Alfred (or show the running one)
    python -m alfred --startup        start hidden in the tray (used at sign-in)
    python -m alfred --track FOLDER   ask the running Alfred to track a folder
    python -m alfred --search QUERY   search with the running Alfred and print JSON
    python -m alfred --status         print the running Alfred's index status
"""
import json
import os
import sys

COMMANDS = {'--track': 'track', '--search': 'search', '--status': 'status'}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = next((a for a in argv if a in COMMANDS), None)
    if command is None:
        from .ui import run
        return run()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    arg = ' '.join(argv[argv.index(command) + 1:]).strip() or None
    if command == '--track' and arg:
        arg = os.path.abspath(arg)
    from .ui import send_command
    reply = send_command({'cmd': COMMANDS[command], 'arg': arg})
    if reply is None:
        print('Alfred is not running. Start it first (from the Start menu).', file=sys.stderr)
        return 1
    print(json.dumps(reply, indent=2, ensure_ascii=False))
    return 0 if not (isinstance(reply, dict) and reply.get('error')) else 1


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()  # a frozen Alfred.exe started as a PDF-table worker runs the worker, not Alfred
    sys.exit(main())
