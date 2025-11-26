"""
Utility to manage running commands in subprocesses.
Provides contextual information and higher-level monitoring
over the run commands.

@author: Baptiste Pestourie
@date: 20.11.2025
"""

from __future__ import annotations

import asyncio
import atexit
import io
import selectors
import signal
import subprocess
import time
import warnings
from asyncio import AbstractEventLoop
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from subprocess import Popen
from threading import Lock, Thread
from typing import Callable, Self


@dataclass
class CommandContext:
    """
    Stores information about the execution of a command
    run in a different process.
    """

    args: list[str] = field(default_factory=list)
    cwd: Path = field(default_factory=Path.cwd)
    exit_code: int | None = None  # None means not completed
    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)
    start_time: float | None = None
    execution_time: float | None = None

    def __hash__(self) -> int:
        """
        unsafe hash, hashes by ID -
        thius object is mutable, and expected to be mutated after instanciation,
        so using fields for a hash is not a reliable option.
        """
        return id(self)

    @property
    def pending(self) -> bool:
        """
        Whether execution is still running.
        """
        return self.exit_code is None

    @property
    def success(self) -> bool:
        """
        Whether the execution succedded,
        i.e., exit code was 0.
        """
        return self.exit_code == 0

    @property
    def command(self) -> str:
        """
        The command that's getting run, in a single-line format.
        """
        return " ".join(self.args)

    def render(self) -> str:
        """
        Renders a log-friendly string version of this object.
        """
        lines: list[str] = []
        header_elements = [f"[{self.cwd}]", ">", self.command]
        match self.exit_code:
            case 0:
                status = "OK"
            case None:
                status = "PENDING"
            case _:
                status = "[FAILED]"

        header_elements.append(status)
        if self.execution_time:
            header_elements.append(f"[{self.execution_time:.2f}] s")
        lines.append(" ".join(header_elements))
        if self.stdout:
            lines.extend(["Stdout", "------", *self.stdout])
        if self.stderr:
            lines.extend(["Stderr", "------", *self.stderr])
        return "\n".join(lines)

    def __str__(self) -> str:
        """
        Shows the command and the folder from which it was called.
        """
        return f"[{self.cwd}] {self.command}"


class ProcessGarbageCollector(Thread):
    """
    A independant thread that takes care of cleaning up processes
    when timing out waiting for their completion.
    Sends first SIGINT, and falls back to SIGKILL if the process
    did not stop within `sigint_allowance_time`.

    Intended to be used mainly as a singleton-style, global shared instance,
    but safe to use as standalone, non-global object.
    Use `get_singleton` to get a reference to the singleton instance.
    """

    def __init__(self, sigint_allowance_time: float = 1.0) -> None:
        """
        Parameters
        ----------
        sigint_allowance_time
            How much time is given to the process to terminate after sending SIGINT.
            Once that time is reached, falls back to SIGKILL.
        """
        super().__init__(daemon=True)
        self._watched_processes: dict[CommandContext, Popen[str]] = {}
        self._loop: AbstractEventLoop | None = None
        self._lock = Lock()
        self._sigint_allowance_time = sigint_allowance_time

    @classmethod
    @cache
    def get_singleton(cls) -> Self:
        """
        Returns a single, shared global instance of that garbage collector.
        For most purposes this should be the favored way of using this object.
        Registers the cleanup function of that object on exit and starts
        the collection thread in the background.
        """
        global_instance = cls()
        global_instance.enable_cleanup_on_exit()
        global_instance.start()
        return global_instance

    def enable_cleanup_on_exit(self) -> None:
        """
        Registers the `shutdown_handler` of this object
        on interpreter termination
        """
        atexit.register(self.shutdown_handler)

    def interrupt(self, process: Popen[str], context: CommandContext) -> None:
        """
        Registers a process for graceful termination by sending SIGINT and monitoring it.
        """
        try:
            process.send_signal(signal.SIGINT)
        except ProcessLookupError:
            return  # Process already dead
        self._watched_processes[context] = process

        with self._lock:
            if (loop := self._loop) is not None:
                self._schedule_kill(loop, process, context)

    def run(self) -> None:
        """
        Main loop of the garbage collector to manage terminating processes.
        """
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        with self._lock:
            for context, proc in self._watched_processes.items():
                self._schedule_kill(loop, proc, context)
        loop.run_forever()

    def _schedule_kill(
        self, loop: AbstractEventLoop, process: Popen[str], context: CommandContext
    ) -> None:
        """
        Schedules process kill once the sigint allowance time is reached.
        """
        left_time = (
            self._sigint_allowance_time
            if context.start_time is None
            else context.start_time + self._sigint_allowance_time - time.time()
        )
        loop.call_soon_threadsafe(
            loop.call_later,
            left_time,
            self._kill_process,
            process,
            context,
        )

    def _update_context(self, context: CommandContext, exit_code: int) -> None:
        """
        Updates the context info with the execution time and exit code returned
        by the process.
        """
        if context.start_time is not None:
            context.execution_time = time.time() - context.start_time
        context.exit_code = exit_code

    def _kill_process(self, proc: Popen[str], context: CommandContext) -> None:
        """
        Calls SIKILL on the process `proc` if it's not dead yet.
        """
        self._refresh_state()
        if context not in self._watched_processes:
            return
        proc.kill()
        self._update_context(context, exit_code=0x80 - signal.SIGKILL)

    def _refresh_state(self) -> None:
        """
        Checks the status of all monitored processes and untrack the
        ones that have terminated already.
        """
        ctime = time.time()
        terminated_procs: list[CommandContext] = []
        for context, proc in self._watched_processes.items():
            if (exit_code := proc.poll()) is not None:
                terminated_procs.append(context)
                if exit_code != context.exit_code:
                    warnings.warn(
                        "[Process Garbage Collector]: we sent SIGINT, "
                        f"but process returned exit code {exit_code}"
                    )
                    context.exit_code = exit_code
            if context.start_time is not None:
                context.execution_time = ctime - context.start_time
        for ctx in terminated_procs:
            del self._watched_processes[ctx]

    def shutdown_handler(self) -> None:
        """
        Kills all monitored processes on interpreter shutdown.
        """
        with self._lock:
            for proc in self._watched_processes.values():
                if proc.poll() is None:
                    proc.kill()


