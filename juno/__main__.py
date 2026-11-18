"""Entry point.

    python -m juno                  open Juno (or show the running one)
    python -m juno --startup        start hidden in the tray (used at sign-in)
    python -m juno --track FOLDER   ask the running Juno to track a folder
    python -m juno --search QUERY   search with the running Juno and print JSON
    python -m juno --status         print the running Juno's index status
    python -m juno --mcp            serve search_files and read_passage to other AI tools over stdio (no window)
    python -m juno --selftest [DIR] check the installation without a window, in a temporary data folder
                                      (--selftest-log PATH: where the report goes; see juno.selftest)
"""
import json
import os
import sys

COMMANDS = {'--track': 'track', '--search': 'search', '--status': 'status'}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(a.startswith('--multiprocessing') for a in argv):
        return 0  # a worker start that freeze_support did not take: never open a window for it
    if '--selftest' in argv:  # before anything that relaunches, opens a window or reads the real data folder
        from .selftest import run
        return run(argv)
    if '--mcp' in argv:
        out, sys.stdout = sys.stdout, sys.stderr  # stdout belongs to the protocol; a stray print goes to stderr
        # numpy's maths library reserves about 30 MB per thread (640 MB on 20 threads) before a single search.
        # One thread keeps the idle server small and was no slower on the benchmark index.
        for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
            os.environ.setdefault(name, '1')
        from .mcp_server import run
        return run(out)
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
        print('Juno is not running. Start it first (from the Start menu).', file=sys.stderr)
        return 1
    print(json.dumps(reply, indent=2, ensure_ascii=False))
    return 0 if not (isinstance(reply, dict) and reply.get('error')) else 1


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()  # a frozen Juno.exe started as a PDF-table worker runs the worker, not Juno
    sys.exit(main())
