"""llama.cpp servers that Alfred starts and owns (M13).

One ``llama-server.exe`` per role, each holding one model:
  search  the text model, 1 slot; on the CPU unless Settings → search_on_gpu. Queries and small indexing jobs.
  index   the text model (plus the picture projector when pictures are waiting); on the GPU when the graphics check
          passed, else the CPU. Larger indexing jobs and every picture. Exits 60 s after the run's embedding work ends.
  (answer comes in phase B: gemma3:4b for Ask Alfred. ROLES, _args and NEED_MIB are where it joins.)

Sharing: the starter writes ALFRED_HOME\\run\\<role>.json (process, port, key, device…) once /health answers. Another
process (the MCP server, a second window) reads it, checks the process and /health, and uses that server. A lock file
per role (msvcrt.locking, released by Windows if the holder dies) lets only one process start a server at a time.

Ownership: every server is put in a Windows job object that ends it when the process that started it ends, even after
a crash, so a server never outlives the app or MCP server that started it. Its key is passed in the environment
(LLAMA_API_KEY), never on the command line, where other programs can read it.
"""
import atexit
import ctypes
import http.client
import json
import logging
import math
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import config, models
from .console import hidden

log = logging.getLogger('alfred')

BUILD = models.BUILD['build']
ROLES = ('search', 'index')        # 'answer' joins in phase B
EMBED_DIM = 768
SMALL_JOB_CPU = (16, 4)            # a small indexing job: at most this many passages, sent in batches of this size,
SMALL_JOB_GPU = (64, 16)           # through the search server (on the CPU / on the GPU); task 5 tunes these
LINGER_SECONDS = 60                # the index server waits this long for a new run before it exits
START_WAIT = 90                    # seconds a start may take (model load; Vulkan's slow first use)
FIRST_TIMEOUT = 60                 # the first request after a start
QUERY_TIMEOUT = 30                 # a query after that
BATCH_TIMEOUT = 180                # a batch of passages or pictures (the design names no limit; Ollama's code used 180-300 s)
FAILURE_WINDOW = 600               # a second failure on the GPU within this many seconds moves the role to the CPU
SEARCH_CPU_THREADS = 4
GPU_MARGIN_MIB = 512
MIN_GPU_MIB = 1024                 # a device with less memory in total is not used
NEED_MIB = {'search': 512, 'index': 512, 'index+pictures': 1640}  # measured: 0.5 GB text, 1.6 GB with the projector
GPU_CHECK_COSINE = 0.995
REFERENCE = Path(__file__).with_name('data') / 'gpu_reference.json'

# Tests: a command (list) that stands in for llama-server.exe, e.g. [sys.executable, 'tests/fake_llama.py'].
PROGRAM_OVERRIDE = None


class ServerError(RuntimeError):
    """A llama.cpp server could not be started or reached."""


class HttpError(ServerError):
    def __init__(self, status, detail):
        super().__init__(f'HTTP {status}: {detail[:300]}')
        self.status, self.detail = status, detail


class Gone(ServerError):
    """The server is not there any more (connection refused or reset, process ended)."""


# ------------------------------------------------------------------ places
def home_of(settings=None):
    home = getattr(settings, 'home', None)
    return Path(home) if home else config.data_home()


def _dir(home, name):
    path = Path(home) / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def install_dir():
    """The folder Alfred runs from (M4's installer puts the engine in its runtime\\llama)."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def program_dir(home=None):
    """ALFRED_LLAMA_DIR (tests, development) → the install folder's runtime\\llama → ALFRED_HOME\\runtime\\llama-<build>."""
    home = Path(home or config.data_home())
    for candidate in (os.environ.get('ALFRED_LLAMA_DIR'), install_dir() / 'runtime' / 'llama', home / 'runtime' / models.BUILD['name']):
        if candidate and (Path(candidate) / 'llama-server.exe').is_file():
            return Path(candidate)
    return None


def command(home=None):
    if PROGRAM_OVERRIDE:
        return list(PROGRAM_OVERRIDE)
    found = program_dir(home)
    if found is None:
        raise ServerError('The search engine (llama.cpp) is not installed')
    return [str(found / 'llama-server.exe')]


