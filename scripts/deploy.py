"""
deploy.py -- push src/lego_pi/ and src/lego_control/ to the Pi and
(re)start the relay.

Both packages are uploaded because lego_pi imports lego_control -- on
the Pi they need to land as sibling directories under the home dir, not
nested under a src/ folder (that nesting is a PC-repo-organization
choice, not something the Pi's import path needs to know about).

Usage:
    python3 scripts/deploy.py

Reads credentials from environment variables (set once per machine):
    LEGOBOT_PI_HOST   e.g. legobot.local or 192.168.4.89
    LEGOBOT_PI_USER   e.g. pi
    LEGOBOT_PI_PASS

Requires: paramiko   (pip install paramiko)

Also installs Pi-side dependencies every deploy (lego_pi/requirements.txt,
via pip --break-system-packages) and syncs the "legobot-relay" systemd
unit file every single deploy -- uploads robot_files/legobot-relay.service,
re-copies it into /etc/systemd/system/, daemon-reload, enable, then
restart. Both steps are unconditional on purpose: an install-once
version of either seemed like a reasonable optimization, but it meant a
missing dependency or a wrong/outdated unit file on the Pi could only be
fixed by SSHing in and doing it by hand. Re-running a pip install or
re-syncing a few lines of text on every deploy costs nothing; requiring
a manual fix step when either changes costs a support conversation.
"""

import os
import sys
from pathlib import Path

import paramiko

_REPO_ROOT = Path(__file__).parent.parent
LOCAL_PACKAGES = ["lego_pi", "lego_control"]
SERVICE_NAME = "legobot-relay"
SERVICE_FILE = f"{SERVICE_NAME}.service"
LOCAL_SERVICE_PATH = _REPO_ROOT / "robot_files" / SERVICE_FILE
SKIP_DIRS = {"__pycache__"}


def get_credentials():
    host = os.environ.get("LEGOBOT_PI_HOST")
    user = os.environ.get("LEGOBOT_PI_USER")
    password = os.environ.get("LEGOBOT_PI_PASS")
    missing = [name for name, val in [
        ("LEGOBOT_PI_HOST", host),
        ("LEGOBOT_PI_USER", user),
        ("LEGOBOT_PI_PASS", password),
    ] if not val]
    if missing:
        print(f"Missing environment variable(s): {', '.join(missing)}")
        print("Set them, then open a NEW terminal before running this script.")
        sys.exit(1)
    return host, user, password


def upload_directory(sftp: paramiko.SFTPClient, local_dir: Path, remote_dir: str):
    try:
        sftp.mkdir(remote_dir)
    except IOError:
        pass  # already exists -- fine

    for item in sorted(local_dir.iterdir()):
        if item.name in SKIP_DIRS:
            continue
        remote_path = f"{remote_dir}/{item.name}"
        if item.is_dir():
            upload_directory(sftp, item, remote_path)
        else:
            print(f"  uploading {item.relative_to(local_dir.parent.parent)}")
            sftp.put(str(item), remote_path)


def run_remote_command(client: paramiko.SSHClient, command: str, password: str, sudo: bool = False):
    """No pty here on purpose: `sudo -S` reads the password from stdin
    just fine without one, and a pty echoes that stdin back into the
    captured output -- which is why the password used to show up
    (twice) in deploy.py's own printed output."""
    full_cmd = f"sudo -S {command}" if sudo else command
    stdin, stdout, stderr = client.exec_command(full_cmd, get_pty=False)
    if sudo:
        stdin.write(f"{password}\n")
        stdin.flush()
    exit_status = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="ignore")
    err = stderr.read().decode(errors="ignore")
    return exit_status, out, err


def sync_service(client: paramiko.SSHClient, sftp: paramiko.SFTPClient, password: str):
    """Unconditionally re-push the unit file and reload/enable it. Cheap
    (a handful of sudo one-liners), and it's what makes a WorkingDirectory
    or User fix in the repo actually take effect on the next --deploy
    instead of requiring a manual scp + systemctl dance."""
    if not LOCAL_SERVICE_PATH.is_file():
        print(f"Can't find {LOCAL_SERVICE_PATH} -- skipping service sync.")
        print("Start the relay manually on the Pi with: python3 -m lego_pi")
        return False

    print(f"Syncing {SERVICE_NAME} systemd unit...")

    # Read and re-encode as strict LF before upload. A Windows git
    # checkout (core.autocrlf=true) can silently turn this into CRLF
    # locally -- systemd then sees "WorkingDirectory=/home/legobot\r"
    # and CHDIRs into a path that, byte-for-byte, doesn't exist. Same
    # symptom as a genuinely wrong path, much harder to spot by eye.
    raw = LOCAL_SERVICE_PATH.read_bytes()
    normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    remote_tmp = SERVICE_FILE  # lands in the Pi user's home dir first
    with sftp.open(remote_tmp, "wb") as f:
        f.write(normalized)

    steps = [
        f"cp {remote_tmp} /etc/systemd/system/{SERVICE_FILE}",
        "systemctl daemon-reload",
        f"systemctl enable {SERVICE_NAME}",
    ]
    for step in steps:
        status, out, err = run_remote_command(client, step, password, sudo=True)
        if status != 0:
            print(f"  FAILED: {step}\n{err}")
            sys.exit(1)
    print(f"  {SERVICE_NAME} unit synced and enabled on boot.")
    return True


