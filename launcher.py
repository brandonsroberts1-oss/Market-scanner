#!/usr/bin/env python3
"""One-click launcher for Market Scanner.

Run by double-clicking "Start Market Scanner" - this script does everything the
README asks you to do by hand: checks your Python, builds an isolated
environment, installs the dependencies, starts the server and opens your
browser.

Deliberately uses only the Python standard library, because it has to run
*before* anything is installed. Keep it that way.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import venv
from pathlib import Path

MIN_PYTHON = (3, 10)
DEFAULT_PORT = 8000
ROOT = Path(__file__).resolve().parent

IS_WINDOWS = os.name == "nt"


# --------------------------------------------------------------------------
# Pretty output
# --------------------------------------------------------------------------
def _supports_colour() -> bool:
    if not sys.stdout.isatty():
        return False
    if IS_WINDOWS:
        # Modern Windows Terminal and Windows 10+ consoles handle ANSI once
        # virtual terminal processing is enabled; older ones do not.
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            return True
        except Exception:
            return False
    return True


COLOUR = _supports_colour()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOUR else text


def banner() -> None:
    line = "=" * 62
    print()
    print(_c(line, "36"))
    print(_c("  MARKET SCANNER", "1;36"))
    print(_c("  Options and equity scanner with paper trading", "36"))
    print(_c(line, "36"))
    print()


def step(message: str) -> None:
    print(f"  {_c('>', '36')} {message}")


def ok(message: str) -> None:
    print(f"  {_c('OK', '32')} {message}")


def warn(message: str) -> None:
    print(f"  {_c('!', '33')} {message}")


def fail(message: str) -> None:
    print()
    print(_c("  " + "-" * 58, "31"))
    print(_c("  SOMETHING WENT WRONG", "1;31"))
    print(_c("  " + "-" * 58, "31"))
    for paragraph in message.split("\n"):
        print(f"  {paragraph}")
    print()


# --------------------------------------------------------------------------
# Environment setup
# --------------------------------------------------------------------------
def check_python() -> None:
    if sys.version_info < MIN_PYTHON:
        need = ".".join(str(n) for n in MIN_PYTHON)
        have = platform.python_version()
        fail(
            f"Market Scanner needs Python {need} or newer, but this is Python {have}.\n"
            f"\n"
            f"Install a current Python from https://www.python.org/downloads/\n"
            f"On Windows, tick 'Add python.exe to PATH' in the installer.\n"
            f"Then double-click the launcher again."
        )
        raise SystemExit(1)


def venv_python(venv_dir: Path) -> Path:
    if IS_WINDOWS:
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def ensure_venv(venv_dir: Path) -> Path:
    """Create the virtual environment if it is missing or broken."""
    python = venv_python(venv_dir)

    if python.exists():
        # A venv copied between machines (or left over from a moved folder)
        # keeps stale absolute paths and fails in confusing ways. Verify it.
        try:
            subprocess.run([str(python), "-c", "import sys"], check=True,
                           capture_output=True, timeout=60)
            return python
        except Exception:
            warn("The existing environment looks broken; rebuilding it.")
            shutil.rmtree(venv_dir, ignore_errors=True)

    step("Creating an isolated Python environment (first run only)...")
    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    except Exception as exc:
        hint = ""
        if "ensurepip" in str(exc) or "pip" in str(exc).lower():
            hint = ("\nOn Debian or Ubuntu you may need:  "
                    "sudo apt install python3-venv python3-pip")
        fail(f"Could not create the Python environment.\n\n{exc}{hint}")
        raise SystemExit(1)

    if not python.exists():
        fail(f"The environment was created but {python} is missing.")
        raise SystemExit(1)
    ok("Environment created.")
    return python


def _read_text(path: Path) -> str:
    """Read a file as UTF-8 regardless of the platform's default encoding."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _requirements_stamp() -> str:
    req = ROOT / "requirements.txt"
    return f"{req.stat().st_mtime_ns}:{req.stat().st_size}" if req.exists() else ""


