#!/usr/bin/env python3
"""Run job-tracker's web UI against private copies of everything it can write.

    preview.py serve [--dir D] [--port N] [--db PATH] [--no-seed]   long-running; Ctrl-C stops it
    preview.py check [--dir D] [--timeout S]                        smoke-test a running preview

Standard library only, so it can explain a missing .venv rather than die importing yaml.
"""
import argparse
import hashlib
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOST = "127.0.0.1"  # serve has no auth and can edit files, so this is not an option
DEFAULT_DIR = Path.home() / ".cache" / "job-tracker-preview"
DEFAULT_PORT = 8791
MARKER = "preview.json"
GRACE_SECONDS = 15

# Copied in, then redirected. A missing source just stays missing.
COPIED = {
    "JOBTRACKER_COMPANIES": "companies.yaml",
    "JOBTRACKER_CRITERIA": "criteria.yaml",
    "JOBTRACKER_PROFILE": "profile.yaml",
    "JOBTRACKER_ANSWERS": "answers.yaml",
    "JOBTRACKER_KEYWORDS": "keywords.yaml",
    "JOBTRACKER_PLUGINS": "plugins.yaml",
    "JOBTRACKER_RESUME_TEX": "resume.tex",
    "JOBTRACKER_COVERLETTER_TEX": "coverletter.tex",
}
# Redirected and started empty.
EMPTY_DIRS = {
    "JOBTRACKER_RESUMES": "resumes",
    "JOBTRACKER_LETTERS": "letters",
    "JOBTRACKER_TAILORED": "tailored",
    "JOBTRACKER_SUBMISSIONS": "submissions",
    "JOBTRACKER_BROWSER_PROFILE": "browser",
}
# Not copied on purpose: with no downloads.yaml here, saves fall through to the
# JOBTRACKER_DOWNLOAD_* variables below instead of the real folders the real file names.
NOT_COPIED = {"JOBTRACKER_DOWNLOADS": "downloads.yaml", "JOBTRACKER_DB": "state.db"}
SAVE_FOLDERS = ("RESUME", "LETTER", "RESUME_TEX", "LETTER_TEX")
# Real files the check step proves unchanged.
WATCHED = ("companies.yaml", "criteria.yaml", "keywords.yaml", "plugins.yaml", "answers.yaml",
           "downloads.yaml", "profile.yaml", "data/state.db", "data/state.db-wal",
           "data/state.db-shm")
ROUTES = ("/healthz", "/readyz", "/", "/jobboards", "/applications", "/companies", "/tuning",
          "/settings")


def repo_root() -> Path:
    """The checkout you are standing in, so a worktree previews its own code."""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, check=True).stdout.strip()
        if (Path(top) / "jobtracker" / "cli.py").exists():
            return Path(top)
    except (OSError, subprocess.CalledProcessError):
        pass
    return Path(__file__).resolve().parents[4]


def git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return done.stdout.strip()


def die(message: str) -> None:
    print(f"preview: {message}", file=sys.stderr)
    raise SystemExit(1)


def fingerprint(root: Path) -> dict:
    out = {}
    for name in WATCHED:
        path = root / name
        out[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return out


def isolation_env(target: Path) -> dict:
    env = dict(os.environ)
    for var, name in {**COPIED, **EMPTY_DIRS, **NOT_COPIED}.items():
        env[var] = str(target / name)
    for kind in SAVE_FOLDERS:
        env[f"JOBTRACKER_DOWNLOAD_{kind}"] = str(target / "saved")
    env.pop("JOBTRACKER_DISCORD_TOKEN", None)  # a preview has no business holding a credential
    return env


def refuse_unredirected_settings(root: Path, env: dict) -> None:
    """A setting added to config.py later, defaulting into the repo, would otherwise write to
    the real file from inside a "safe" preview and nothing would say so."""
    source = (root / "jobtracker" / "config.py").read_text()
    found = re.findall(r'environ\.get\(\s*"(JOBTRACKER_\w+)"\s*,\s*(?:ROOT|_DEFAULT)', source)
    missing = sorted(var for var in found
                     if var not in {**COPIED, **EMPTY_DIRS, **NOT_COPIED})
    if missing:
        die(f"config.py has settings this script does not redirect: {', '.join(missing)}. "
            "Add them to COPIED or EMPTY_DIRS in preview.py before running it.")


def copy_database(source: Path, target: Path) -> bool:
    """Plain copies, never a connection: even `mode=ro` on a WAL database creates -shm and -wal
    beside the source. The -wal comes too, because it can hold rows the main file does not yet,
    and SQLite replays it when the copy is first opened."""
    if not source.exists():
        return False
    shutil.copy2(source, target)
    wal = Path(f"{source}-wal")
    if wal.exists():
        shutil.copy2(wal, f"{target}-wal")
    return True


def build(root: Path, target: Path, source_db: Path, seed: bool, env: dict, python: Path) -> None:
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target, 0o700)  # answers.yaml is personal
    for var, name in COPIED.items():
        source = Path(os.environ.get(var) or root / name)
        if source.exists():
            shutil.copy2(source, target / name)
    for name in EMPTY_DIRS.values():
        (target / name).mkdir(exist_ok=True)
    (target / "saved").mkdir(exist_ok=True)

    if not copy_database(source_db, target / "state.db"):
        print(f"preview: no database at {source_db}, starting empty")
    if seed:
        script = Path(__file__).with_name("seed.py")
        # A script's import path is its own folder, so the repo has to be named.
        subprocess.run([str(python), str(script), str(target / "state.db")], cwd=root,
                       env={**env, "PYTHONPATH": str(root)}, check=True)


