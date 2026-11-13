"""A second Alfred process for test_runtime: finds or starts the search server on the fake llama-server, prints the
server's process id, then waits (until killed, or for the given seconds).

    python runtime_child.py <seconds>
"""
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from alfred import config, models, runtime  # noqa: E402

runtime.PROGRAM_OVERRIDE = [sys.executable, str(HERE / 'fake_llama.py')]
models.find = lambda key, home=None: Path(os.environ['ALFRED_HOME']) / f'{key}.gguf'
settings = config.load(os.environ['ALFRED_HOME'])
client = runtime.client('search', settings)
print(client.pid, flush=True)
time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 3)