def call_command(
    *args: str,
    timeout: float | None = None,
    printer: Callable[[str], None] | None = None,
    cwd: Path | str | None = None,
    process_gc: ProcessGarbageCollector | None = None,
    on_popen: Callable[[Popen[str]], None] | None = None,
) -> CommandContext:
    """
    Calls a command without blocking on IO, using selectors.
    It reads stdout and stderr as data comes in.
    It will leave early if the timeout is reached.
    The optional printer is called for each line received.

    Parameters
    ----------
    timeout
        If passed, wait for completion for a maximum of `timeout`
        and submit the process to the ProcessGarbageCollector when
        timing out.
        The process will be interrupted and later killed and it fails to
        terminate before the PGC allowance time.
    printer
        Optional, a function that receive each line from stderr/stdout
        from the proces.
    cwd
        Optional, the folder from which the process was called,
        assumes Path.cwd() by default.
    process_gc
        Optional, a custom ProcessGarbageCollector.
        Defaults to the global one.
    on_popen
        Optional, a callback to call after creating the Popen object.
        Should take the Popen object as unique argument.
    """
    start_time = time.time()
    current_cwd = Path(cwd) if cwd else Path.cwd()
    cmd = " ".join(args)
    proc = Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=current_cwd,
        text=True,  # Enable text mode
        bufsize=1,  # Request line buffering
        shell=True,
    )
    if on_popen is not None:
        on_popen(proc)
    should_interrupt = False
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    sel = selectors.DefaultSelector()
    if proc.stdout:
        sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
    if proc.stderr:
        sel.register(proc.stderr, selectors.EVENT_READ, "stderr")

    while sel.get_map():
        remaining_timeout = None
        if timeout is not None:
            elapsed_time = time.time() - start_time
            if elapsed_time >= timeout:
                break
            remaining_timeout = timeout - elapsed_time

        events = sel.select(timeout=remaining_timeout)

        if not events:
            # Timeout in select
            break

        for key, _ in events:
            # key.fileobj will be an io.TextIOWrapper because text=True
            assert isinstance(key.fileobj, io.TextIOWrapper)
            line = key.fileobj.readline()
            if not line:  # EOF for text streams is an empty string
                sel.unregister(key.fileobj)
                continue

            # readline() includes the newline, rstrip() to remove it before printing/storing
            processed_line = line.rstrip("\n")

            if printer:
                printer(processed_line)

            if key.data == "stdout":
                stdout_lines.append(processed_line)
            else:  # stderr
                stderr_lines.append(processed_line)

    final_exit_code = proc.poll()

    if final_exit_code is None:  # Process still running -> timeout occurred
        should_interrupt = True
        # Assume standard behavior for termination by SIGINT (Ctrl+C)
        final_exit_code = 0x80 + signal.SIGINT  # 130

    execution_time = time.time() - start_time

    context = CommandContext(
        args=list(args),
        cwd=current_cwd,
        exit_code=final_exit_code,
        stdout=stdout_lines,
        stderr=stderr_lines,
        start_time=start_time,
        execution_time=execution_time,
    )
    if should_interrupt:
        process_gc = process_gc or ProcessGarbageCollector.get_singleton()
        process_gc.interrupt(proc, context)
    return context
