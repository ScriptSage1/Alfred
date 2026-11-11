"""Being a good background citizen: how many CPUs this process may use, whether the PC runs on battery, and a
lower priority while indexing in the background. Windows helpers through ctypes; every function is best-effort,
never raises, and returns the safe default elsewhere."""
import ctypes
import logging
import os

log = logging.getLogger('alfred')
WINDOWS = os.name == 'nt'
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
NORMAL_PRIORITY_CLASS = 0x00000020
PROCESS_POWER_THROTTLING = 4  # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
POWER_THROTTLING_CURRENT_VERSION = 1
POWER_THROTTLING_EXECUTION_SPEED = 0x1


def _kernel32():
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    return kernel32


def usable_cpus():
    """Logical CPUs in this process's affinity mask (what the OS lets it run on), else os.cpu_count()."""
    fallback = os.cpu_count() or 1
    if not WINDOWS:
        try:
            return max(1, len(os.sched_getaffinity(0)))
        except (AttributeError, OSError):
            return fallback
    try:
        kernel32 = _kernel32()
        process, system = ctypes.c_size_t(), ctypes.c_size_t()
        if not kernel32.GetProcessAffinityMask(ctypes.c_void_p(kernel32.GetCurrentProcess()), ctypes.byref(process), ctypes.byref(system)):
            return fallback
        count = bin(process.value).count('1')
        return count if count > 0 else fallback
    except Exception:
        return fallback


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [('ACLineStatus', ctypes.c_ubyte), ('BatteryFlag', ctypes.c_ubyte), ('BatteryLifePercent', ctypes.c_ubyte),
                ('SystemStatusFlag', ctypes.c_ubyte), ('BatteryLifeTime', ctypes.c_ulong), ('BatteryFullLifeTime', ctypes.c_ulong)]


def on_battery():
    """True only when Windows says the PC runs on battery; unknown counts as mains power."""
    if not WINDOWS:
        return False
    try:
        status = _SystemPowerStatus()
        if not _kernel32().GetSystemPowerStatus(ctypes.byref(status)):
            return False
        return status.ACLineStatus == 0
    except Exception:
        return False


class _PowerThrottlingState(ctypes.Structure):
    _fields_ = [('Version', ctypes.c_ulong), ('ControlMask', ctypes.c_ulong), ('StateMask', ctypes.c_ulong)]


def background_priority(on):
    """Below-normal priority and EcoQoS (Windows 11's efficiency mode for the process) while ``on``; normal
    priority and no throttling otherwise. Returns True when the priority was set."""
    if not WINDOWS:
        return False
    try:
        kernel32 = _kernel32()
        process = ctypes.c_void_p(kernel32.GetCurrentProcess())
        done = bool(kernel32.SetPriorityClass(process, BELOW_NORMAL_PRIORITY_CLASS if on else NORMAL_PRIORITY_CLASS))
        try:
            state = _PowerThrottlingState(POWER_THROTTLING_CURRENT_VERSION, POWER_THROTTLING_EXECUTION_SPEED,
                                          POWER_THROTTLING_EXECUTION_SPEED if on else 0)
            kernel32.SetProcessInformation(process, PROCESS_POWER_THROTTLING, ctypes.byref(state), ctypes.sizeof(state))
        except Exception:  # older Windows: no EcoQoS
            pass
        return done
    except Exception as error:
        log.debug('Could not change the process priority: %s', error)
        return False
