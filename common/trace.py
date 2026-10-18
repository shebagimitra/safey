# ============================================================
# COMPONENT: C0 - Common Trace & Diagnostics
# PURPOSE: Structured diagnostic logging and standardized component error handling.
# INPUT: Component ID, event messages, key-value summary attributes.
# OUTPUT: Formatted trace lines to stdout and append operations to data/trace.log.
# CALLED BY: All pipeline components (C0-C7)      CALLS: Standard library (datetime, sys, os, pathlib)
# IF IT BREAKS, CHECK: File system permissions for data/ directory, path resolution.
# ============================================================

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ComponentError(Exception):
    """IN: component (str), step (str), message (str).
    OUT: ComponentError exception instance.
    FAILS WITH: Never fails initialization."""

    def __init__(self, component: str, step: str, message: str) -> None:
        """IN: component (str), step (str), message (str).
        OUT: None.
        FAILS WITH: Never fails initialization."""
        # STEP 1: Initialize base exception and bind diagnostic attributes | takes: component, step, message | gives: configured error
        super().__init__(f"[{component}] step '{step}' failed: {message}")
        self.component: str = component
        self.step: str = step
        self.message: str = message


def trace(component: str, message: str, /, **fields: Any) -> None:
    """IN: component (str), message (str), keyword arguments for summary metadata.
    OUT: None.
    FAILS WITH: ComponentError if unable to write to trace log."""
    # STEP 1: Format timestamp and key-value fields | takes: component, message, fields | gives: log_line
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if fields:
        field_strs = [f"{k}={v}" for k, v in fields.items()]
        log_line = f"[{timestamp}] [{component}] {message} | {' '.join(field_strs)}"
    else:
        log_line = f"[{timestamp}] [{component}] {message}"

    # STEP 2: Emit formatted log line to stdout | takes: log_line | gives: console output
    print(log_line, flush=True)

    # STEP 3: Append log entry to data/trace.log | takes: log_line | gives: persisted file entry
    try:
        data_dir = os.environ.get("DATA_DIR", "data")
        log_path = Path(data_dir) / "trace.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")
    except Exception as exc:
        raise ComponentError(component, "append_trace_log", f"Failed writing to trace log at {log_path}: {exc}") from exc


def run_selftest() -> None:
    """IN: None (invoked via CLI argument --selftest).
    OUT: None (prints PASS/FAIL and exits).
    FAILS WITH: SystemExit on error."""
    # STEP 1: Test ComponentError instantiation | takes: dummy values | gives: verified exception instance
    try:
        err = ComponentError("C0", "test_step", "Sample diagnostic error")
        if err.component != "C0" or err.step != "test_step":
            print("FAIL: ComponentError attributes mismatch")
            sys.exit(1)
        # STEP 2: Test trace function execution and log file append | takes: test message | gives: stdout and file output
        trace("C0", "Self-test diagnostic event", test_id="selftest_001", status="ok")
        print("PASS")
    except Exception as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        run_selftest()