def missing(settings=None, pictures=False):
    """Why search by meaning (or, with ``pictures``, by what pictures show) cannot run now, or None."""
    home = home_of(settings)
    if not PROGRAM_OVERRIDE and program_dir(home) is None:
        return 'The search engine (llama.cpp) is not installed'
    if models.find('embed-text', home) is None:
        return 'The search model (EmbeddingGemma 2) is not downloaded'
    if pictures and models.find('embed-pictures', home) is None:
        return 'The picture part of the search model is not downloaded'
    return None


def _run(home, *args, timeout=20):
    out = subprocess.run(command(home) + list(args), capture_output=True, text=True, timeout=timeout,
                         stdin=subprocess.DEVNULL, **hidden())
    return (out.stdout or '') + (out.stderr or '')


_DEVICE = re.compile(r'^\s*(\S+):\s*(.+?)\s*\((\d+)\s*MiB,\s*(\d+)\s*MiB free\)\s*$')
_build_cache = {}


def devices(home=None):
    """[{'id', 'name', 'total_mib', 'free_mib'}] from ``llama-server --list-devices`` (empty: no usable GPU driver)."""
    try:
        text = _run(home, '--list-devices')
    except (OSError, ServerError, subprocess.SubprocessError):
        return []
    found = []
    for line in text.splitlines():
        match = _DEVICE.match(line)
        if match:
            found.append({'id': match[1], 'name': match[2], 'total_mib': int(match[3]), 'free_mib': int(match[4])})
    return found


def build(home=None):
    """The engine's build number (``--version``), asked once per program."""
    key = tuple(command(home))
    if key not in _build_cache:
        try:
            match = re.search(r'build (\w+)', _run(home, '--version'))
            _build_cache[key] = f'b{match[1]}' if match and match[1].isdigit() else (match[1] if match else 'unknown')
        except (OSError, subprocess.SubprocessError):
            _build_cache[key] = 'unknown'
    return _build_cache[key]


def driver_version(name):
    """The display driver's version for a device name, from the registry (no program started); '' when unknown."""
    if os.name != 'nt':
        return ''
    import winreg
    root = r'SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}'
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as display:
            for number in range(64):
                try:
                    sub = winreg.EnumKey(display, number)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(display, sub) as key:
                        desc = winreg.QueryValueEx(key, 'DriverDesc')[0]
                        if desc and (desc.lower() in name.lower() or name.lower() in desc.lower()):
                            return str(winreg.QueryValueEx(key, 'DriverVersion')[0])
                except OSError:
                    continue
    except OSError:
        pass
    return ''


# ------------------------------------------------------------- windows bits
_JOB = None
_job_lock = threading.Lock()


def _kernel32():
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    k32.SetInformationJobObject.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
    k32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    k32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
    k32.GetProcessTimes.argtypes = (ctypes.c_void_p,) + (ctypes.POINTER(ctypes.c_uint64),) * 4
    k32.K32GetProcessMemoryInfo.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    return k32


class _Basic(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64), ('LimitFlags', ctypes.c_uint32),
                ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', ctypes.c_uint32),
                ('Affinity', ctypes.c_size_t), ('PriorityClass', ctypes.c_uint32), ('SchedulingClass', ctypes.c_uint32)]