def install_dependencies(python: Path, venv_dir: Path) -> None:
    """Install requirements, skipping the work when nothing has changed."""
    stamp_file = venv_dir / ".requirements-stamp"
    stamp = _requirements_stamp()
    if stamp and stamp_file.exists() and _read_text(stamp_file).strip() == stamp:
        ok("Dependencies already installed.")
        return

    step("Installing dependencies (this takes a minute the first time)...")
    commands = [
        [str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        [str(python), "-m", "pip", "install", "--quiet", "-r", str(ROOT / "requirements.txt")],
    ]
    for cmd in commands:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 and cmd is commands[-1]:
            detail = (result.stderr or result.stdout or "").strip()
            fail(
                "Could not install the dependencies.\n\n"
                f"{detail[-1200:]}\n\n"
                "The usual causes are no internet connection, a corporate proxy, "
                "or a firewall blocking pypi.org."
            )
            raise SystemExit(1)

    try:
        stamp_file.write_text(stamp, encoding="utf-8")
    except OSError:
        pass
    ok("Dependencies installed.")


def verify_install(python: Path) -> None:
    """Import the real application before starting the server.

    Catching an import failure here turns a wall of traceback into one readable
    message. It also catches environment problems that only show up on some
    platforms - a missing time zone database on Windows, for instance.
    """
    check = subprocess.run(
        [str(python), "-c",
         "import fastapi, uvicorn, httpx, numpy; import backend.main; print('ok')"],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    if "ok" in check.stdout:
        return

    detail = (check.stderr or check.stdout or "").strip()
    hint = ""
    if "ZoneInfoNotFoundError" in detail or "No time zone found" in detail:
        hint = ("\n\nThis is a missing time zone database. Reinstalling should fix "
                "it:\n  delete the .venv folder next to this launcher, then run "
                "the launcher again.")
    elif "ModuleNotFoundError" in detail:
        hint = ("\n\nA dependency is missing. Delete the .venv folder next to this "
                "launcher and run it again to rebuild the environment.")

    fail(f"The app could not start.\n\n{detail[-1200:]}{hint}")
    raise SystemExit(1)


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
def find_free_port(preferred: int) -> int:
    """Use the preferred port, or the next free one if it is taken."""
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return preferred


def wait_for_server(url: str, process: subprocess.Popen, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False                    # the server exited on its own
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.4)
    return False


def describe_provider(url: str) -> None:
    """Tell the user, in plain language, what data they are looking at."""
    try:
        with urllib.request.urlopen(url + "/api/status", timeout=5) as response:
            status = json.load(response)
    except Exception:
        return
    provider = status.get("provider", "unknown")
    session = (status.get("market") or {}).get("label", "")
    stale = (status.get("data") or {}).get("stale_count", 0)

    if status.get("realtime"):
        ok(f"Real-time data via {provider}.")
    else:
        ok(f"Live data via {provider} (option chains may be delayed ~15 minutes).")
    if session:
        print(f"     Market is currently: {session}.")
    if stale:
        warn(f"{provider} did not answer for {stale} symbol(s); showing the most "
             f"recent real prices already fetched, marked stale.")


def open_browser_when_ready(url: str, process: subprocess.Popen) -> None:
    if not wait_for_server(url + "/api/status", process):
        if process.poll() is None:
            warn("The server is taking longer than expected to answer.")
            print(f"     Try opening {url} in your browser anyway.")
        return

    describe_provider(url)
    print()
    print(_c("  " + "-" * 58, "32"))
    print(_c(f"   Market Scanner is running:   {url}", "1;32"))
    print(_c("  " + "-" * 58, "32"))
    print()
    opened = False
    try:
        import webbrowser
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    if opened:
        print("  Your browser should have opened. If it did not, copy the")
        print("  address above into your browser.")
    else:
        print("  Open the address above in your browser.")
    print()
    print(_c("  To stop the app: close this window, or press Ctrl+C.", "90"))
    print()


def run_diagnostics_mode(python: Path) -> int:
    """`--diagnose`: report what every data source actually returns."""
    step("Probing data sources...")
    print()
    result = subprocess.run(
        [str(python), "-c",
         "import asyncio;"
         "from backend.providers.diagnostics import run_diagnostics, format_report;"
         "from backend.providers.selftest import run_selftest, format_selftest;"
         "print(format_report(asyncio.run(run_diagnostics())));"
         "print(format_selftest(asyncio.run(run_selftest())))"],
        cwd=str(ROOT),
    )
    return result.returncode


def main() -> int:
    banner()
    check_python()
    ok(f"Python {platform.python_version()} on {platform.system()}.")

    if not (ROOT / "backend" / "main.py").exists():
        fail(f"This launcher must sit next to the 'backend' folder.\n"
             f"It is currently in: {ROOT}\n\n"
             f"If you downloaded a ZIP, make sure you extracted it first and are "
             f"running the launcher from inside the extracted folder.")
        return 1

    venv_dir = ROOT / ".venv"
    python = ensure_venv(venv_dir)
    install_dependencies(python, venv_dir)
    verify_install(python)

    if "--diagnose" in sys.argv or "--diagnostics" in sys.argv:
        return run_diagnostics_mode(python)

    port = find_free_port(int(os.environ.get("PORT", DEFAULT_PORT)))
    if port != DEFAULT_PORT:
        warn(f"Port {DEFAULT_PORT} was busy, using {port} instead.")
    url = f"http://127.0.0.1:{port}"

    step("Starting the server...")
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    # Access logs scroll one line per request and bury the single message the
    # user actually needs. Warnings and errors still come through. Set
    # MARKET_SCANNER_VERBOSE=1 to get the full uvicorn output back.
    verbose = os.environ.get("MARKET_SCANNER_VERBOSE", "").strip() not in ("", "0")
    command = [str(python), "-m", "uvicorn", "backend.main:app",
               "--host", "127.0.0.1", "--port", str(port)]
    if not verbose:
        command += ["--log-level", "warning", "--no-access-log"]
    process = subprocess.Popen(command, cwd=str(ROOT), env=env)

    threading.Thread(target=open_browser_when_ready, args=(url, process),
                     daemon=True).start()

    try:
        return process.wait()
    except KeyboardInterrupt:
        print("\n  Stopping...")
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        print("  Stopped.")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:                                  # noqa: BLE001
        import traceback
        fail(f"Unexpected error:\n\n{traceback.format_exc()[-1500:]}")
        raise SystemExit(1) from exc
