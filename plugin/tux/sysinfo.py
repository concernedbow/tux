"""Local system inspection: a startup snapshot plus targeted hardware scans."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path


def sh(cmd: str, timeout: int = 15) -> str:
    """Run a read-only command, returning combined output (never raises)."""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "LC_ALL": "C"}, stdin=subprocess.DEVNULL)
        out = (p.stdout + p.stderr).strip()
        return out if out else f"(no output, exit {p.returncode})"
    except subprocess.TimeoutExpired:
        return f"(timed out after {timeout}s)"
    except Exception as e:  # noqa: BLE001
        return f"(failed: {e})"


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _os_release() -> dict[str, str]:
    data = {}
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            for line in Path(path).read_text().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    data[k] = v.strip().strip('"')
            break
        except OSError:
            continue
    return data


def package_manager() -> str:
    for pm in ("apt", "dnf", "pacman", "zypper", "apk", "xbps-install", "emerge", "nix-env"):
        if have(pm):
            return pm
    return "unknown"


def snapshot() -> str:
    """A compact description of this machine, computed once per session."""
    osr = _os_release()
    cpu = sh("lscpu | sed -n 's/^Model name:[[:space:]]*//p' | head -1")
    mem = sh("free -h | awk '/Mem:/ {print $2\" total, \"$7\" available\"}'")
    lines = [
        f"Distro: {osr.get('PRETTY_NAME', 'unknown')} (id={osr.get('ID', '?')}, like={osr.get('ID_LIKE', '-')})",
        f"Kernel: {platform.release()} ({platform.machine()})",
        f"Package manager: {package_manager()}"
        + (" + snap" if have("snap") else "") + (" + flatpak" if have("flatpak") else ""),
        f"Init: {'systemd' if Path('/run/systemd/system').exists() else 'non-systemd'}",
        f"Desktop: {os.environ.get('XDG_CURRENT_DESKTOP', 'none')} "
        f"session={os.environ.get('XDG_SESSION_TYPE', 'tty')}",
        f"CPU: {cpu} ({os.cpu_count()} threads)",
        f"Memory: {mem}",
        f"Hardware: {_read('/sys/class/dmi/id/sys_vendor')} {_read('/sys/class/dmi/id/product_name')}".strip(),
        f"User: {os.environ.get('USER', '?')} (sudo group: {'yes' if _in_sudo_group() else 'no'})",
    ]
    if have("lspci"):
        gpu = sh("lspci | grep -Ei 'vga|3d|display' | cut -d: -f3- | sed 's/^ //'")
        lines.append("GPU: " + "; ".join(gpu.splitlines()))
    lines.append("Root disk: " + sh("df -h / | awk 'NR==2 {print $3\" used of \"$2\" (\"$5\")\"}'"))
    return "\n".join(lines)


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def _in_sudo_group() -> bool:
    try:
        import grp
        names = {grp.getgrgid(g).gr_name for g in os.getgroups()}
        return bool(names & {"sudo", "wheel", "admin"})
    except Exception:  # noqa: BLE001
        return False


# Each area maps to a list of (label, command, required_binary or None).
SCANS: dict[str, list[tuple[str, str, str | None]]] = {
    "overview": [
        ("uptime/load", "uptime", None),
        ("failed units", "systemctl --failed --no-legend --no-pager", "systemctl"),
        ("recent errors", "journalctl -p 3 -b --no-pager -n 40 -q", "journalctl"),
        ("kernel warnings", "dmesg --level=err,warn 2>&1 | tail -30", None),
        ("disk usage", "df -h -x tmpfs -x devtmpfs -x squashfs -x overlay", None),
        ("memory", "free -h", None),
        ("top cpu", "ps -eo pid,comm,%cpu,%mem --sort=-%cpu | head -8", None),
    ],
    "cpu": [
        ("lscpu", "lscpu", "lscpu"),
        ("frequencies", "grep MHz /proc/cpuinfo | sort | uniq -c | head", None),
        ("governor", "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null", None),
        ("power profile", "powerprofilesctl get", "powerprofilesctl"),
        ("thermal", "sensors", "sensors"),
        ("throttling", "journalctl -k -b --no-pager -q | grep -i -E 'thrott|mce|machine check' | tail -20", "journalctl"),
    ],
    "memory": [
        ("free", "free -h", None),
        ("swap", "swapon --show", None),
        ("oom kills", "journalctl -k --no-pager -q | grep -i -E 'out of memory|oom-kill' | tail -15", "journalctl"),
        ("top memory", "ps -eo pid,comm,%mem,rss --sort=-%mem | head -10", None),
        ("edac errors", "grep -r . /sys/devices/system/edac/mc/*/ce_count 2>/dev/null", None),
    ],
    "storage": [
        ("block devices", "lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL", "lsblk"),
        ("disk usage", "df -h -x tmpfs -x devtmpfs -x squashfs -x overlay", None),
        ("inodes", "df -i -x tmpfs -x devtmpfs -x squashfs -x overlay | awk 'NR==1 || $5+0 > 70'", None),
        ("nvme health", "for d in /dev/nvme?; do [ -e $d ] && smartctl -H $d 2>&1 | tail -3; done", "smartctl"),
        ("sata health", "for d in /dev/sd?; do [ -e $d ] && smartctl -H $d 2>&1 | tail -3; done", "smartctl"),
        ("io errors", "journalctl -k -b --no-pager -q | grep -i -E 'i/o error|ata.*error|nvme.*(error|timeout)|ext4-fs error|btrfs.*error' | tail -20", "journalctl"),
        ("largest in home", "du -xh --max-depth=1 ~ 2>/dev/null | sort -rh | head -10", None),
    ],
    "gpu": [
        ("devices", "lspci -k | grep -A3 -Ei 'vga|3d|display'", "lspci"),
        ("nvidia", "nvidia-smi", "nvidia-smi"),
        ("opengl", "glxinfo -B", "glxinfo"),
        ("vaapi", "vainfo", "vainfo"),
        ("drm errors", "journalctl -k -b --no-pager -q | grep -i -E 'drm|i915|amdgpu|nouveau|nvidia' | grep -i -E 'error|fail|hang|timeout' | tail -20", "journalctl"),
        ("drivers available", "ubuntu-drivers devices", "ubuntu-drivers"),
    ],
    "network": [
        ("interfaces", "ip -br addr", "ip"),
        ("routes", "ip route", "ip"),
        ("dns", "resolvectl status 2>/dev/null | head -30 || cat /etc/resolv.conf", None),
        ("networkmanager", "nmcli general status && nmcli device status", "nmcli"),
        ("wifi", "nmcli -f IN-USE,SSID,SIGNAL,SECURITY device wifi list 2>/dev/null | head -10", "nmcli"),
        ("rfkill", "rfkill list", "rfkill"),
        # contacts outside services (declared in PRIVACY.md): 2 pings to Cloudflare, 1 DNS lookup
        ("connectivity (pings 1.1.1.1, looks up example.com)",
         "ping -c 2 -W 2 1.1.1.1 2>&1 | tail -2; getent hosts example.com || echo 'DNS lookup failed'", None),
        ("network drivers", "lspci -k | grep -A3 -i -E 'network|ethernet'", "lspci"),
        ("errors", "journalctl -b --no-pager -q -u NetworkManager -p 4 -n 20", "journalctl"),
    ],
    "audio": [
        ("server", "pactl info 2>&1 | grep -E 'Server Name|Default S'", "pactl"),
        ("pipewire", "wpctl status", "wpctl"),
        ("alsa cards", "aplay -l", "aplay"),
        ("capture devices", "arecord -l", "arecord"),
        ("services", "systemctl --user --no-pager status pipewire pipewire-pulse wireplumber 2>&1 | grep -E '●|Active'", "systemctl"),
        ("errors", "journalctl --user -b --no-pager -q -p 4 -n 20 -u pipewire -u wireplumber", "journalctl"),
        ("sof/hda", "journalctl -k -b --no-pager -q | grep -i -E 'sof|snd_hda|hda-intel' | tail -15", "journalctl"),
    ],
    "camera": [
        ("video devices", "v4l2-ctl --list-devices", "v4l2-ctl"),
        ("dev nodes", "ls -l /dev/video* /dev/media* 2>&1", None),
        ("libcamera", "cam -l", "cam"),
        ("usb cameras", "lsusb | grep -i -E 'camera|webcam|video'", "lsusb"),
        ("ipu/uvc kernel", "journalctl -k -b --no-pager -q | grep -i -E 'uvc|ipu|ov[0-9]|camera' | tail -20", "journalctl"),
        ("in use by", "lsof /dev/video* 2>/dev/null | head", "lsof"),
    ],
    "usb": [
        ("tree", "lsusb -t", "lsusb"),
        ("devices", "lsusb", "lsusb"),
        ("recent events", "journalctl -k -b --no-pager -q | grep -i usb | tail -25", "journalctl"),
    ],
    "bluetooth": [
        ("controller", "bluetoothctl show", "bluetoothctl"),
        ("devices", "bluetoothctl devices", "bluetoothctl"),
        ("service", "systemctl --no-pager status bluetooth 2>&1 | head -6", "systemctl"),
        ("rfkill", "rfkill list bluetooth", "rfkill"),
        ("errors", "journalctl -b --no-pager -q -u bluetooth -n 20", "journalctl"),
    ],
    "battery": [
        ("upower", "upower -i $(upower -e | grep -m1 BAT) 2>/dev/null", "upower"),
        ("sysfs", "for b in /sys/class/power_supply/BAT*; do echo $b; cat $b/{status,capacity,energy_full,energy_full_design,cycle_count} 2>/dev/null; done", None),
        ("power profile", "powerprofilesctl get", "powerprofilesctl"),
        ("tlp", "tlp-stat -s", "tlp-stat"),
    ],
    "display": [
        ("session", "echo $XDG_SESSION_TYPE $XDG_CURRENT_DESKTOP", None),
        ("xrandr", "xrandr --query", "xrandr"),
        ("connectors", "for c in /sys/class/drm/card*-*; do echo \"$(basename $c): $(cat $c/status)\"; done", None),
        ("errors", "journalctl -b --no-pager -q -p 3 | grep -i -E 'gnome-shell|kwin|mutter|xorg|wayland' | tail -15", "journalctl"),
    ],
    "boot": [
        ("boot time", "systemd-analyze", "systemd-analyze"),
        ("slowest units", "systemd-analyze blame --no-pager | head -15", "systemd-analyze"),
        ("failed units", "systemctl --failed --no-legend --no-pager", "systemctl"),
        ("previous boot errors", "journalctl -b -1 -p 3 --no-pager -q -n 30", "journalctl"),
        ("secure boot", "mokutil --sb-state", "mokutil"),
    ],
    "packages": [],  # built at scan time by _package_scan()
}


def _package_scan() -> list[tuple[str, str, str | None]]:
    pm = package_manager()
    rows: list[tuple[str, str, str | None]] = [("package manager", f"echo {pm}", None)]
    # dnf/pacman/zypper update checks refresh metadata from the distro's mirrors (declared in PRIVACY.md);
    # apt only reads its local cache
    if pm == "apt":
        rows += [
            ("broken/half-installed", "dpkg -l | grep -v -E '^(ii|rc|hi)' | tail -n +6 | head -20", None),
            ("held", "apt-mark showhold", None),
            ("upgradable", "apt list --upgradable 2>/dev/null | head -25", None),
            ("dpkg/apt locks", "lsof /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock 2>/dev/null", "lsof"),
        ]
    elif pm == "dnf":
        rows += [("upgradable (contacts your mirrors)", "dnf -q check-update | head -25", None),
                 ("history", "dnf history | head -10", None)]
    elif pm == "pacman":
        rows += [("upgradable (contacts your mirrors)", "checkupdates 2>/dev/null | head -25", None),
                 ("orphans", "pacman -Qdtq | head", None)]
    elif pm == "zypper":
        rows += [("upgradable (contacts your mirrors)", "zypper -q lu | head -25", None)]
    if have("snap"):
        rows.append(("snaps", "snap list", None))
    if have("flatpak"):
        rows.append(("flatpaks", "flatpak list --columns=application,version", None))
    return rows


def scan(area: str) -> str:
    rows = _package_scan() if area == "packages" else SCANS.get(area)
    if rows is None:
        return f"Unknown area '{area}'. Choose from: {', '.join(SCANS)}"
    out = []
    for label, cmd, binary in rows:
        if binary and not have(binary):
            out.append(f"### {label}\n(skipped: `{binary}` not installed)")
            continue
        out.append(f"### {label}\n$ {cmd}\n{sh(cmd)}")
    return "\n\n".join(out)