class _Extended(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', _Basic), ('IoInfo', ctypes.c_uint64 * 6), ('ProcessMemoryLimit', ctypes.c_size_t),
                ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


class _Memory(ctypes.Structure):
    _fields_ = [('cb', ctypes.c_uint32), ('PageFaultCount', ctypes.c_uint32)] + [(n, ctypes.c_size_t) for n in (
        'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
        'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]


def _tie_to_owner(process):
    """Put ``process`` in this process's job object, which ends it when this process ends (KILL_ON_JOB_CLOSE)."""
    global _JOB
    if os.name != 'nt':
        return
    k32 = _kernel32()
    with _job_lock:
        if _JOB is None:
            job = k32.CreateJobObjectW(None, None)
            info = _Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise ServerError(f'Could not create a job object (error {ctypes.get_last_error()})')
            _JOB = job  # never closed: Windows closes it, and ends the servers, when this process ends
    if not k32.AssignProcessToJobObject(_JOB, int(process._handle)):
        raise ServerError(f'Could not tie the server to Alfred (error {ctypes.get_last_error()})')


def process_created(pid):
    """Creation time of a running process, or None when it is not running (guards against a reused process id)."""
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            return 0
        except OSError:
            return None
    k32 = _kernel32()
    handle = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        code = ctypes.c_uint32()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
            return None
        times = [ctypes.c_uint64() for _ in range(4)]
        if not k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        return times[0].value
    finally:
        k32.CloseHandle(handle)


def process_memory_mib(pid):
    """Working set of a process in MiB, or None."""
    if os.name != 'nt':
        return None
    k32 = _kernel32()
    handle = k32.OpenProcess(0x1000 | 0x0010, False, int(pid))
    if not handle:
        return None
    try:
        info = _Memory()
        info.cb = ctypes.sizeof(info)
        if not k32.K32GetProcessMemoryInfo(handle, ctypes.byref(info), info.cb):
            return None
        return round(info.WorkingSetSize / 2 ** 20)
    finally:
        k32.CloseHandle(handle)


@contextmanager
def _file_lock(home, role, timeout=START_WAIT + 30):
    """One starter per role across processes. Windows releases the lock if its holder dies."""
    path = _dir(home, 'run') / f'{role}.lock'
    handle = open(path, 'a+b')
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise ServerError(f'Timed out waiting for another process to start the {role} server') from None
                time.sleep(0.05)
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        handle.close()


# ------------------------------------------------------------------ records
def record_path(home, role):
    return _dir(home, 'run') / f'{role}.json'


def read_record(home, role):
    try:
        return json.loads(record_path(home, role).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    temp = path.with_suffix(f'.{os.getpid()}.{threading.get_ident()}.tmp')
    temp.write_text(json.dumps(data, indent=1), encoding='utf-8')
    os.replace(temp, path)


def _remove_record(home, role, pid):
    record = read_record(home, role)
    if record and record.get('pid') == pid:
        try:
            record_path(home, role).unlink()
        except OSError:
            pass


# --------------------------------------------------------------------- http
_KEPT = {}  # port -> {thread id: HTTPConnection}: one kept-open connection per thread and server (saves about 20 ms a query)
_kept_lock = threading.Lock()


def _drop(port, thread=None):
    """Close the kept connections to ``port`` (of one thread, or all of them when the server stops)."""
    with _kept_lock:
        if thread is None:
            connections = list(_KEPT.pop(port, {}).values())
        else:
            connection = _KEPT.get(port, {}).pop(thread, None)
            connections = [connection] if connection is not None else []
    for connection in connections:
        connection.close()


def _request(port, method, path, payload=None, key=None, timeout=QUERY_TIMEOUT):
    """JSON request to a local server over a kept-open connection, with the key. Raises HttpError for an error
    status, Gone when the server is not reachable, ServerError for a timeout."""
    thread = threading.get_ident()
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = f'Bearer {key}'
    for attempt in (0, 1):
        with _kept_lock:
            con = _KEPT.get(port, {}).get(thread)
            reused = con is not None
            if con is None:
                con = _KEPT.setdefault(port, {})[thread] = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
        if reused:
            con.timeout = timeout
            if con.sock is not None:
                con.sock.settimeout(timeout)
        try:
            con.request(method, path, body, headers)
            response = con.getresponse()
            data = response.read()
        except TimeoutError:
            _drop(port, thread)
            raise ServerError(f'The search engine did not answer within {timeout} s') from None
        except (OSError, http.client.HTTPException) as error:
            _drop(port, thread)
            if reused and attempt == 0:
                continue  # the kept connection had been closed by the server: once more on a new one
            raise Gone(f'The search engine is not reachable ({error})') from None
        if response.status != 200:
            raise HttpError(response.status, data.decode('utf-8', 'replace'))
        try:
            return json.loads(data)
        except ValueError:
            raise ServerError('The search engine sent an unreadable answer') from None


def _health(port, timeout=2.0):
    try:
        return _request(port, 'GET', '/health', timeout=timeout).get('status') == 'ok'
    except ServerError:
        return False


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


# ------------------------------------------------------------------- server
class Server:
    """One llama-server process started by this process (it dies with this process)."""

    def __init__(self, role, home, args, device, mmproj, model_hash):
        self.role, self.home, self.args, self.device, self.mmproj, self.model_hash = role, Path(home), list(args), device, mmproj, model_hash
        self.process = self.port = self.key = self.marker = self.record = None
        self._log = None

    def start(self):
        log_path = _dir(self.home, 'logs') / f'llama-{self.role}.log'
        last = ''
        for attempt in range(3):  # a port taken between choosing and binding: another port
            self.port, self.key = free_port(), secrets.token_urlsafe(24)
            self._log = open(log_path, 'w', encoding='utf-8', errors='replace')  # restarted at each start; never verbose
            env = dict(os.environ, LLAMA_API_KEY=self.key)
            started = time.monotonic()
            try:
                self.process = subprocess.Popen(command(self.home) + self.args + ['--host', '127.0.0.1', '--port', str(self.port)],
                                                stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT, env=env,
                                                close_fds=True, **hidden())
            except OSError as error:
                self._log.close()
                raise ServerError(f'Could not start the search engine: {error}') from None
            try:
                _tie_to_owner(self.process)
            except ServerError:
                self.stop()
                raise
            while time.monotonic() - started < START_WAIT:
                if self.process.poll() is not None:
                    break
                if _health(self.port, timeout=1.0):
                    return self._ready(started)
                time.sleep(0.1)
            alive = self.process.poll() is None
            self.stop()
            last = self.log_tail()
            if alive:
                raise ServerError(f'The {self.role} server did not start within {START_WAIT} s')
            if 'bind' not in last.lower() and 'address' not in last.lower():
                break
        raise ServerError(f'The {self.role} server stopped while starting: {last[-300:].strip() or "no output"}')

    def _ready(self, started):
        if self.mmproj:
            self.marker = self.read_marker()
        self.record = {'role': self.role, 'pid': self.process.pid, 'created': process_created(self.process.pid), 'port': self.port,
                       'key': self.key, 'build': BUILD, 'model_hash': self.model_hash, 'device': self.device, 'mmproj': self.mmproj,
                       'marker': self.marker, 'owner': os.getpid(), 'started_at': time.time(),
                       'start_seconds': round(time.monotonic() - started, 2)}
        _write_json(record_path(self.home, self.role), self.record)
        log.info('llama.cpp %s server ready on %s in %.1f s (port %s)', self.role, self.device, self.record['start_seconds'], self.port)
        return self

    def read_marker(self):
        return _request(self.port, 'GET', '/props', key=self.key, timeout=10).get('media_marker')

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def log_tail(self, size=2000):
        try:
            return (_dir(self.home, 'logs') / f'llama-{self.role}.log').read_text(encoding='utf-8', errors='replace')[-size:]
        except OSError:
            return ''

    def stop(self):
        process = self.process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)
        if process is not None:
            _remove_record(self.home, self.role, process.pid)
        if self.port:
            _drop(self.port)
        if self._log is not None:
            self._log.close()
            self._log = None


# ------------------------------------------------------------------- client
class Client:
    """A live server of one role: this process's own, or another process's found through its record."""

    def __init__(self, role, home, record, server=None):
        self.role, self.home, self.server = role, Path(home), server
        self.port, self.key, self.pid = record['port'], record['key'], record['pid']
        self.device, self.mmproj, self.marker = record.get('device', 'cpu'), bool(record.get('mmproj')), record.get('marker')
        self.fresh = True  # no request answered yet: the first one gets FIRST_TIMEOUT (Vulkan's one-off slow first use)
        self.started_here = server is not None

    @property
    def base(self):
        return f'http://127.0.0.1:{self.port}'

    def usable(self, pictures=False):
        if pictures and not self.mmproj:
            return False
        return self.server.alive() if self.server is not None else True

    def post(self, path, payload, timeout=QUERY_TIMEOUT):
        if self.server is not None and not self.server.alive():
            raise Gone(f'The {self.role} server has stopped')
        result = _request(self.port, 'POST', path, payload, key=self.key, timeout=max(timeout, FIRST_TIMEOUT) if self.fresh else timeout)
        self.fresh = False
        return result

    def get(self, path, timeout=10):
        return _request(self.port, 'GET', path, key=self.key, timeout=timeout)

    def refresh_marker(self):
        """The picture marker is random per server start; read it again (after an HTTP 400 for a picture request)."""
        self.marker = self.get('/props').get('media_marker')
        if self.server is not None:
            self.server.marker = self.marker
            if self.server.record:
                self.server.record['marker'] = self.marker
                _write_json(record_path(self.home, self.role), self.server.record)
        return self.marker


_clients = {}       # (home, role) -> Client
_lingers = {}       # (home, role) -> threading.Timer
_failures = {}      # (home, role) -> [monotonic times of failures]
_forced_cpu = set()  # (home, role): moved to the CPU for this session after repeated GPU failures
_thread_locks = {}
_locks_lock = threading.Lock()


def _thread_lock(key):
    with _locks_lock:
        return _thread_locks.setdefault(key, threading.RLock())


def _live_record(home, role):
    record = read_record(home, role)
    if not record or not record.get('port') or not record.get('pid'):
        return None
    created = process_created(record['pid'])
    if created is None or (record.get('created') is not None and created != record['created']):
        return None
    return record if _health(record['port']) else None


def keep_alive_seconds(settings):
    try:
        minutes = int((settings or {}).get('keep_alive_minutes', 5))
    except (TypeError, ValueError):
        minutes = 5
    return max(5, min(minutes, 24 * 60)) * 60


def index_threads(settings):
    try:
        threads = int((settings or {}).get('index_threads') or 0)
    except (TypeError, ValueError):
        threads = 0
    return threads if threads > 0 else max(1, (os.cpu_count() or 4) - 2)


def _args(role, home, settings, device, mmproj):
    """Command-line arguments for a role (no host, port or key: Server adds those)."""
    text = models.find('embed-text', home)
    if text is None:
        raise ServerError('The search model (EmbeddingGemma 2) is not downloaded')
    args = ['-m', str(text), '--embedding', '--no-webui', '--offline', '-b', '2048', '-ub', '2048',
            '--sleep-idle-seconds', str(keep_alive_seconds(settings))]
    if mmproj:
        projector = models.find('embed-pictures', home)
        if projector is None:
            raise ServerError('The picture part of the search model is not downloaded')
        args += ['--mmproj', str(projector)] + (['--no-mmproj-offload'] if device == 'cpu' else [])
    else:
        args.append('--no-mmproj')
    gpu = ['--device', device, '-ngl', 'all'] if device != 'cpu' else ['--device', 'none', '-ngl', '0']
    if role == 'search':
        args += ['-c', '2048', '-np', '1'] + gpu + (['-t', str(SEARCH_CPU_THREADS)] if device == 'cpu' else [])
    elif role == 'index':
        if device == 'cpu':
            args += ['-c', '2048', '-np', '1'] + gpu + ['-t', str(index_threads(settings))]
        else:
            args += ['-c', '8192', '-np', '4'] + gpu  # 4 slots of 2,048 tokens: one request's inputs are spread over them
    else:
        raise ServerError(f'Unknown server role: {role}')
    return args


# ---------------------------------------------------------- the GPU check
def _gpu_key(home, device):
    return '|'.join((build(home), device['name'], driver_version(device['name']), models.FILES['embed-text'].sha256))


def _gpu_checks(home):
    try:
        return json.loads((Path(home) / 'run' / 'gpu_check.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def gpu_verdict(home, device):
    """The stored GPU check result for this build, device, driver and model, or None when it has not run."""
    return _gpu_checks(home).get(_gpu_key(home, device))


def _store_verdict(home, key, verdict):
    checks = _gpu_checks(home)
    if verdict is None:
        checks.pop(key, None)
    else:
        checks[key] = verdict
    _write_json(_dir(home, 'run') / 'gpu_check.json', checks)


def clear_gpu_verdict(home=None):
    """'Check again', or a failure on the GPU later: the next GPU start checks again."""
    home = Path(home or config.data_home())
    try:
        (home / 'run' / 'gpu_check.json').unlink()
    except OSError:
        pass


def reference():
    """The reference vectors shipped with Alfred: {'prompts': [...], 'vectors': [[...]], 'build', 'model_sha256'}."""
    try:
        return json.loads(REFERENCE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _unit(vector):
    norm = math.sqrt(sum(x * x for x in vector)) or 1e-12
    return [x / norm for x in vector]


def run_gpu_check(client):
    """Embed the 8 reference passages and the query on ``client`` and compare with the shipped vectors."""
    ref = reference()
    if not ref or ref.get('model_sha256') != models.FILES['embed-text'].sha256:
        return {'passed': False, 'reason': 'no reference vectors for this model', 'checked_at': time.strftime('%Y-%m-%d %H:%M')}
    started = time.monotonic()
    try:
        data = client.post('/v1/embeddings', {'input': ref['prompts']}, timeout=FIRST_TIMEOUT)['data']
        vectors = [_unit(d['embedding'][-EMBED_DIM:]) for d in sorted(data, key=lambda d: d['index'])]
    except (ServerError, KeyError, TypeError) as error:
        return {'passed': False, 'reason': f'the server failed: {error}', 'checked_at': time.strftime('%Y-%m-%d %H:%M')}
    cosines = [sum(a * b for a, b in zip(mine, _unit(theirs))) for mine, theirs in zip(vectors, ref['vectors'])]
    lowest = min(cosines) if len(cosines) == len(ref['vectors']) else 0.0
    passed = lowest >= GPU_CHECK_COSINE
    return {'passed': passed, 'min_cosine': round(lowest, 5), 'seconds': round(time.monotonic() - started, 2),
            'reason': '' if passed else f'vectors differ from the reference (lowest cosine {lowest:.4f})',
            'checked_at': time.strftime('%Y-%m-%d %H:%M')}


def _best_gpu(home, need):
    """(device, reason): the device with the most memory if it is usable and has room for ``need`` MiB, else None."""
    found = devices(home)
    if not found:
        return None, 'no usable graphics card'
    best = max(found, key=lambda d: d['total_mib'])
    if best['total_mib'] < MIN_GPU_MIB:
        return None, 'the graphics card has less than 1 GB of memory'
    if best['free_mib'] < need + GPU_MARGIN_MIB:
        return None, f"not enough free graphics memory ({best['free_mib']} MiB free)"
    return best, ''


def _wants_gpu(role, settings, home):
    if (str(home), role) in _forced_cpu:
        return False
    if role == 'search':
        return bool((settings or {}).get('search_on_gpu'))
    return (settings or {}).get('embed_device', 'auto') != 'cpu'


def _start(role, home, settings, pictures):
    """Start a server for ``role`` (the caller holds the role's lock). On the GPU only after the graphics check passed:
    the index role runs the check itself the first time; search never waits for it and uses the CPU until then."""
    model_hash = models.FILES['embed-text'].sha256
    if _wants_gpu(role, settings, home):
        device, reason = _best_gpu(home, NEED_MIB['index+pictures' if pictures else role])
        if device is not None:
            verdict = gpu_verdict(home, device)
            if verdict is None and role == 'index':
                log.info('Preparing the graphics card (once)…')
                server = Server(role, home, _args(role, home, settings, device['id'], pictures), device['id'], pictures, model_hash)
                try:
                    server.start()
                    verdict = run_gpu_check(Client(role, home, server.record, server))
                except ServerError as error:
                    verdict = {'passed': False, 'reason': f'the server failed: {error}', 'checked_at': time.strftime('%Y-%m-%d %H:%M')}
                _store_verdict(home, _gpu_key(home, device), verdict)
                log.info('Graphics card check: %s', verdict)
                if verdict['passed']:
                    return server
                server.stop()
            elif verdict and verdict.get('passed'):
                return Server(role, home, _args(role, home, settings, device['id'], pictures), device['id'], pictures, model_hash).start()
        else:
            log.info('%s server on the CPU: %s', role, reason)
    return Server(role, home, _args(role, home, settings, 'cpu', pictures), 'cpu', pictures, model_hash).start()


def client(role, settings=None, pictures=False, fresh=False):
    """A live server for ``role``: the one this process already uses, another process's (through its record), or a new
    one. ``pictures``: the server must have the picture projector. ``fresh``: the last one failed; find or start another."""
    if role not in ROLES:
        raise ServerError(f'Unknown server role: {role}')
    home = home_of(settings)
    key = (str(home), role)
    with _thread_lock(key):
        _cancel_linger(key)
        current = _clients.get(key)
        if current is not None and not fresh and current.usable(pictures):
            return current
        if current is not None and not fresh and current.server is not None and not current.server.alive():
            _failed(role, settings, current)  # our server ended by itself (a crash): counts as a failure
        if current is not None and current.server is not None:
            current.server.stop()  # ours, but stopped, failed, or without the projector now needed
        _clients.pop(key, None)
        with _file_lock(home, role):
            record = _live_record(home, role)
            if record is not None and (not pictures or record.get('mmproj')) and not (current is not None and record['pid'] == current.pid):
                found = Client(role, home, record)
            else:
                server = _start(role, home, settings, pictures)
                found = Client(role, home, server.record, server)
        _clients[key] = found
        return found


def call(role, path, payload, settings=None, pictures=False, timeout=QUERY_TIMEOUT):
    """POST to the role's server. If the server is gone, start one again once and repeat the request; a second failure
    on the GPU within 10 minutes moves the role to the CPU for this session and clears the GPU verdict."""
    current = client(role, settings, pictures)
    try:
        return current.post(path, payload, timeout)
    except Gone as error:
        log.warning('llama.cpp %s server failed (%s); starting it again', role, error)
        _drop(current.port)
        _failed(role, settings, current)
        return client(role, settings, pictures, fresh=True).post(path, payload, timeout)


def _failed(role, settings, current):
    home = home_of(settings)
    key = (str(home), role)
    now = time.monotonic()
    times = [t for t in _failures.get(key, []) if now - t < FAILURE_WINDOW] + [now]
    _failures[key] = times
    if len(times) >= 2 and current.device != 'cpu':
        log.warning('llama.cpp %s server failed twice on the GPU: moving it to the CPU for this session', role)
        _forced_cpu.add(key)
        clear_gpu_verdict(home)


def wake(role='search', settings=None):
    """Make sure the role's server runs and is awake (a tiny request ends its idle sleep). True when it was started now."""
    current = client(role, settings)
    started = current.started_here and current.fresh
    call(role, '/v1/embeddings', {'input': ['task: search result | query: warm up']}, settings, timeout=FIRST_TIMEOUT)
    return started


# ------------------------------------------------------------ indexing jobs
def small_job(settings=None):
    """(passages, batch): the largest job that goes through the search server, and its batch size."""
    return SMALL_JOB_GPU if (settings or {}).get('search_on_gpu') else SMALL_JOB_CPU


def running_here(role, settings=None):
    """Whether this process already has a live server of ``role`` that it started."""
    current = _clients.get((str(home_of(settings)), role))
    return current is not None and current.server is not None and current.server.alive()


def role_for_job(passages, pictures=0, settings=None):
    """'search' for a small job (no pictures, few passages), else 'index'. An index server this process already runs
    keeps the work."""
    if pictures or running_here('index', settings) or passages > small_job(settings)[0]:
        return 'index'
    return 'search'


def release(role='index', settings=None, after=LINGER_SECONDS):
    """The run's embedding work is done: stop this process's server of ``role`` after ``after`` seconds unless a new
    run asks for it first."""
    key = (str(home_of(settings)), role)
    with _thread_lock(key):
        current = _clients.get(key)
        if current is None or current.server is None:
            return
        _cancel_linger(key)
        timer = threading.Timer(after, _linger_over, (key, current))
        timer.daemon = True
        _lingers[key] = timer
        timer.start()


def _cancel_linger(key):
    timer = _lingers.pop(key, None)
    if timer is not None:
        timer.cancel()


def _linger_over(key, expected):
    with _thread_lock(key):
        if _clients.get(key) is expected:
            _clients.pop(key, None)
            _lingers.pop(key, None)
            if expected.server is not None:
                expected.server.stop()
                log.info('llama.cpp %s server stopped after the run', key[1])


def stop(role, settings=None):
    """Stop this process's server of ``role`` (another process's is left alone)."""
    key = (str(home_of(settings)), role)
    with _thread_lock(key):
        _cancel_linger(key)
        current = _clients.pop(key, None)
        if current is not None and current.server is not None:
            current.server.stop()


def stop_all():
    for home, role in list(_clients):
        key = (home, role)
        with _thread_lock(key):
            _cancel_linger(key)
            current = _clients.pop(key, None)
            if current is not None and current.server is not None:
                current.server.stop()


atexit.register(stop_all)


def status(settings=None):
    """The servers this ALFRED_HOME has now: [{'role', 'device', 'pid', 'memory_mib', 'mine', 'mmproj'}]."""
    home = home_of(settings)
    found = []
    for role in ROLES:
        record = _live_record(home, role)
        if record:
            found.append({'role': role, 'device': record.get('device'), 'pid': record['pid'], 'mmproj': bool(record.get('mmproj')),
                          'memory_mib': process_memory_mib(record['pid']), 'mine': record.get('owner') == os.getpid()})
    return found