def restart_service(client: paramiko.SSHClient, password: str):
    print(f"Restarting {SERVICE_NAME} service...")
    status, out, err = run_remote_command(
        client, f"systemctl restart {SERVICE_NAME}", password, sudo=True
    )
    if status != 0:
        print(f"Restart FAILED (exit {status}):\n{err}")
        sys.exit(1)
    status, out, err = run_remote_command(
        client, f"systemctl is-active {SERVICE_NAME}", password, sudo=True
    )
    print(f"Service state: {out.strip() or err.strip()}")


def _ensure_pip(client: paramiko.SSHClient, password: str):
    """Some Raspberry Pi OS images don't ship python3-pip at all -- pip
    install then fails with 'No module named pip' before it even gets to
    resolving packages. Check first and only pay the apt-get cost
    (~10-30s) the one time it's actually missing."""
    status, _, _ = run_remote_command(client, "python3 -m pip --version", password)
    if status == 0:
        return

    print("  pip not found on the Pi -- installing python3-pip via apt (one-time)...")
    # Wrapped in `bash -c '...'` because `sudo -S cmd1 && cmd2` only runs
    # cmd1 as root -- the shell parses `&&` before sudo ever sees it, so
    # cmd2 would silently run unprivileged and fail.
    cmd = (
        "bash -c "
        "'DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-pip'"
    )
    status, out, err = run_remote_command(client, cmd, password, sudo=True)
    if status != 0:
        print(f"  apt-get install python3-pip FAILED (exit {status}):\n{err or out}")
        sys.exit(1)
    print("  python3-pip installed.")


def _ensure_portaudio(client: paramiko.SSHClient, password: str):
    """sounddevice (in requirements.txt, for media_relay.py) is only a
    Python wrapper -- pip installing it does NOT pull in libportaudio2,
    the native shared library it binds to via ctypes. On Linux that has
    to come from apt; the Windows/Mac wheels happen to bundle their own
    copy, which is why this gap doesn't show up testing on a PC. Without
    it, `import sounddevice` raises OSError at process startup, which
    crash-loops the whole relay service (both apps, since __main__.py
    imports media_relay before either server binds) even though nothing
    about robot control itself is broken. Same check-first pattern as
    _ensure_pip: only pay the apt-get cost the one time it's missing.

    Checks via `dpkg -s`, not `ldconfig -p` -- ldconfig lives in /sbin,
    which typically isn't on a non-root SSH user's PATH (only root's).
    Running it unqualified over paramiko's exec_command silently hit
    "command not found", produced empty output, and made this check
    report "not installed" on every single deploy regardless of whether
    it actually was -- the install itself was sticking fine, only the
    check couldn't see it. dpkg is on PATH for every user by default and
    is the actual source of truth for package state anyway, not a
    downstream side effect of it."""
    status, _, _ = run_remote_command(
        client, "dpkg -s libportaudio2", password
    )
    if status == 0:
        return

    print("  libportaudio2 not found on the Pi -- installing via apt (one-time)...")
    cmd = (
        "bash -c "
        "'DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libportaudio2'"
    )
    status, out, err = run_remote_command(client, cmd, password, sudo=True)
    if status != 0:
        print(f"  apt-get install libportaudio2 FAILED (exit {status}):\n{err or out}")
        sys.exit(1)
    print("  libportaudio2 installed.")


def install_dependencies(client: paramiko.SSHClient, password: str):
    """Installs whatever's in lego_pi/requirements.txt on the Pi. Runs
    every deploy, same reasoning as sync_service: an idempotent pip
    install costs a few seconds when nothing changed, versus a manual
    SSH-and-pip-install session the one time a new dependency gets added
    and nobody remembers this step exists.

    --break-system-packages: modern Raspberry Pi OS enforces PEP 668 and
    refuses a bare `pip install` outside a venv. The relay runs as root
    via systemd, so this installs straight into the same system Python
    root's ExecStart actually uses -- no venv indirection to keep in
    sync with the unit file.
    """
    print("Installing Pi-side dependencies (lego_pi/requirements.txt)...")
    _ensure_pip(client, password)
    _ensure_portaudio(client, password)
    cmd = "python3 -m pip install --break-system-packages -q -r lego_pi/requirements.txt"
    status, out, err = run_remote_command(client, cmd, password, sudo=True)
    if status != 0:
        print(f"  pip install FAILED (exit {status}):\n{err or out}")
        sys.exit(1)
    print("  dependencies OK.")


def main():
    host, user, password = get_credentials()

    src_dir = _REPO_ROOT / "src"
    for pkg in LOCAL_PACKAGES:
        if not (src_dir / pkg).is_dir():
            print(f"Can't find local package folder: {src_dir / pkg}")
            sys.exit(1)

    print(f"Connecting to {host} as {user}...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username=user, password=password, timeout=10)

    sftp = client.open_sftp()
    for pkg in LOCAL_PACKAGES:
        print(f"Uploading src/{pkg}/ -> ~/{pkg}/ ...")
        upload_directory(sftp, src_dir / pkg, pkg)
    print("Upload complete.")

    install_dependencies(client, password)

    service_synced = sync_service(client, sftp, password)
    if not service_synced:
        print("Skipping restart -- no unit file to restart against.")
        sftp.close()
        client.close()
        print("Done (packages uploaded, service unmanaged).")
        return

    restart_service(client, password)

    sftp.close()
    client.close()
    print("Done.")


if __name__ == "__main__":
    main()