def responding(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/healthz", timeout=2) as res:
            return res.status == 200
    except (OSError, urllib.error.URLError):
        return False


def running(info: dict) -> bool:
    """Both halves: a dead preview's port may since have gone to a different one."""
    return alive(info["pid"]) and responding(info["url"])


def free_port(start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((HOST, port))
                return port
            except OSError:
                continue
    die(f"no free port between {start} and {start + 19}")


def cmd_serve(args: argparse.Namespace) -> None:
    root = repo_root()
    target = Path(args.dir).expanduser().resolve()
    python = root / ".venv" / "bin" / "python"
    if not python.exists():
        die(f"{python} is missing. It is untracked and nothing recreates it. Rebuild it with: "
            "python3 -m venv .venv && .venv/bin/pip install requests PyYAML httpx pytest "
            "opentelemetry-api opentelemetry-sdk")
    if target == root or root in target.parents:
        die(f"--dir must be outside the repo, got {target}")

    env = isolation_env(target)
    refuse_unredirected_settings(root, env)

    marker = target / MARKER
    reused = marker.exists()
    if reused:
        previous = json.loads(marker.read_text())
        if running(previous):
            print(f"preview: already running at {previous['url']}")
            return
        print(f"preview: keeping the data already in {target} (use a new --dir for a fresh copy)")
    else:
        source_db = Path(args.db or os.environ.get("JOBTRACKER_DB") or root / "data" / "state.db")
        build(root, target, source_db.expanduser(), not args.no_seed, env, python)

    port = free_port(args.port)
    url = f"http://{HOST}:{port}"
    marker.write_text(json.dumps({
        "url": url,
        "pid": os.getpid(),  # exec below keeps it
        "dir": str(target),
        "root": str(root),
        "branch": git(root, "branch", "--show-current"),
        "commit": git(root, "rev-parse", "--short", "HEAD"),
        "real_files": fingerprint(root),
    }, indent=2))

    print(f"preview: {url} from {root} ({git(root, 'branch', '--show-current')}), "
          f"data in {target}", flush=True)
    os.chdir(root)
    os.execve(str(python), [
        str(python), "-m", "jobtracker.cli", "serve",
        "--db", str(target / "state.db"),
        "--criteria", str(target / "criteria.yaml"),
        "--answers", str(target / "answers.yaml"),
        "--keywords", str(target / "keywords.yaml"),
        "--plugins", str(target / "plugins.yaml"),
        "--downloads", str(target / "downloads.yaml"),
        "--host", HOST, "--port", str(port),
    ], env)


def fetch(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=30) as res:
            return res.status, res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, ""
    except (OSError, urllib.error.URLError) as err:
        return 0, str(err)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_check(args: argparse.Namespace) -> None:
    target = Path(args.dir).expanduser().resolve()
    marker = target / MARKER
    deadline = time.monotonic() + args.timeout
    # Seeding happens before the marker is written, so this also waits out a slow first start.
    # On a reused folder the marker can be the last run's until serve rewrites it, so it is read
    # afresh each time, and a dead pid only counts as a failure once serve has had time to start.
    started = time.monotonic()
    while True:
        info = json.loads(marker.read_text()) if marker.exists() else None
        if info and running(info):
            break
        waited = time.monotonic() - started
        if info and not alive(info["pid"]) and waited > GRACE_SECONDS:
            die(f"the server for {target} is not running. Read its output.")
        if time.monotonic() > deadline:
            die(f"no preview answered for {target} after {args.timeout}s. Read the server's output.")
        time.sleep(0.25)
    url = info["url"]

    failed = False
    pages = {}
    for route in ROUTES:
        status, body = fetch(url + route)
        pages[route] = body
        failed |= status != 200
        print(f"{status}  {route}")

    # The posting page takes a posting, so read a real link off the dashboard.
    link = re.search(r'href="(/posting\?[^"]+)"', pages["/"])
    if link:
        route = html.unescape(link.group(1))
        status, _ = fetch(url + route)
        failed |= status != 200
        print(f"{status}  {route}")
    else:
        print("  -  /posting (no posting link on the dashboard to follow)")

    picks = pages["/"].count('class="pick"')
    applications = len(re.findall(r'<article class="app\b', pages["/applications"]))
    print(f"\ntoday's picks: {picks}, applications: {applications}")

    root = Path(info["root"])
    changed = [name for name, digest in fingerprint(root).items()
               if digest != info["real_files"].get(name)]
    if changed:
        failed = True
        print(f"REAL FILES CHANGED since the preview started: {', '.join(changed)}")
        print("Something other than the preview may have written them; check before trusting it.")
    else:
        print(f"real files unchanged: {len(WATCHED)} checked")

    print(f"\nurl:    {url}\nbranch: {info['branch']} at {info['commit']}\ndata:   {info['dir']}"
          f"\nstop:   kill {info['pid']}")
    raise SystemExit(1 if failed else 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="start the preview; runs until stopped")
    serve.add_argument("--dir", default=str(DEFAULT_DIR), help="where the private copies live")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help="first port to try")
    serve.add_argument("--db", help="database to copy (default: data/state.db)")
    serve.add_argument("--no-seed", action="store_true", help="show the copied data as it is")
    serve.set_defaults(func=cmd_serve)
    check = sub.add_parser("check", help="smoke-test a running preview")
    check.add_argument("--dir", default=str(DEFAULT_DIR))
    check.add_argument("--timeout", type=int, default=60)
    check.set_defaults(func=cmd_check)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
