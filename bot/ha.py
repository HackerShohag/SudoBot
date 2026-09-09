"""Single-token HA transport helpers.

The standby never contacts Telegram. SSH is used both for primary heartbeats
and for explicitly server-targeted host commands.
"""

import shlex

from bot.config import (
    SERVER_SSH_HOST,
    SERVER_SSH_KEY,
    SERVER_SSH_KNOWN_HOSTS,
    SERVER_SSH_PORT,
    SERVER_SSH_USER,
)


def server_ssh_args() -> list[str]:
    """Return non-interactive SSH arguments as an argv list."""
    if not SERVER_SSH_HOST or not SERVER_SSH_USER:
        raise RuntimeError(
            "SERVER_SSH_HOST and SERVER_SSH_USER must be configured"
        )
    if not 1 <= SERVER_SSH_PORT <= 65535:
        raise RuntimeError("SERVER_SSH_PORT must be between 1 and 65535")

    args = [
        "ssh",
        "-T",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=2",
        "-o", "StrictHostKeyChecking=yes",
        "-p", str(SERVER_SSH_PORT),
    ]
    if SERVER_SSH_KEY:
        args.extend(("-o", "IdentitiesOnly=yes", "-i", SERVER_SSH_KEY))
    if SERVER_SSH_KNOWN_HOSTS:
        args.extend(
            (
                "-o", f"UserKnownHostsFile={SERVER_SSH_KNOWN_HOSTS}",
            )
        )
    args.append(f"{SERVER_SSH_USER}@{SERVER_SSH_HOST}")
    return args


def server_command(command: str) -> str:
    """Build a shell-safe local command that runs *command* over SSH."""
    remote_command = f"sh -lc {shlex.quote(command)}"
    return shlex.join([*server_ssh_args(), remote_command])
