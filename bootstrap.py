#!/usr/bin/env python3
"""
bootstrap.py — from a fresh clone to a running server, on Windows, macOS or Linux.

The only prerequisite is Python 3.9 or newer. This script makes a private
virtual environment beside the code, installs the four runtime dependencies
into it, and starts the console. It touches nothing outside this folder and
never installs anything system-wide.

    python bootstrap.py                 set up, then open the console
    python bootstrap.py --no-start      set up only
    python bootstrap.py --with-demo     also install Playwright for the UI suites
    python bootstrap.py --port 4100     serve the console somewhere else

Run it again any time: it reuses the environment it already made and only
installs what is missing, so it is safe to repeat.
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
MIN_PYTHON = (3, 9)
WINDOWS = os.name == "nt"


def say(step, text):
    print(f"  {step}  {text}", flush=True)


def venv_python():
    """Where this platform puts the interpreter inside a virtual environment."""
    return VENV / ("Scripts/python.exe" if WINDOWS else "bin/python")


def check_python():
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is needed; this is "
            f"{platform.python_version()}.\n"
            "  Windows : https://www.python.org/downloads/ (tick 'Add python.exe to PATH')\n"
            "  macOS   : brew install python@3.12\n"
            "  Linux   : sudo apt install python3 python3-venv   (or your package manager)")
    say("ok", f"Python {platform.python_version()} on {platform.system()}")


def make_venv():
    if venv_python().exists():
        say("ok", f"virtual environment already at {VENV.name}{os.sep}")
        return
    say("..", f"creating {VENV.name}{os.sep} — this folder holds the dependencies")
    try:
        venv.EnvBuilder(with_pip=True, clear=False).create(VENV)
    except Exception as exc:
        hint = ("\n  On Debian/Ubuntu the venv module ships separately: "
                "sudo apt install python3-venv" if not WINDOWS else "")
        raise SystemExit(f"could not create the virtual environment: {exc}{hint}")
    if not venv_python().exists():
        raise SystemExit(f"the virtual environment is missing {venv_python()}")
    say("ok", "virtual environment created")


def pip_install(*args, label=""):
    command = [str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check", *args]
    result = subprocess.run(command, cwd=str(HERE))
    if result.returncode != 0:
        raise SystemExit(f"pip failed while installing {label or ' '.join(args)}.\n"
                         f"  Behind a proxy? Set HTTPS_PROXY and run this again.")


def install_requirements():
    requirements = HERE / "requirements.txt"
    if not requirements.exists():
        raise SystemExit("requirements.txt is missing — is this the full repository?")
    say("..", "installing dependencies (flask, pyyaml, jsonschema)")
    pip_install("-q", "--upgrade", "pip", label="pip")
    pip_install("-q", "-r", str(requirements), label="requirements.txt")
    say("ok", "dependencies installed")


def install_demo():
    if shutil.which("npm") is None:
        say("--", "npm not found — skipping the browser suites (they are optional)")
        return
    say("..", "installing Playwright for the UI suites (a few minutes the first time)")
    demo = HERE / "demo"
    if subprocess.run(["npm", "install"], cwd=str(demo)).returncode != 0:
        say("--", "npm install failed — the browser suites will not run, nothing else changes")
        return
    subprocess.run(["npx", "playwright", "install", "chromium"], cwd=str(demo))
    say("ok", "browser suites ready:  node demo/e2e.js")


def verify():
    """Import the dependencies in the new environment, so failure is visible now."""
    probe = "import flask, yaml, jsonschema; print('imports ok')"
    result = subprocess.run([str(venv_python()), "-c", probe],
                            cwd=str(HERE), capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"the environment is incomplete:\n{result.stderr.strip()}")
    say("ok", "everything imports")


def start(port):
    activate = (f"{VENV.name}\\Scripts\\activate" if WINDOWS
                else f"source {VENV.name}/bin/activate")
    print("\n" + "-" * 66)
    print(f"  Console:  http://127.0.0.1:{port}")
    print(f"  Stop   :  Ctrl-C")
    print(f"\n  Next time, either run this script again or activate the")
    print(f"  environment yourself:\n      {activate}\n      python console.py")
    print("-" * 66 + "\n", flush=True)
    environment = dict(os.environ, CONSOLE_PORT=str(port), PYTHONUNBUFFERED="1")
    try:
        return subprocess.call([str(venv_python()), "console.py"],
                               cwd=str(HERE), env=environment)
    except KeyboardInterrupt:
        return 0


def main():
    ap = argparse.ArgumentParser(description="Set up and run mockd")
    ap.add_argument("--no-start", action="store_true", help="set up, do not run")
    ap.add_argument("--with-demo", action="store_true", help="also install Playwright")
    ap.add_argument("--port", type=int, default=4100)
    args = ap.parse_args()

    print("\nmockd setup\n")
    check_python()
    make_venv()
    install_requirements()
    verify()
    if args.with_demo:
        install_demo()

    if args.no_start:
        run = (f"{VENV.name}\\Scripts\\python console.py" if WINDOWS
               else f"{VENV.name}/bin/python console.py")
        print(f"\nReady. Start it with:\n    {run}\n")
        return 0
    return start(args.port)


if __name__ == "__main__":
    sys.exit(main())
