"""The double-click launcher.

This is the first code a new user runs and the only code that runs *before*
anything is installed, so it must import on a bare interpreter and fail with
readable messages rather than tracebacks.
"""
import ast
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "launcher.py"
BAT = ROOT / "Start Market Scanner.bat"
COMMAND = ROOT / "Start Market Scanner.command"

sys.path.insert(0, str(ROOT))
import launcher  # noqa: E402


def test_launcher_uses_only_the_standard_library():
    """It runs before pip does, so a third-party import would be fatal."""
    tree = ast.parse(LAUNCHER.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    third_party = imported - set(sys.stdlib_module_names)
    assert not third_party, f"launcher imports non-stdlib modules: {third_party}"


def test_launcher_runs_on_a_bare_interpreter():
    """Import it with site-packages isolated, proving no hidden dependency."""
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         f"import sys; sys.path.insert(0, {str(ROOT)!r}); import launcher; print('ok')"],
        capture_output=True, text=True,
    )
    assert "ok" in result.stdout, result.stderr


def test_minimum_python_matches_what_the_code_needs():
    # Pydantic resolves `float | None` annotations at runtime, which needs 3.10.
    assert launcher.MIN_PYTHON >= (3, 10)


def test_find_free_port_returns_something_bindable():
    port = launcher.find_free_port(8000)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))       # must not raise


def test_find_free_port_steps_past_a_busy_one():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        taken = busy.getsockname()[1]
        assert launcher.find_free_port(taken) != taken


def test_venv_python_path_is_platform_correct():
    path = launcher.venv_python(Path("/tmp/x/.venv"))
    if os.name == "nt":
        assert path.parts[-2:] == ("Scripts", "python.exe")
    else:
        assert path.parts[-2:] == ("bin", "python")


def test_launcher_refuses_to_run_from_the_wrong_folder(tmp_path):
    """The most common user error: launching from outside the extracted ZIP."""
    stray = tmp_path / "launcher.py"
    stray.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    result = subprocess.run([sys.executable, str(stray)], capture_output=True,
                            text=True, cwd=str(tmp_path), timeout=120)
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "backend" in combined and "extracted" in combined
    assert "Traceback" not in combined, "users should see a message, not a traceback"


# ---------------- double-click wrappers ----------------
def test_both_wrappers_exist():
    assert BAT.exists(), "Windows launcher missing"
    assert COMMAND.exists(), "Mac/Linux launcher missing"


def test_command_file_is_executable():
    """Without the executable bit, double-clicking opens a text editor."""
    assert os.access(COMMAND, os.X_OK), "Start Market Scanner.command is not executable"


def test_bat_uses_crlf_line_endings():
    """cmd.exe mis-parses multi-line blocks in a LF-only .bat file."""
    raw = BAT.read_bytes()
    assert b"\r\n" in raw
    lone_lf = raw.replace(b"\r\n", b"").count(b"\n")
    assert lone_lf == 0, "the .bat has bare LF line endings"


def test_bat_has_balanced_blocks_and_escaped_parens():
    lines = BAT.read_bytes().decode().split("\r\n")
    depth = 0
    for line in lines:
        stripped = line.strip()
        if stripped.endswith("("):
            depth += 1
        elif stripped == ")":
            depth -= 1
        assert depth >= 0, "unbalanced ) in the .bat"
    assert depth == 0, "unclosed ( in the .bat"

    # Inside a block, an unescaped paren in ECHO terminates the block early.
    for i, line in enumerate(lines, 1):
        if line.strip().lower().startswith("echo"):
            for j, ch in enumerate(line):
                if ch in "()":
                    assert j > 0 and line[j - 1] == "^", \
                        f"unescaped paren in echo on .bat line {i}: {line}"


@pytest.mark.parametrize("script", [BAT, COMMAND])
def test_wrappers_move_to_their_own_directory(script):
    """Double-clicking runs from the user's home directory, not the app folder."""
    text = script.read_bytes().decode()
    assert ('cd /d "%~dp0"' in text) or ('cd "$(dirname "$0")"' in text)


@pytest.mark.parametrize("script", [BAT, COMMAND])
def test_wrappers_keep_the_window_open_on_failure(script):
    """A window that flashes shut tells the user nothing."""
    text = script.read_bytes().decode().lower()
    assert "pause" in text or "read -r -p" in text


@pytest.mark.parametrize("script", [BAT, COMMAND])
def test_wrappers_explain_a_missing_python(script):
    text = script.read_bytes().decode()
    assert "python.org/downloads" in text
    assert "3.10" in text


@pytest.mark.parametrize("script", [BAT, COMMAND])
def test_wrappers_check_the_python_version_not_just_presence(script):
    """An ancient Python on PATH must be rejected, not used."""
    text = script.read_bytes().decode()
    assert "version_info>=(3,10)" in text.replace(" ", "")


def test_command_wrapper_is_valid_shell():
    result = subprocess.run(["bash", "-n", str(COMMAND)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
