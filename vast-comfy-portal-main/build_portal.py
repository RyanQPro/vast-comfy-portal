"""Build dist/instance-portal.tar.gz: the Vast Instance Portal with the Models page.

The archive matches the official release asset (a top-level ``portal-aio/``), so the
image's ``update-portal`` installs it on first boot.

    python build_portal.py                    # version from portal-aio/VERSION
    python build_portal.py v3.1.8-models.2

Works the same on Windows: line endings are normalised to LF and file modes are
taken from git, since a Windows checkout may have neither.
"""

import io
import subprocess
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE
SRC = REPO / "portal-aio"
OUT = HERE / "dist" / "instance-portal.tar.gz"

SKIP_DIRS = {"__pycache__", ".pytest_cache", "tests", "venv"}
BINARY_SUFFIXES = {".png", ".ico", ".jpg", ".woff", ".woff2"}
REQUIRED = [
    "portal-aio/VERSION", "portal-aio/requirements.txt", "portal-aio/portal/portal.py",
    "portal-aio/portal/model_manager.py", "portal-aio/portal/static/assets/models.js",
    "portal-aio/portal/static/assets/models.css", "portal-aio/portal/templates/index.html",
]


def executables() -> set[str]:
    try:
        out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-s", "portal-aio"],
                             check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        out = ""
    if out:
        return {line.split("\t", 1)[1] for line in out.splitlines() if line.startswith("100755")}
    # Not a git checkout (e.g. a downloaded zip): scripts with a shebang are executable.
    return {"portal-aio/" + p.relative_to(SRC).as_posix() for p in files()
            if p.is_file() and p.read_bytes()[:2] == b"#!"}


def files() -> list[Path]:
    result = []
    for path in sorted(SRC.rglob("*")):
        rel = path.relative_to(SRC)
        if any(part in SKIP_DIRS for part in rel.parts) or path.suffix == ".pyc":
            continue
        result.append(path)
    return result


def build(version: str) -> None:
    exec_paths = executables()
    now = int(time.time())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(OUT, "w:gz") as tar:
        def add(name: str, data: bytes | None, mode: int) -> None:
            info = tarfile.TarInfo(name)
            info.mtime, info.uid, info.gid, info.uname, info.gname = now, 0, 0, "root", "root"
            info.mode = mode
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

        add("portal-aio", None, 0o755)
        for path in files():
            name = "portal-aio/" + path.relative_to(SRC).as_posix()
            if path.is_dir():
                add(name, None, 0o755)
                continue
            data = path.read_bytes()
            if path.suffix.lower() not in BINARY_SUFFIXES:
                data = data.replace(b"\r\n", b"\n")
            if name == "portal-aio/VERSION":
                data = f"{version}\n".encode()
            add(name, data, 0o755 if name in exec_paths else 0o644)


def check(version: str) -> int:
    with tarfile.open(OUT, "r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
        missing = [f for f in REQUIRED if f not in members]
        if missing:
            sys.exit(f"ERROR: missing {missing}")
        if any("/tests/" in n for n in members):
            sys.exit("ERROR: tests were packaged")
        if tar.extractfile("portal-aio/VERSION").read().decode().strip() != version:
            sys.exit("ERROR: VERSION mismatch")
        for name, member in members.items():
            if member.isfile() and not name.endswith(tuple(BINARY_SUFFIXES)):
                if b"\r" in tar.extractfile(member).read():
                    sys.exit(f"ERROR: {name} still has CRLF line endings")
        if members["portal-aio/launch.sh"].mode != 0o755:
            sys.exit("ERROR: launch.sh is not executable")
        return len(members)


def main() -> None:
    version = sys.argv[1] if len(sys.argv) > 1 else (SRC / "VERSION").read_text().strip()
    if not version.startswith("v"):
        version = "v" + version
    build(version)
    count = check(version)
    print(f"OK: {OUT} ({OUT.stat().st_size // 1024} KB, {count} entries, {version})")
    print(f"\nPublish it as the asset of a GitHub release tagged {version}:")
    print(f'  gh release create {version} "{OUT}" --repo <you>/<repo> --title "Portal {version}"')


if __name__ == "__main__":
    main()
