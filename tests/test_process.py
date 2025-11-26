import signal
import sys
import time
from pathlib import Path

import pytest

from smelt.process import ProcessGarbageCollector, call_command

# The command echoes 3 numbers with a 100ms delay between each.
# Total execution time is ~300ms.
CMD = "sleep .1; echo 1; sleep .1; echo 2; sleep .1; echo 3"

UNSTOPPABLE_SCRIPT = """
import signal
import time
import sys

def handle_sigint(sig, frame):
    print("SIGINT ignored", flush=True)

signal.signal(signal.SIGINT, handle_sigint)

print("started", flush=True)
while True:
    time.sleep(1)
"""


def test_call_command_success() -> None:
    """
    Tests the successful execution of a command, checking exit code and stdout.
    The command should run to completion.
    """
    result = call_command(CMD)

    assert result.success is True
    assert result.exit_code == 0
    assert result.stdout == ["1", "2", "3"]
    assert result.stderr == []
    assert result.execution_time is not None
    # Check that it took at least 300ms.
    assert result.execution_time >= 0.3


def test_call_command_printer_timing() -> None:
    """
    Tests that the printer function is called progressively as output is generated,
    not all at once at the end.
    """
    received_lines_with_ts = []

    def printer_with_timestamp(line: str) -> None:
        received_lines_with_ts.append((line, time.time()))

    result = call_command(CMD, printer=printer_with_timestamp)

    assert result.exit_code == 0

    received_lines = [line for line, ts in received_lines_with_ts]
    assert received_lines == ["1", "2", "3"]

    # Verify that the prints happened at roughly 100ms intervals.
    timestamps = [ts for line, ts in received_lines_with_ts]

    # Check deltas between prints. Allow a generous range to avoid flaky tests.
    delta1 = timestamps[1] - timestamps[0]
    delta2 = timestamps[2] - timestamps[1]

    # These should be around 0.1 seconds. We check for a wide range [0.08, 0.25]
    # which is equivalent to 0.165 +/- 0.085
    assert delta1 == pytest.approx(0.165, abs=0.085)
    assert delta2 == pytest.approx(0.165, abs=0.085)


def test_call_command_timeout() -> None:
    """
    Tests that a timeout interrupts the command, resulting in a non-zero exit code
    and partial output.
    """
    # Total command time is > 0.3s. A timeout of 0.15s should cut it off after the first echo.
    timeout = 0.15

    result = call_command(CMD, timeout=timeout)
    assert result.success is False
    # On Unix, a process terminated by SIGINT (like Ctrl+C) often returns 130.
    assert result.exit_code == 130

    # Only the first number should be in stdout.
    assert result.stdout == ["1"]
    assert result.stderr == []

    assert result.execution_time is not None
    # Check that the function returned shortly after the timeout was hit.
    # We expect the execution time to be in the range [timeout, timeout + 0.1s].
    # This is equivalent to timeout + 0.05s +/- 0.05s
    assert result.execution_time == pytest.approx(timeout + 0.05, abs=0.05)


def test_gc_sigkill_fallback(tmp_path: Path) -> None:
    """
    Tests that the ProcessGarbageCollector falls back to SIGKILL if a process
    ignores SIGINT.
    """

    script_path = tmp_path / "ignore_sigint.py"
    script_path.write_text(UNSTOPPABLE_SCRIPT)

    procs = []

    timeout = sigint_allowance = 0.2
    # 2. Create a custom GC with a short allowance time for a fast test
    custom_gc = ProcessGarbageCollector(sigint_allowance_time=sigint_allowance)
    custom_gc.start()

    # 3. Run the command that will time out
    command_to_run = f"{sys.executable} {script_path}"
    result = call_command(
        command_to_run,
        timeout=timeout,
        process_gc=custom_gc,
        on_popen=procs.append,
    )

    # 4. Verify the immediate result from call_command
    assert result.success is False
    assert result.exit_code == 130  # Assumed SIGINT exit code
    assert result.stdout == ["started"]

    # 5. Wait for the GC to do its work (SIGINT allowance + SIGKILL)
    time.sleep(sigint_allowance + 0.1)

    # 6. Verify that the process was eventually SIGKILLed
    assert len(procs) == 1
    process = procs[0]

    # After being killed, poll() will return the exit code.
    # SIGKILL is signal 9, so the return code should be -9.
    assert process.poll() == -signal.SIGKILL


def test_gc_atexit_handler(tmp_path: Path) -> None:
    """
    Checks that the process garbage coillector kills everything immediately
    when the interpreter shutdown handler is called.
    In real conditions, this would be injected by atexit.
    """

    script_path = tmp_path / "ignore_sigint.py"
    script_path.write_text(UNSTOPPABLE_SCRIPT)

    procs = []

    # 2. Create a custom GC with a short allowance time for a fast test
    custom_gc = ProcessGarbageCollector()
    custom_gc.start()

    # 3. Run the command that will time out
    command_to_run = f"{sys.executable} {script_path}"
    result = call_command(
        command_to_run,
        timeout=0.1,
        process_gc=custom_gc,
        on_popen=procs.append,
    )

    # 4. Verify the immediate result from call_command
    assert result.success is False
    assert result.exit_code == 130  # Assumed SIGINT exit code
    assert result.stdout == ["started"]

    # 6. Verify that the process was eventually SIGKILLed
    assert len(procs) == 1
    process = procs[0]

    assert process.poll() == None
    # Simulating interpreter exit
    custom_gc.shutdown_handler()
    # process should stop now
    assert process.wait(0.1)
    assert process.poll() == -signal.SIGKILL
