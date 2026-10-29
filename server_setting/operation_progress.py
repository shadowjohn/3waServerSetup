"""Live operation telemetry without publishing native output or command arguments.

The UI owns the optional context-local callback. Worker threads inherit it via
asyncio.to_thread; callback failures must never affect a package transaction.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import os
import queue
import re
import signal
import subprocess
import threading
import time


_SINK = ContextVar('threewa_operation_progress', default=None)
_PHASE = ContextVar('threewa_operation_phase', default='command')
PHASES = frozenset({'command', 'preflight', 'source-download', 'build-dependencies',
                   'compile', 'verify', 'activate', 'restore', 'package-install',
                   'apt-update', 'apt-upgrade'})
_COMMAND = re.compile(r'[a-z][a-z0-9.+-]{0,63}')
_FETCHED = re.compile(rb'^Fetched ([0-9]{1,15}(?:\.[0-9]{1,3})?) (B|kB|MB|GB) in ')


@contextmanager
def progress_context(callback):
    token = _SINK.set(callback)
    try:
        yield
    finally:
        _SINK.reset(token)


def emit_progress(phase, command=None, state='running', elapsed_seconds=0,
                  output_bytes=0, idle_seconds=0, download_bytes=None,
                  output_truncated=False):
    """Only fixed phases and numeric measurements cross this boundary."""
    callback = _SINK.get()
    if callback is None:
        return
    if (phase not in PHASES or state not in {'running', 'completed', 'failed'}
            or command is not None and not _COMMAND.fullmatch(command)):
        return
    event = {'phase': phase, 'command': command, 'state': state,
             'elapsed_seconds': round(max(0, elapsed_seconds), 2),
             'output_bytes': max(0, int(output_bytes)),
             'idle_seconds': round(max(0, idle_seconds), 2),
             'output_truncated': bool(output_truncated)}
    if download_bytes is not None:
        event['download_bytes'] = max(0, int(download_bytes))
    try:
        callback(event)
    except Exception:
        # A closing tab or a failed renderer cannot interrupt dpkg or a build.
        pass


@contextmanager
def phase_context(phase):
    if phase not in PHASES:
        raise ValueError('Unknown operation phase.')
    token = _PHASE.set(phase)
    emit_progress(phase)
    try:
        yield
    finally:
        _PHASE.reset(token)


def _kill(child):
    try:
        if os.name == 'posix':
            os.killpg(child.pid, signal.SIGKILL)
        else:
            child.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def run_captured(argv, *, timeout, command, cwd=None, env=None,
                 maximum_output=8 * 1024 * 1024, text=True, errors='replace'):
    """Drain both pipes continuously, retain bounded tails, and report activity.

    Excess output is drained rather than aborting a running dpkg/compile. The
    returned output_truncated flag lets metadata consumers reject partial data.
    No raw line, argument, environment, URL or native error enters the callback.
    """
    if (not isinstance(timeout, (int, float)) or timeout <= 0
            or not isinstance(maximum_output, int) or maximum_output < 2
            or not isinstance(command, str) or not _COMMAND.fullmatch(command)):
        raise ValueError('Invalid bounded command capture.')
    started = last_output = last_emit = time.monotonic()
    phase = _PHASE.get()
    buffers = [bytearray(), bytearray()]
    total = 0
    truncated = False
    downloaded = None
    pending = bytearray()
    messages = queue.Queue(maxsize=64)
    stop = threading.Event()
    readers = []
    child = None

    def report(state):
        now = time.monotonic()
        emit_progress(phase, command=command, state=state,
                      elapsed_seconds=now - started, output_bytes=total,
                      idle_seconds=now - last_output, download_bytes=downloaded,
                      output_truncated=truncated)

    def drain(index, stream):
        try:
            while not stop.is_set():
                data = stream.read1(4096)
                if not data:
                    break
                while not stop.is_set():
                    try:
                        messages.put((index, data), timeout=.1)
                        break
                    except queue.Full:
                        pass
        finally:
            while not stop.is_set():
                try:
                    messages.put((index, None), timeout=.1)
                    break
                except queue.Full:
                    pass

    report('running')
    try:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env,
            shell=False, start_new_session=os.name == 'posix')
        for index, stream in enumerate((child.stdout, child.stderr)):
            reader = threading.Thread(target=drain, args=(index, stream), daemon=True)
            reader.start()
            readers.append(reader)
        finished = 0
        while finished < 2 or child.poll() is None:
            now = time.monotonic()
            if now - started >= timeout:
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                index, data = messages.get(timeout=min(.1, max(.001, timeout - (now - started))))
            except queue.Empty:
                data = b''
                index = 0
            if data is None:
                finished += 1
            elif data:
                last_output = time.monotonic()
                total += len(data)
                buffers[index].extend(data)
                limit = maximum_output // 2
                if len(buffers[index]) > limit:
                    del buffers[index][:-limit]
                    truncated = True
                if command == 'apt-get' and index == 0:
                    pending.extend(data)
                    while b'\n' in pending:
                        line, _, remainder = pending.partition(b'\n')
                        pending = bytearray(remainder)
                        match = _FETCHED.match(line)
                        if match:
                            downloaded = int(float(match[1]) * {b'B': 1, b'kB': 1000, b'MB': 1000000, b'GB': 1000000000}[match[2]])
                    if len(pending) > 4096:
                        pending.clear()
            now = time.monotonic()
            if now - last_emit >= .25:
                report('running')
                last_emit = now
        code = child.wait()
        output = [bytes(buffer).decode('utf-8', errors) if text else bytes(buffer) for buffer in buffers]
        result = subprocess.CompletedProcess(argv, code, *output)
        result.output_truncated = truncated
        report('completed' if code == 0 else 'failed')
        return result
    except BaseException:
        if child is not None:
            _kill(child)
        report('failed')
        raise
    finally:
        stop.set()
        for reader in readers:
            reader.join(timeout=1)
        if child is not None:
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
