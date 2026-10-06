"""Classify shell commands before they run.

Three outcomes:
  READ_ONLY - inspects the system without changing it; may run without asking.
  CHANGE    - modifies something (or we can't prove it doesn't); needs approval.
  BLOCKED   - catastrophic or irreversible; never run by the agent.

The read-only check is deliberately conservative: anything we don't positively
recognise is treated as a change.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import Enum


class Risk(str, Enum):
    READ_ONLY = "read-only"
    CHANGE = "change"
    BLOCKED = "blocked"


@dataclass
class Verdict:
    risk: Risk
    reason: str
    needs_root: bool = False


BLOCKED_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\s+(-[a-zA-Z]*\s+)*(-[a-zA-Z]*[rR][a-zA-Z]*\s+)(-[a-zA-Z]*\s+)*(/|/\*|~|~/|\$HOME/?|/home/?|/etc/?|/usr/?|/boot/?|/var/?)(\s|$)",
     "recursive delete of a top-level or home directory"),
    (r"--no-preserve-root", "rm --no-preserve-root"),
    (r"\bmkfs(\.\w+)?\b", "formats a filesystem"),
    (r"\bdd\b.*\bof=/dev/(sd|nvme|hd|vd|mmcblk|dm-)", "raw write to a disk device"),
    (r">\s*/dev/(sd|nvme|hd|vd|mmcblk)", "redirect onto a disk device"),
    (r"\b(wipefs|shred|blkdiscard|sgdisk\s+--zap|sfdisk|fdisk|parted)\b", "partition table / disk wipe tool"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb"),
    (r"\bchmod\s+(-[a-zA-Z]*R[a-zA-Z]*\s+)\S*\s+/(\s|$)", "recursive chmod on /"),
    (r"\bchown\s+(-[a-zA-Z]*R[a-zA-Z]*\s+)\S+\s+/(\s|$)", "recursive chown on /"),
    (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b", "pipes a download straight into a shell"),
    (r"\bapt(-get)?\s+(remove|purge|autoremove)\b.*\b(linux-image|systemd|libc6|ubuntu-desktop|sudo|apt|dpkg)\b",
     "removes a core system package"),
    (r">\s*/etc/(passwd|shadow|sudoers|fstab)\b", "overwrites a critical system file"),
    (r"\buserdel\b|\bpasswd\s+-d\b", "deletes a user or their password"),
]

# Commands that only read state. Value is None (any args OK) or a validator
# taking the argv (without the command name) and returning True if read-only.
def _no_flags(*bad: str):
    return lambda args: not any(a == b or a.startswith(b + "=") for a in args for b in bad)


def _first_arg_in(*allowed: str):
    return lambda args: not args or args[0] in allowed


def _all_args_in(*allowed: str):
    # flags must be listed explicitly; positional args may only be device paths
    return lambda args: all(a in allowed or a.startswith("/dev/") for a in args)


READ_ONLY_COMMANDS: dict[str, object] = {
    # files & text
    "ls": None, "cat": None, "head": None, "tail": _no_flags("-f", "--follow", "-F"),
    "less": None, "wc": None, "stat": None, "file": None, "grep": None, "egrep": None,
    "zgrep": None, "zcat": None, "sort": _no_flags("-o", "--output"), "uniq": None,
    "cut": None, "tr": None, "column": None, "diff": None, "realpath": None, "readlink": None,
    "basename": None, "dirname": None, "md5sum": None, "sha256sum": None, "tree": None,
    "find": _no_flags("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"),
    "sed": lambda a: not any(x.startswith("-i") or x == "--in-place" or x.startswith("--in-place=") for x in a)
    and not any(re.search(r"(^|[;}\s])[wWe]\b|/[wW]\s", x) for x in a),
    "echo": None, "printf": None, "true": None, "date": None, "cal": None, "test": None,
    # identity & system
    "uname": None, "hostname": lambda a: not a or all(x.startswith("-") for x in a),
    "hostnamectl": _first_arg_in("status"), "timedatectl": _first_arg_in("status", "show", "timesync-status"),
    "localectl": _first_arg_in("status", "list-locales", "list-keymaps"), "whoami": None, "id": None,
    "groups": None, "who": None, "w": None, "last": None, "uptime": None, "lsb_release": None,
    "which": None, "whereis": None, "type": None, "command": lambda a: bool(a) and a[0] == "-v",
    "locale": None, "getent": None, "loginctl": _first_arg_in("list-sessions", "show-session", "session-status",
                                                              "list-users", "show-user", "user-status", "list-seats"),
    # processes & resources
    "ps": None, "pgrep": None, "pidof": None, "top": lambda a: "-b" in a or "-bn1" in a, "free": None,
    "vmstat": None, "iostat": None, "mpstat": None, "df": None, "du": None, "lsof": None, "nproc": None,
    "lscpu": None, "lsmem": None, "lsblk": None, "lspci": None, "lsusb": None, "lsmod": None,
    "lshw": None, "lsinitramfs": None, "modinfo": None, "findmnt": None, "blkid": None,
    "swapon": lambda a: a in (["--show"], ["-s"]), "mount": lambda a: not a or a in (["-l"],),
    "dmesg": _no_flags("-C", "--clear", "-c", "--read-clear", "-D", "--console-off", "-E", "--console-on",
                       "-n", "--console-level", "-w", "--follow"),
    "journalctl": _no_flags("--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate", "--flush",
                            "--sync", "--relinquish-var", "--smart-relinquish-var", "-f", "--follow",
                            "--setup-keys", "--update-catalog"),
    "systemctl": _first_arg_in("status", "show", "list-units", "list-unit-files", "list-timers", "list-sockets",
                               "list-dependencies", "is-active", "is-enabled", "is-failed", "cat",
                               "get-default", "--failed", "--version"),
    "systemd-analyze": _first_arg_in("blame", "critical-chain", "time", "verify", "security"),
    "coredumpctl": _first_arg_in("list", "info"),
    "sensors": None, "uptime": None, "inxi": None, "dmidecode": None, "smartctl": _all_args_in(
        "-a", "-i", "-H", "-x", "-A", "--all", "--info", "--health", "--xall", "--attributes", "--scan", "-l",
        "error", "selftest", "-c", "--json", "-j"),
    "nvme": _first_arg_in("list", "smart-log", "id-ctrl", "error-log"), "hdparm": _all_args_in("-I", "-i"),
    "upower": None, "acpi": None, "powerprofilesctl": _first_arg_in("get", "list"),
    "cpupower": _first_arg_in("frequency-info", "idle-info"), "turbostat": None,
    # graphics & display
    "nvidia-smi": lambda a: not any(x in a for x in ("-r", "--gpu-reset", "-pm", "-pl", "-c", "-e", "-ac", "-rac")),
    "glxinfo": None, "vulkaninfo": None, "vainfo": None, "xrandr": lambda a: not a or a in (["--query"], ["-q"], ["--listmonitors"]),
    "xdpyinfo": None, "wlr-randr": lambda a: not a, "prime-select": _first_arg_in("query"),
    "ubuntu-drivers": _first_arg_in("list", "devices"),
    # audio & video
    "pactl": _first_arg_in("list", "info", "stat", "get-default-sink", "get-default-source", "get-sink-volume",
                           "get-source-volume", "get-sink-mute", "get-source-mute"),
    "wpctl": _first_arg_in("status", "inspect", "get-volume"), "pw-cli": _first_arg_in("ls", "list-objects", "info"),
    "aplay": lambda a: bool(a) and a[0] in ("-l", "-L", "--list-devices", "--list-pcms"),
    "arecord": lambda a: bool(a) and a[0] in ("-l", "-L", "--list-devices", "--list-pcms"),
    "v4l2-ctl": lambda a: all(x.startswith("--list") or x.startswith("--info") or x.startswith("--all")
                              or x in ("-d", "-l", "-D", "-A") or x.startswith("/dev/") for x in a),
    "cam": lambda a: bool(a) and all(x in ("-l", "--list", "-I", "--info") for x in a),
    # network
    "ip": lambda a: not any(x in a for x in ("add", "del", "delete", "set", "flush", "change", "replace", "link-set")),
    "ss": None, "netstat": None, "ping": lambda a: "-c" in a or any(x.startswith("-c") for x in a),
    "traceroute": None, "tracepath": None, "dig": None, "nslookup": None, "host": None,
    "resolvectl": _first_arg_in("status", "query", "statistics", "dns", "domain"),
    "nmcli": lambda a: not any(x in a for x in ("up", "down", "add", "delete", "modify", "edit", "connect",
                                                 "disconnect", "reload", "load", "import", "on", "off",
                                                 "rescan", "hotspot", "set-hostname")),
    "iw": lambda a: not any(x in a for x in ("set", "connect", "disconnect", "del", "add", "scan")),
    "iwconfig": lambda a: len(a) <= 1, "rfkill": _first_arg_in("list"), "ethtool": lambda a: len(a) <= 2 and not any(
        x in a for x in ("-s", "-K", "-G", "-A", "-C", "-E", "-r")),
    "curl": lambda a: all(not x.startswith(("-o", "--output", "-O", "-T", "--upload", "-d", "--data", "-X", "-F"))
                          for x in a),
    "bluetoothctl": _first_arg_in("show", "list", "devices", "info", "paired-devices", "version"),
    "hciconfig": lambda a: len(a) <= 1,
    # packages
    "dpkg": _first_arg_in("-l", "-L", "-s", "-S", "--list", "--listfiles", "--status", "--search",
                          "--get-selections", "--print-architecture"),
    "dpkg-query": None, "apt-cache": None, "apt": _first_arg_in("list", "show", "search", "policy", "depends",
                                                                  "rdepends", "changelog"),
    "apt-mark": _first_arg_in("showhold", "showmanual", "showauto"),
    "snap": _first_arg_in("list", "info", "find", "services", "changes", "connections", "version", "logs"),
    "flatpak": _first_arg_in("list", "info", "search", "remotes", "history"),
    "rpm": lambda a: bool(a) and a[0].startswith("-q"), "dnf": _first_arg_in("list", "info", "search", "repolist",
                                                                          "provides", "history"),
    "pacman": lambda a: bool(a) and a[0].startswith("-Q"), "zypper": _first_arg_in("se", "search", "info", "lr", "repos"),
    "fwupdmgr": _first_arg_in("get-devices", "get-updates", "get-history", "security"),
    "efibootmgr": lambda a: not a or a == ["-v"], "mokutil": lambda a: bool(a) and a[0] in ("--sb-state", "--list-enrolled"),
    "gsettings": _first_arg_in("get", "list-keys", "list-schemas", "list-recursively", "range"),
    "xdg-settings": _first_arg_in("get"), "update-alternatives": lambda a: bool(a) and a[0] in ("--display", "--query", "--list"),
    "docker": _first_arg_in("ps", "images", "info", "version", "logs", "inspect", "stats"),
    "python3": lambda a: a in (["--version"], ["-V"]),
    # tux's own helpers (tux-note only appends to tux's notes file)
    "tux-scan": None, "tux-snapshot": None, "tux-note": None,
}

SHELL_SPLIT = re.compile(r"\|\||&&|;|\||&")
DANGEROUS_SYNTAX = re.compile(r"\$\(|`|<\(|>\(")
REDIRECT_OUT = re.compile(r"(^|[^0-9&<>])>{1,2}(?!&)|\d>{1,2}(?!&)|&>")


def _strip_safe_redirects(cmd: str) -> str:
    # 2>/dev/null, 2>&1 and >/dev/null are harmless
    return re.sub(r"\d?>{1,2}\s*/dev/null|\d?>&\d|&>\s*/dev/null", " ", cmd)


def classify(command: str) -> Verdict:
    cmd = command.strip()
    if not cmd:
        return Verdict(Risk.BLOCKED, "empty command")

    for pattern, reason in BLOCKED_PATTERNS:
        if re.search(pattern, cmd):
            return Verdict(Risk.BLOCKED, reason)

    needs_root = bool(re.search(r"(^|[;&|]\s*)(sudo|pkexec|doas|tux-sudo)\b", cmd))
    if needs_root:
        return Verdict(Risk.CHANGE, "runs with root privileges", needs_root=True)

    cleaned = _strip_safe_redirects(cmd)
    if DANGEROUS_SYNTAX.search(cleaned):
        return Verdict(Risk.CHANGE, "uses command substitution")
    if REDIRECT_OUT.search(cleaned):
        return Verdict(Risk.CHANGE, "writes output to a file")

    for segment in SHELL_SPLIT.split(cleaned):
        segment = segment.strip()
        if not segment:
            continue
        try:
            argv = shlex.split(segment)
        except ValueError:
            return Verdict(Risk.CHANGE, "could not parse command")
        # skip leading VAR=value assignments
        while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
            argv = argv[1:]
        if not argv:
            continue
        name = argv[0].rsplit("/", 1)[-1]
        if name not in READ_ONLY_COMMANDS:
            return Verdict(Risk.CHANGE, f"`{name}` is not on the read-only list")
        check = READ_ONLY_COMMANDS[name]
        if check is not None and not check(argv[1:]):
            return Verdict(Risk.CHANGE, f"`{name}` with these arguments may modify the system")

    return Verdict(Risk.READ_ONLY, "read-only inspection")


SENSITIVE_PATHS = [
    r"^/etc/(shadow|gshadow)", r"/\.ssh/id_[^/]*$", r"/\.ssh/.*_key$", r"/\.gnupg/", r"/\.aws/credentials",
    r"/\.netrc$", r"/\.password-store/", r"/\.local/share/keyrings/", r"/\.mozilla/.*/(logins|key\d)\.",
    r"/\.config/google-chrome/.*/(Login Data|Cookies)", r"\.env$", r"/\.config/anthropic/",
]


def is_sensitive_path(path: str) -> bool:
    return any(re.search(p, path) for p in SENSITIVE_PATHS)
