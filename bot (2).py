import discord
from discord.ext import commands, tasks
import asyncio
import subprocess
import json
import re
import os
import hashlib
import random
import string
import shlex
import shutil
import logging
import threading
import time
from datetime import datetime
from collections import deque
from typing import Optional, List, Dict, Any

# ─── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('vps_bot')

# ─── Docker availability check ────────────────────────────────────────────────

if not shutil.which("docker"):
    logger.error("Docker command not found. Please ensure Docker is installed.")
    raise SystemExit("Docker command not found. Please ensure Docker is installed.")

# ─── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# ─── Core constants ─────────────────────────────────────────────────────────────

MAIN_ADMIN_ID   = 1251119503492775956
VPS_USER_ROLE_ID = 1431499643698544720
DOCKER_IMAGE    = "darknodes-vps"
DOCKERFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Dockerfile")
_IMAGE_HASH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".darknodes_image_hash")
SSH_PORT_START  = 10000
# Server public IP used in direct SSH connection strings.
# Override by setting the SERVER_IP environment variable on your host.
SERVER_IP = os.environ.get("SERVER_IP", "")

# Base hostname prefix.  Each container gets DarkNodes-N (e.g. DarkNodes-1).
VPS_HOSTNAME = "DarkNodes"

def get_next_vps_hostname() -> str:
    """Return a unique hostname like DarkNodes-1, DarkNodes-2, … for a new container."""
    total = sum(len(v) for v in vps_data.values())
    return f"{VPS_HOSTNAME}-{total + 1}"

# ─── Mining/monitoring config ─────────────────────────────────────────────────
# Stored in mining_config.json so admins can tune without touching source code.

DEFAULT_MINING_CONFIG = {
    # CPU must stay above this % for sustained_duration_minutes before counting
    "cpu_threshold": 95,
    # Minutes of sustained high CPU required before adding CPU indicator score
    "sustained_duration_minutes": 20,
    # Confidence score needed to trigger automatic suspension (0-100)
    "auto_suspend_threshold": 70,
    # Seconds between per-container checks
    "monitoring_interval": 120,
    # Toggle automatic suspension on/off (admins can disable globally)
    "auto_suspend_enabled": True,
    # Discord channel ID for alert embeds (0 = disabled)
    "notification_channel_id": 0,
    # Known mining process names (case-insensitive substring match)
    "mining_process_blacklist": [
        "xmrig", "minerd", "cpuminer", "bfgminer", "cgminer",
        "ethminer", "nbminer", "t-rex", "teamredminer", "gminer",
        "lolminer", "phoenixminer", "claymore", "nanominer",
        "kawpowminer", "wildrig", "srbminer", "bminer", "ccminer",
        "sgminer", "excavator", "multiminer", "nicehash"
    ],
    # Known mining pool domain substrings
    "mining_pool_blacklist": [
        "minexmr.com", "supportxmr.com", "xmrpool.eu",
        "nanopool.org", "nicehash.com", "2miners.com",
        "ethermine.org", "flexpool.io", "pool.hashvault.pro",
        "gulf.moneroocean.stream", "xmr.pool.minergate.com",
        "miningpoolhub.com", "antpool.com", "f2pool.com",
        "slushpool.com", "viawallet.com", "hiveon.net"
    ],
    # Suspicious CLI argument fragments
    "mining_cli_blacklist": [
        "stratum+tcp://", "stratum+ssl://", "stratum2+tcp://",
        "--donate-level", "--coin xmr", "--algo rx/", "--algo kawpow",
        "-o pool", "--url pool", "--pool-address", "--mining-address"
    ],
    # Users whose VPS containers are never auto-suspended (user IDs as strings)
    "whitelisted_users": [],
    # Specific container names that are never auto-suspended
    "whitelisted_containers": [],
    # Process names that are always safe (never add mining confidence even if detected)
    "whitelisted_processes": [
        "node", "npm", "yarn", "python3", "python", "pip", "pip3",
        "java", "javac", "cargo", "go", "rustc", "gradle", "mvn",
        "apt", "apt-get", "dpkg", "make", "cmake", "gcc", "g++",
        "clang", "ninja", "bazel", "docker", "bash", "sh", "curl", "wget"
    ]
}

def load_mining_config():
    try:
        with open('mining_config.json', 'r') as f:
            loaded = json.load(f)
            # Merge with defaults so new keys always exist
            cfg = dict(DEFAULT_MINING_CONFIG)
            cfg.update(loaded)
            return cfg
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("mining_config.json not found, using defaults")
        return dict(DEFAULT_MINING_CONFIG)

def save_mining_config():
    try:
        with open('mining_config.json', 'w') as f:
            json.dump(MONITOR_CONFIG, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving mining config: {e}")

MONITOR_CONFIG = load_mining_config()

# ─── Data storage ─────────────────────────────────────────────────────────────

def load_data():
    try:
        with open('user_data.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("user_data.json not found or corrupted, initializing empty data")
        return {}

def load_vps_data():
    try:
        with open('vps_data.json', 'r') as f:
            loaded = json.load(f)
            vps_data = {}
            for uid, v in loaded.items():
                if isinstance(v, dict):
                    if "container_name" in v:
                        vps_data[uid] = [v]
                    else:
                        vps_data[uid] = list(v.values())
                elif isinstance(v, list):
                    vps_data[uid] = v
                else:
                    logger.warning(f"Unknown VPS data format for user {uid}, skipping")
                    continue
            return vps_data
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("vps_data.json not found or corrupted, initializing empty data")
        return {}

def load_admin_data():
    try:
        with open('admin_data.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("admin_data.json not found or corrupted, initializing with main admin")
        return {"admins": [str(MAIN_ADMIN_ID)]}

def load_suspension_log():
    try:
        with open('suspension_log.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def load_monitor_log():
    try:
        with open('monitor_log.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

user_data       = load_data()
vps_data        = load_vps_data()
admin_data      = load_admin_data()
suspension_log  = load_suspension_log()   # List of suspension event dicts
monitor_log     = load_monitor_log()      # {container_name: [event, ...]}

def save_data():
    try:
        with open('user_data.json', 'w') as f:
            json.dump(user_data, f, indent=4)
        with open('vps_data.json', 'w') as f:
            json.dump(vps_data, f, indent=4)
        with open('admin_data.json', 'w') as f:
            json.dump(admin_data, f, indent=4)
        logger.info("Data saved successfully")
    except Exception as e:
        logger.error(f"Error saving data: {e}")

def save_suspension_log():
    try:
        with open('suspension_log.json', 'w') as f:
            json.dump(suspension_log, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving suspension log: {e}")

def save_monitor_log():
    try:
        with open('monitor_log.json', 'w') as f:
            json.dump(monitor_log, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving monitor log: {e}")

# ─── In-memory per-container monitor state ────────────────────────────────────
# Tracks CPU history and detection flags per container between scans.
# Not persisted — resets on bot restart (intentional; evidence is in suspension_log).

container_monitor_state: Dict[str, Dict] = {}

def get_container_state(container_name: str) -> Dict:
    """Return (creating if missing) the in-memory monitor state for a container."""
    if container_name not in container_monitor_state:
        container_monitor_state[container_name] = {
            "high_cpu_start": None,        # datetime when sustained high CPU began
            "cpu_samples":    deque(maxlen=30),  # recent CPU % readings
            "net_tx_samples": deque(maxlen=10),  # recent net transmit bytes
            "flags":          set(),        # current detection flags
            "last_confidence": 0,
            "monitoring_paused": False,     # admin can pause per-container
        }
    return container_monitor_state[container_name]

# ─── Helper functions ──────────────────────────────────────────────────────────

def get_next_ssh_port():
    """Get the next available SSH port for a new container."""
    used_ports = set()
    for vps_list in vps_data.values():
        for vps in vps_list:
            if "ssh_port" in vps:
                used_ports.add(vps["ssh_port"])
    port = SSH_PORT_START
    while port in used_ports:
        port += 1
    return port

def generate_password(length=16):
    """Generate a random strong password."""
    chars = string.ascii_letters + string.digits + "!@#$%"
    return ''.join(random.choice(chars) for _ in range(length))

def find_vps_record(container_name: str):
    """Return (owner_user_id, vps_dict) for a container name, or (None, None)."""
    for uid, vps_list in vps_data.items():
        for vps in vps_list:
            if vps.get("container_name") == container_name:
                return uid, vps
    return None, None

# ─── Admin checks ─────────────────────────────────────────────────────────────

def is_admin():
    async def predicate(ctx):
        user_id = str(ctx.author.id)
        if user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", []):
            return True
        await ctx.send(embed=create_error_embed("Access Denied", "You don't have permission to use this command."))
        return False
    return commands.check(predicate)

def is_main_admin():
    async def predicate(ctx):
        if str(ctx.author.id) == str(MAIN_ADMIN_ID):
            return True
        await ctx.send(embed=create_error_embed("Access Denied", "Only the main admin can use this command."))
        return False
    return commands.check(predicate)

# ─── Embed helpers ─────────────────────────────────────────────────────────────

def create_embed(title, description="", color=0x1a1a1a, fields=None):
    embed = discord.Embed(title=f"▌ {title}", description=description, color=color)
    embed.set_thumbnail(url="")
    if fields:
        for field in fields:
            embed.add_field(name=f"▸ {field['name']}", value=field["value"], inline=field.get("inline", False))
    embed.set_footer(
        text=f"DarkNodes | VPS Manager • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        icon_url=""
    )
    return embed

def create_success_embed(title, description=""):
    return create_embed(title, description, color=0x00ff88)

def create_error_embed(title, description=""):
    return create_embed(title, description, color=0xff3366)

def create_info_embed(title, description=""):
    return create_embed(title, description, color=0x00ccff)

def create_warning_embed(title, description=""):
    return create_embed(title, description, color=0xffaa00)

# ─── Docker execution helpers ─────────────────────────────────────────────────

async def execute_docker(command, timeout=120):
    """Execute a Docker CLI command with timeout and structured error handling."""
    try:
        cmd = shlex.split(command)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            error = stderr.decode().strip() if stderr else "Command failed with no error output"
            raise Exception(error)
        return stdout.decode().strip() if stdout else True
    except asyncio.TimeoutError:
        logger.error(f"Docker command timed out: {command}")
        raise Exception(f"Command timed out after {timeout} seconds")
    except Exception as e:
        logger.error(f"Docker Error: {command} - {str(e)}")
        raise

async def docker_exec(container_name, command, timeout=60):
    """Execute a shell command inside a running Docker container."""
    cmd = ["docker", "exec", container_name, "bash", "-c", command]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return stdout.decode().strip(), stderr.decode().strip(), proc.returncode

# ─── Image management ─────────────────────────────────────────────────────────

def _dockerfile_hash() -> str:
    """Return MD5 of the Dockerfile, or '' if it cannot be read."""
    try:
        with open(DOCKERFILE_PATH, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except Exception:
        return ""


async def run_docker_command(command: str, timeout: int = 30):
    """
    Run a Docker host-level command without raising on failure.
    Returns (stdout, stderr, returncode) always.
    Use for diagnostic/cleanup calls where you need the output regardless of exit code.
    """
    try:
        cmd = shlex.split(command)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode(errors="replace").strip(), stderr.decode(errors="replace").strip(), proc.returncode
    except asyncio.TimeoutError:
        return "", f"Command timed out after {timeout}s", -1
    except Exception as e:
        return "", str(e), -1


async def ensure_vps_image(force_rebuild: bool = False) -> tuple:
    """
    Ensure the darknodes-vps Docker image exists and is up to date.

    Logic:
      1. Verify the Dockerfile is present on disk.
      2. Check whether the image already exists in Docker.
      3. Compare the current Dockerfile MD5 against the last saved hash.
         If the hash changed (or the image is missing), rebuild automatically.
      4. Return (True, summary) on success or (False, full_build_error) on failure.

    Called before every container creation — admins never need to manage
    the image manually.
    """
    if not os.path.exists(DOCKERFILE_PATH):
        return False, (
            f"Dockerfile not found at `{DOCKERFILE_PATH}`.\n"
            "Ensure the Dockerfile sits in the same directory as bot.py."
        )

    current_hash = _dockerfile_hash()

    # ── Does the image already exist? ────────────────────────────────────────
    img_out, _, _ = await run_docker_command(f"docker images -q {DOCKER_IMAGE}", timeout=15)
    image_exists  = bool(img_out.strip())

    # ── Load the previously saved hash ───────────────────────────────────────
    stored_hash = ""
    try:
        with open(_IMAGE_HASH_FILE) as fh:
            stored_hash = fh.read().strip()
    except FileNotFoundError:
        pass

    dockerfile_changed = current_hash != stored_hash

    if image_exists and not dockerfile_changed and not force_rebuild:
        logger.info(f"darknodes-vps image is current (hash {current_hash[:8]})")
        return True, "Image is already up to date."

    # ── Build ─────────────────────────────────────────────────────────────────
    if not image_exists:
        reason = "image not found — building for the first time"
    elif force_rebuild:
        reason = "forced rebuild requested"
    else:
        reason = "Dockerfile changed — rebuilding"
    logger.info(f"Building {DOCKER_IMAGE} ({reason}) …")

    build_dir = os.path.dirname(os.path.abspath(DOCKERFILE_PATH))
    build_cmd = f"docker build -t {DOCKER_IMAGE} -f {DOCKERFILE_PATH} {build_dir}"

    try:
        proc = await asyncio.create_subprocess_shell(
            build_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,   # merge so we capture everything
        )
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
        build_logs = stdout_bytes.decode(errors="replace")

        if proc.returncode != 0:
            logger.error(f"Image build FAILED (rc={proc.returncode}):\n{build_logs[-3000:]}")
            return False, build_logs

        # Persist the hash so future runs skip the rebuild
        try:
            with open(_IMAGE_HASH_FILE, "w") as fh:
                fh.write(current_hash)
        except Exception as he:
            logger.warning(f"Could not save image hash: {he}")

        logger.info(f"{DOCKER_IMAGE} image built successfully.")
        return True, build_logs

    except asyncio.TimeoutError:
        return False, "Image build timed out after 10 minutes. Check your network or Docker daemon."
    except Exception as e:
        return False, f"Build process error: {e}"


# ─── Deployment progress embed builders ──────────────────────────────────────

_DEPLOY_STEPS = [
    "Verifying image",
    "Creating container",
    "Booting systemd",
    "Configuring VPS",
    "Verifying deployment",
    "Creating access methods",
]


def _deploy_progress_embed(step_index: int, steps: list = None, failed: bool = False) -> discord.Embed:
    """DarkNodes-branded live deployment progress embed."""
    if steps is None:
        steps = _DEPLOY_STEPS
    total  = len(steps)
    filled = min(step_index, total)
    pct    = int(filled / total * 100)
    bar    = "\u2588" * filled + "\u2591" * (total - filled)
    color  = 0xff4444 if failed else (0x00ff88 if filled >= total else 0x5865F2)

    lines = []
    for i, name in enumerate(steps):
        if failed and i == step_index:
            lines.append(f"\u274c  **{name}**  \u2190 failed here")
        elif i < filled:
            lines.append(f"\u2705  {name}")
        elif i == filled and not failed:
            lines.append(f"\u23f3  **{name}\u2026**")
        else:
            lines.append(f"\u2b1c  {name}")

    title = "\u274c  DarkNodes VPS \u2014 Deploy Failed" if failed else "\U0001f311  DarkNodes VPS \u2014 Deploying"
    embed = discord.Embed(
        title=title,
        description=f"`{bar}` **{pct}%**\n\n" + "\n".join(lines),
        color=color,
    )
    embed.set_footer(text="DarkNodes  \u2022  High-Performance VPS Hosting")
    return embed


def _deploy_success_embed(user: discord.Member, vps_count: int, container_name: str,
                           ram: str, cpu: str, disk: str, steps: list = None) -> discord.Embed:
    """Final success embed shown in the channel after deployment."""
    if steps is None:
        steps = _DEPLOY_STEPS
    bar   = "\u2588" * len(steps)
    lines = [f"\u2705  {s}" for s in steps]
    embed = discord.Embed(
        title="\u2705  DarkNodes VPS \u2014 Live!",
        description=f"`{bar}` **100%**\n\n" + "\n".join(lines),
        color=0x00ff88,
    )
    embed.add_field(name="\U0001f464 Owner",      value=user.mention,          inline=True)
    embed.add_field(name="\U0001f194 VPS ID",     value=f"`#{vps_count}`",     inline=True)
    embed.add_field(name="\U0001f4e6 Container",  value=f"`{container_name}`", inline=True)
    embed.add_field(name="\U0001f9e0 RAM",         value=f"`{ram} GB`",         inline=True)
    embed.add_field(name="\u2699\ufe0f CPU",       value=f"`{cpu} Core(s)`",    inline=True)
    embed.add_field(name="\U0001f4be Disk",        value=f"`{disk} GB`",        inline=True)
    embed.add_field(
        name="\U0001f3ae Next Step",
        value="Use `!manage` \u2192 **SSH** to get live tmate + sshx session links",
        inline=False,
    )
    embed.set_footer(text="DarkNodes VPS  \u2022  Deployed & Ready")
    return embed


def _vps_dm_embed(vps_count: int, container_name: str, ram: str, cpu: str,
                   tmate_ssh: str = "", sshx_url: str = "",
                   plan: str = None, processor: str = None) -> discord.Embed:
    """DM embed delivered to the VPS owner after deployment."""
    plan_str = (f"**{plan}**" + (f" ({processor})" if processor else "")) if plan else "Custom"
    embed = discord.Embed(
        title="\U0001f389  DarkNodes \u2014 Your VPS is Ready!",
        description=(
            f"Your VPS is **online** and fully configured.\n"
            f"**Plan:** {plan_str}  \u2022  **Container:** `{container_name}`"
        ),
        color=0x5865F2,
    )
    embed.add_field(name="\U0001f194 VPS ID",    value=f"`#{vps_count}`",   inline=True)
    embed.add_field(name="\U0001f9e0 RAM",         value=f"`{ram}`",           inline=True)
    embed.add_field(name="\u2699\ufe0f CPU",       value=f"`{cpu} Core(s)`",  inline=True)

    if tmate_ssh or sshx_url:
        access_parts = []
        if tmate_ssh:
            access_parts.append(f"**\U0001f5a5\ufe0f tmate SSH**\n```{tmate_ssh}```")
        if sshx_url:
            access_parts.append(f"**\U0001f517 sshx Web**\n{sshx_url}")
        embed.add_field(name="\U0001f511  Access Methods", value="\n".join(access_parts), inline=False)
    else:
        embed.add_field(
            name="\U0001f511  Access Methods",
            value="Use `!manage` \u2192 **SSH** to generate tmate + sshx session links",
            inline=False,
        )

    embed.add_field(
        name="\U0001f4cc  Tips",
        value=(
            "\u2022 `!manage` \u2192 **SSH** refreshes sessions on every click\n"
            "\u2022 Sessions are link-based \u2014 no password required\n"
            "\u2022 Keep this DM as your VPS reference"
        ),
        inline=False,
    )
    embed.set_footer(text="DarkNodes VPS  \u2022  Keep this DM safe")
    return embed


# ─── Container creation ───────────────────────────────────────────────────────

async def create_docker_container(container_name, ram_mb, cpu_count, ssh_port, password, disk_gb=30, hostname=None, progress_msg=None):
    """
    Create a VPS container from the darknodes-vps image.

    Design:
    - The darknodes-vps image boots via /lib/systemd/systemd (PID 1).
      All packages are pre-baked; containers start in seconds.
    - --privileged + --cgroupns=host are required for systemd cgroup management.
    - tmpfs mounts on /run, /run/lock, /tmp satisfy systemd runtime requirements.
    - Docker Engine runs INSIDE each VPS container (Docker-in-Docker / DinD).
    - Named volumes persist /var/lib/docker, /home, /root, /opt across restarts.
    - On ANY failure the container is KEPT and full diagnostics are raised:
      startup logs, docker inspect, and the exact docker run command.
      A failed container is never silently removed.
    - progress_msg: optional discord.Message updated live with deployment stages.
    """
    # ── Live progress helper ──────────────────────────────────────────────────
    async def _progress(step_idx: int, failed: bool = False):
        if progress_msg is None:
            return
        try:
            await progress_msg.edit(embed=_deploy_progress_embed(step_idx, failed=failed))
        except Exception:
            pass

    # Volume names — one persistent set per container
    _vol_docker = f"{container_name}-docker"
    _vol_home   = f"{container_name}-home"
    _vol_root   = f"{container_name}-root"
    _vol_opt    = f"{container_name}-opt"

    # 1. Ensure the base image exists and matches the current Dockerfile
    await _progress(0)
    img_ok, img_detail = await ensure_vps_image()
    if not img_ok:
        raise RuntimeError(
            f"Cannot create VPS — darknodes-vps image unavailable.\n\n"
            f"Build error:\n{img_detail[-3000:]}"
        )

    _hostname = hostname or f"{VPS_HOSTNAME}-1"

    # 2. Build docker run flags
    #    Full Docker-in-Docker: Docker Engine runs INSIDE each VPS container.
    #    Named volumes persist /var/lib/docker, /home, /root, /opt across restarts.
    #    No host socket is mounted — each VPS has its own independent dockerd.
    _port_flag = f"-p {ssh_port}:22 " if ssh_port and ssh_port > 0 else ""
    base_flags = (
        f"--name {container_name} "
        f"--hostname {_hostname} "
        f"--memory={ram_mb}m "
        f"--memory-swap={ram_mb * 2}m "
        f"--cpus={cpu_count} "
        f"--restart=unless-stopped "
        f"--privileged "
        f"--cgroupns=host "
        f"--tmpfs /run:exec,mode=755,size=256m "
        f"--tmpfs /run/lock:size=64m "
        f"--tmpfs /tmp:exec,size=512m "
        f"-v {_vol_docker}:/var/lib/docker "
        f"-v {_vol_home}:/home "
        f"-v {_vol_root}:/root "
        f"-v {_vol_opt}:/opt "
        f"-e container=docker "
        f"-l darknodes.vps=true "
        f"-l darknodes.owner={container_name} "
        f"{_port_flag}"
    )
    run_cmd = f"docker run -d {base_flags}{DOCKER_IMAGE}"

    # Helper: collect diagnostics without raising
    async def _collect_diagnostics(container: str) -> str:
        logs_out,    _, _ = await run_docker_command(f"docker logs --tail=80 {container}",   timeout=20)
        inspect_out, _, _ = await run_docker_command(f"docker inspect {container}",           timeout=15)
        inspect_snippet = inspect_out[:2000] + "\n…(truncated)" if len(inspect_out) > 2000 else inspect_out
        return (
            f"── Exact docker run command ─────────────────────────────────\n"
            f"{run_cmd}\n\n"
            f"── Container startup logs (last 80 lines) ───────────────────\n"
            f"{logs_out or '(none)'}\n\n"
            f"── docker inspect (truncated) ───────────────────────────────\n"
            f"{inspect_snippet or '(none)'}"
        )

    # 3. Start the container — CMD from the image is /lib/systemd/systemd (PID 1)
    await _progress(1)
    try:
        await execute_docker(run_cmd, timeout=60)
        logger.info(f"Container {container_name} started from {DOCKER_IMAGE}")
    except Exception as start_err:
        raise RuntimeError(
            f"Failed to start container: {start_err}\n\n"
            f"── Exact docker run command ──────────────────────────────────\n"
            f"{run_cmd}"
        )

    # 4. Wait for systemd to reach its running/degraded state (polled inside configure_vps)
    await _progress(2)
    await asyncio.sleep(3)

    # 5. Configure the VPS — password, hostname, Docker daemon wait, SSH
    await _progress(3)
    try:
        await configure_vps(container_name, password, hostname=_hostname)
    except Exception as cfg_err:
        diag = await _collect_diagnostics(container_name)
        raise RuntimeError(
            f"VPS configuration failed: {cfg_err}\n\n{diag}"
        )

    # 6. Full verification — ALL checks are critical; on failure keep the container
    await _progress(4)
    passed, report = await verify_vps(container_name, hostname=_hostname)
    if not passed:
        diag = await _collect_diagnostics(container_name)
        raise RuntimeError(
            f"VPS verification failed — container kept for diagnosis.\n\n"
            f"Verification report:\n{report}\n\n"
            f"{diag}"
        )

    return True


async def configure_vps(container_name: str, password: str, hostname: str = None):
    """
    Configure a running darknodes-vps container.

    Steps:
      1. Poll until systemd reaches running/degraded state (max 60 s).
      2. Set the root password to the generated per-container value.
      3. Apply the unique per-container hostname.
      4. Wait for Docker daemon (DinD) to become healthy inside the container.
      5. Ensure sshd is active via systemd.

    Raises RuntimeError if the script does not confirm completion.
    """
    _hostname = hostname or VPS_HOSTNAME
    logger.info(f"Configuring VPS {container_name} (hostname={_hostname}) …")

    configure_script = f"""\nexport PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n# ── 1. Wait for systemd to reach a stable state ──────────────────────────────\n# Poll is-system-running until it returns "running" or "degraded".\n# "degraded" is acceptable — it means systemd booted but some non-critical\n# unit failed (common in containers).
_waited=0
while [ $_waited -lt 60 ]; do
    _state=$(systemctl is-system-running 2>/dev/null || true)
    # "maintenance" is normal in Docker — rescue/emergency units fail harmlessly
    if [ "$_state" = "running" ] || [ "$_state" = "degraded" ] || [ "$_state" = "maintenance" ]; then
        break
    fi
    sleep 2
    _waited=$((_waited + 2))
done
echo "systemd state after wait: $(systemctl is-system-running 2>/dev/null || echo unknown)"

# ── 1b. Mask rescue/emergency units that cause false failures in containers ───
systemctl mask rescue.service rescue.target emergency.service emergency.target 2>/dev/null || true

# ── 2. Set root password ──────────────────────────────────────────────────────
echo 'root:{password}' | chpasswd

# ── 3. Apply unique per-container hostname ────────────────────────────────────
hostname {_hostname}
echo "{_hostname}" > /etc/hostname
grep -q "{_hostname}" /etc/hosts || echo "127.0.1.1 {_hostname}" >> /etc/hosts

# ── 4. Wait for Docker daemon (DinD) to become healthy ───────────────────────
# docker.service is enabled in the image; systemd starts it at boot automatically.
# Poll until `docker info` succeeds (daemon ready) or 90 s elapses.
_docker_waited=0
while [ $_docker_waited -lt 90 ]; do
    if systemctl is-active --quiet docker 2>/dev/null; then
        if docker info >/dev/null 2>&1; then
            echo "Docker daemon healthy after ${_docker_waited}s."
            break
        fi
    fi
    sleep 3
    _docker_waited=$((_docker_waited + 3))
done
if ! docker info >/dev/null 2>&1; then
    echo "WARNING: Docker daemon not healthy after 90s — check journald."
    journalctl -u docker --no-pager -n 30 2>/dev/null || true
fi

# ── 5. Ensure SSH host keys and sshd service ─────────────────────────────────
ssh-keygen -A 2>/dev/null || true
mkdir -p /run/sshd

if systemctl is-active --quiet ssh 2>/dev/null || systemctl is-active --quiet sshd 2>/dev/null; then
    : # already active — nothing to do
else
    systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
    sleep 2
fi

echo "DARKNODES_CONFIGURE_COMPLETE"
"""

    stdout, stderr, rc = await docker_exec(container_name, configure_script, timeout=150)
    if "DARKNODES_CONFIGURE_COMPLETE" not in stdout:
        raise RuntimeError(
            f"Configuration script did not complete (exit {rc})\n"
            f"stdout: {stdout[-800:]}\n"
            f"stderr: {stderr[-500:]}"
        )
    logger.info(f"VPS {container_name} configured successfully.")


async def verify_vps(container_name: str, hostname: str = None) -> tuple:
    """
    Verify a newly-created container is fully functional (DinD architecture).

    Returns (all_passed: bool, human_readable_report: str).

    Critical (deployment fails if any fail):
      ✓ PID 1 is systemd
      ✓ systemctl --version responsive
      ✓ Docker service active  (systemctl is-active docker)
      ✓ Docker daemon info     (docker info — confirms DinD is running)
      ✓ docker ps              (daemon end-to-end sanity)
      ✓ SSH active             (systemctl is-active ssh)
      ✓ Hostname matches expected value

    Optional (warnings only):
      ✓ docker version / compose
      ✓ docker run --rm hello-world  (DinD pull + run)
      ✓ tmate installed
      ✓ sshx installed
    """
    results: Dict[str, tuple] = {}

    async def run_check(label: str, cmd: str, expect: str = None, timeout: int = 20) -> bool:
        try:
            stdout, stderr, rc = await docker_exec(container_name, cmd, timeout=timeout)
            out = (stdout + " " + stderr).strip()
            if rc != 0:
                results[label] = (False, f"exit {rc} — {out[:150]}")
                return False
            if expect and expect.lower() not in out.lower():
                results[label] = (False, f"'{expect}' not found — got: {out[:150]}")
                return False
            results[label] = (True, stdout[:80].strip() or "ok")
            return True
        except Exception as exc:
            results[label] = (False, str(exc)[:150])
            return False

    # Give services a moment to settle after configure_vps
    await asyncio.sleep(6)

    # ── Critical: systemd ─────────────────────────────────────────────────────
    await run_check("PID 1 is systemd",
                    "cat /proc/1/comm", "systemd")
    await run_check("systemctl",
                    "systemctl --version 2>&1", "systemd")

    # ── Critical: Docker daemon (DinD) ────────────────────────────────────────
    # docker.service is enabled in the image; systemd starts it at boot.
    # Retry up to once to allow dockerd to finish initialising.
    docker_svc_ok = await run_check(
        "Docker service active",
        "systemctl is-active docker 2>/dev/null || echo inactive",
        "active",
    )
    if not docker_svc_ok:
        await asyncio.sleep(10)
        docker_svc_ok = await run_check(
            "Docker service active (retry)",
            "systemctl is-active docker 2>/dev/null || echo inactive",
            "active",
        )

    docker_info_ok = await run_check(
        "Docker daemon info",
        "docker info 2>&1 | head -20",
        "Server Version",
        timeout=30,
    )
    if not docker_info_ok:
        await asyncio.sleep(8)
        docker_info_ok = await run_check(
            "Docker daemon info (retry)",
            "docker info 2>&1 | head -20",
            "Server Version",
            timeout=30,
        )

    await run_check(
        "docker ps",
        "docker ps --format '{{.ID}}' 2>&1",
        None,
        timeout=20,
    )

    # ── Critical: SSH ─────────────────────────────────────────────────────────
    ssh_ok = await run_check(
        "SSH active",
        "systemctl is-active ssh 2>/dev/null || "
        "systemctl is-active sshd 2>/dev/null || echo inactive",
        "active",
    )
    if not ssh_ok:
        await asyncio.sleep(5)
        await run_check(
            "SSH active (retry)",
            "systemctl is-active ssh 2>/dev/null || "
            "systemctl is-active sshd 2>/dev/null || echo inactive",
            "active",
        )

    # ── Critical: hostname ────────────────────────────────────────────────────
    await run_check("Hostname", "hostname", hostname or VPS_HOSTNAME)

    # ── Optional: extra Docker checks ─────────────────────────────────────────
    await run_check("docker version",
                    "docker version --format '{{.Server.Version}}' 2>&1 || docker version 2>&1 | head -5",
                    None, timeout=15)
    await run_check("docker compose",
                    "docker compose version 2>&1", "compose", timeout=15)

    # hello-world confirms DinD can pull images and run containers end-to-end
    await run_check("docker run hello-world",
                    "docker run --rm hello-world 2>&1 | tail -5",
                    "Hello from Docker", timeout=90)

    # Tool presence
    await run_check("tmate installed",
                    "command -v tmate && tmate -V 2>&1 || echo MISSING", "tmate")
    await run_check("sshx installed",
                    "test -f /usr/local/bin/sshx && echo found || echo MISSING", "found")

    # ── Build report ──────────────────────────────────────────────────────────
    _CRITICAL = frozenset({
        "PID 1 is systemd", "systemctl",
        "Docker service active", "Docker service active (retry)",
        "Docker daemon info", "Docker daemon info (retry)",
        "docker ps",
        "SSH active", "SSH active (retry)", "Hostname",
    })
    _OPTIONAL = frozenset({
        "docker version", "docker compose",
        "docker run hello-world",
        "tmate installed", "sshx installed",
    })

    report_lines = []
    for lbl, (ok, detail) in results.items():
        if lbl in _OPTIONAL:
            icon = "✅" if ok else "⚠️"
            tag  = "" if ok else " (optional)"
        else:
            icon = "✅" if ok else "❌"
            tag  = ""
        report_lines.append(f"{icon} {lbl}{tag}: {detail}")
    report = "\n".join(report_lines)

    critical_failed   = [lbl for lbl, (ok, _) in results.items() if lbl in _CRITICAL and not ok]
    optional_warnings = [lbl for lbl, (ok, _) in results.items() if lbl in _OPTIONAL and not ok]
    all_passed = len(critical_failed) == 0

    if all_passed:
        if optional_warnings:
            logger.warning(f"VPS {container_name} verification PASSED "
                           f"(optional warned: {optional_warnings}).\n{report}")
        else:
            logger.info(f"VPS {container_name} verification PASSED.\n{report}")
    else:
        logger.error(f"VPS {container_name} verification FAILED "
                     f"— critical: {critical_failed}.\n{report}")

    return all_passed, report


async def _get_server_ip() -> str:
    """Return the server's public IP for direct SSH connection strings."""
    if SERVER_IP:
        return SERVER_IP
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", "hostname -I 2>/dev/null | awk '{print $1}'",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        ip = out.decode().strip().split()[0] if out.strip() else ""
        return ip
    except Exception:
        return ""


async def get_tmate_session(container_name: str) -> dict:
    """
    Start a tmate session inside the container.

    Returns dict:  {"ssh": "<connection string>"}
    Only the SSH connection string is returned — web URLs are not exposed.
    Raises Exception on failure so callers can treat it as optional.
    """
    chk_out, _, chk_rc = await docker_exec(
        container_name, "command -v tmate && tmate -V", timeout=10
    )
    if chk_rc != 0:
        raise Exception("tmate binary not found in container image")

    start_script = r"""
pkill tmate 2>/dev/null || true
sleep 1
rm -f /tmp/tmate.sock
tmate -S /tmp/tmate.sock new-session -d 2>&1
sleep 2
tmate -S /tmp/tmate.sock wait tmate-ready 2>&1
TMATE_SSH=$(tmate -S /tmp/tmate.sock display -p '#{tmate_ssh}' 2>/dev/null || true)
echo "TMATE_SSH:${TMATE_SSH}"
"""
    stdout, stderr, rc = await docker_exec(container_name, start_script, timeout=45)

    tmate_ssh = ""
    for line in stdout.splitlines():
        if line.startswith("TMATE_SSH:"):
            tmate_ssh = line[len("TMATE_SSH:"):].strip()

    if not tmate_ssh:
        raise Exception(f"tmate SSH string not returned. stderr: {stderr[:300]}")

    return {"ssh": tmate_ssh}


async def get_sshx_session(container_name: str) -> str:
    """
    Start an sshx session inside the container and return the validated web URL.

    ANSI escape codes are stripped before URL extraction.
    Raises Exception on failure so callers can treat it as optional.
    """
    chk_out, _, chk_rc = await docker_exec(
        container_name, "command -v sshx", timeout=10
    )
    if chk_rc != 0:
        raise Exception("sshx binary not found in container image")

    # NO_COLOR + TERM=dumb suppresses ANSI at source; sed strips any survivors.
    start_script = r"""
pkill sshx 2>/dev/null || true
sleep 1
TERM=dumb NO_COLOR=1 COLORTERM= sshx > /tmp/sshx.log 2>&1 &
_waited=0
while [ $_waited -lt 30 ]; do
    _url=$(sed 's/\x1b\[[0-9;]*[mGKHFJSTsulhABCDEFfr]//g; s/\x1b\[?[0-9;]*[hl]//g; s/\r//g' \
           /tmp/sshx.log 2>/dev/null | grep -o 'https://sshx\.io/s/[^[:space:]]*' | head -1)
    if [ -n "$_url" ]; then
        echo "SSHX_URL:${_url}"
        exit 0
    fi
    sleep 1
    _waited=$((_waited + 1))
done
echo "SSHX_URL:"
echo "SSHX_DEBUG:$(sed 's/\x1b\[[0-9;]*[mGKHFJSTsulhABCDEFfr]//g' /tmp/sshx.log 2>/dev/null | tail -5)"
"""
    stdout, stderr, rc = await docker_exec(container_name, start_script, timeout=40)

    raw_url    = ""
    debug_info = ""
    for line in stdout.splitlines():
        if line.startswith("SSHX_URL:"):
            raw_url = line[len("SSHX_URL:"):].strip()
        elif line.startswith("SSHX_DEBUG:"):
            debug_info = line[len("SSHX_DEBUG:"):].strip()

    if not raw_url:
        hint = f" Log tail: {debug_info[:200]}" if debug_info else f" stderr: {stderr[:200]}"
        raise Exception(f"sshx did not produce a URL within 30s.{hint}")

    # Python-side ANSI strip (safety net)
    url = re.sub(r'\x1b\[[0-9;]*[mGKHFJSTsulhABCDEFfr]', '', raw_url)
    url = re.sub(r'\x1b\[?[0-9;]*[hl]', '', url).strip()

    if not re.match(r'^https://sshx\.io/s/[A-Za-z0-9_\-]+(#[A-Za-z0-9_\-]+)?$', url):
        raise Exception(f"sshx returned an invalid or garbled URL: {url!r}")

    return url

# ─── VPS Role helper ──────────────────────────────────────────────────────────

async def get_or_create_vps_role(guild):
    global VPS_USER_ROLE_ID
    if VPS_USER_ROLE_ID:
        role = guild.get_role(VPS_USER_ROLE_ID)
        if role:
            return role
    role = discord.utils.get(guild.roles, name="VPS User")
    if role:
        VPS_USER_ROLE_ID = role.id
        return role
    try:
        role = await guild.create_role(
            name="VPS User",
            color=discord.Color.dark_purple(),
            reason="VPS User role for bot management",
            permissions=discord.Permissions.none()
        )
        VPS_USER_ROLE_ID = role.id
        logger.info(f"Created VPS User role: {role.name} (ID: {role.id})")
        return role
    except Exception as e:
        logger.error(f"Failed to create VPS User role: {e}")
        return None

# ─── Anti-mining detection helpers ───────────────────────────────────────────

def _is_whitelisted(container_name: str, owner_user_id: str) -> bool:
    """Return True if this VPS/user is on the admin whitelist."""
    return (
        owner_user_id in MONITOR_CONFIG.get("whitelisted_users", []) or
        container_name in MONITOR_CONFIG.get("whitelisted_containers", [])
    )

def _process_is_safe(proc_name: str) -> bool:
    """Return True if the process name is on the safe-process whitelist."""
    safe = MONITOR_CONFIG.get("whitelisted_processes", [])
    proc_lower = proc_name.lower()
    return any(s.lower() in proc_lower for s in safe)

async def _get_container_cpu(container_name: str) -> float:
    """
    Return current CPU usage % for a container using docker stats.
    Returns 0.0 on any error.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}",
            container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        raw = stdout.decode().strip().replace("%", "")
        return float(raw) if raw else 0.0
    except Exception:
        return 0.0

async def _scan_container_for_mining(container_name: str) -> Dict:
    """
    Inspect a container for mining indicators.
    Returns a dict with keys:
      found_processes, found_connections, found_cli_args, confidence_score, details
    """
    result = {
        "found_processes": [],
        "found_connections": [],
        "found_cli_args": [],
        "confidence_score": 0,
        "details": []
    }

    blacklist_procs = MONITOR_CONFIG.get("mining_process_blacklist", [])
    blacklist_pools = MONITOR_CONFIG.get("mining_pool_blacklist", [])
    blacklist_cli   = MONITOR_CONFIG.get("mining_cli_blacklist",   [])

    # ── Check 1: Known mining process names in ps ────────────────────────────
    try:
        stdout, _, rc = await docker_exec(
            container_name, "ps aux --no-headers 2>/dev/null || true", timeout=15
        )
        if rc == 0:
            for line in stdout.splitlines():
                line_lower = line.lower()
                # Skip safe processes first
                parts = line.split()
                proc_name = parts[10] if len(parts) > 10 else ""
                if _process_is_safe(proc_name):
                    continue
                for miner in blacklist_procs:
                    if miner.lower() in line_lower:
                        result["found_processes"].append(miner)
                        result["details"].append(f"Mining process: {miner}")
                for cli in blacklist_cli:
                    if cli.lower() in line_lower:
                        result["found_cli_args"].append(cli)
                        result["details"].append(f"Mining CLI arg: {cli}")
    except Exception:
        pass

    # ── Check 2: Network connections to known mining pools ───────────────────
    try:
        # ss is more reliable than netstat in modern containers
        stdout, _, rc = await docker_exec(
            container_name,
            "ss -tnp 2>/dev/null || netstat -tnp 2>/dev/null || true",
            timeout=15
        )
        if rc == 0:
            for line in stdout.splitlines():
                line_lower = line.lower()
                for pool in blacklist_pools:
                    if pool.lower() in line_lower:
                        result["found_connections"].append(pool)
                        result["details"].append(f"Mining pool connection: {pool}")
        # Also check /etc/hosts and DNS cache for pool domains
        stdout2, _, _ = await docker_exec(
            container_name, "cat /etc/hosts 2>/dev/null || true", timeout=10
        )
        for pool in blacklist_pools:
            if pool.lower() in stdout2.lower():
                if pool not in result["found_connections"]:
                    result["found_connections"].append(pool)
                    result["details"].append(f"Mining pool in /etc/hosts: {pool}")
    except Exception:
        pass

    # ── Confidence scoring ───────────────────────────────────────────────────
    # Rules:
    #   +25 per unique mining process found        (capped at 50)
    #   +25 per unique mining pool connection      (capped at 50)
    #   +15 per unique mining CLI argument found   (capped at 30)
    # CPU indicator is added by the caller (not here) to keep this pure.
    # Deduplication: score each category independently.

    proc_score = min(len(set(result["found_processes"])) * 25, 50)
    conn_score = min(len(set(result["found_connections"])) * 25, 50)
    cli_score  = min(len(set(result["found_cli_args"]))  * 15, 30)

    result["confidence_score"] = proc_score + conn_score + cli_score

    # Deduplicate lists
    result["found_processes"]  = list(set(result["found_processes"]))
    result["found_connections"] = list(set(result["found_connections"]))
    result["found_cli_args"]   = list(set(result["found_cli_args"]))

    return result


async def _suspend_vps_for_mining(
    container_name: str,
    owner_user_id: str,
    evidence: Dict,
    cpu_pct: float
):
    """
    Stop a container suspected of mining and mark it as suspended.
    Stores evidence and sends a Discord admin notification.
    """
    # Stop the container
    try:
        await execute_docker(f"docker stop --time=5 {container_name}", timeout=30)
        logger.warning(f"SUSPENDED for mining: {container_name}")
    except Exception as e:
        logger.error(f"Failed to stop container {container_name}: {e}")

    # Mark VPS as suspended in vps_data
    uid, vps = find_vps_record(container_name)
    if vps:
        vps["status"]            = "suspended"
        vps["suspension_reason"] = "Automated anti-mining detection"
        vps["suspension_time"]   = datetime.utcnow().isoformat()
        vps["suspension_evidence"] = evidence
        save_data()

    # Append to suspension log (persistent)
    try:
        mem_stdout, _, _ = await docker_exec(
            container_name,
            "free -m 2>/dev/null | awk '/Mem:/{print $3\"/\"$2}' || echo 'N/A'",
            timeout=10
        )
    except Exception:
        mem_stdout = "N/A"

    log_entry = {
        "timestamp":        datetime.utcnow().isoformat(),
        "container_name":   container_name,
        "owner_user_id":    owner_user_id,
        "cpu_pct":          cpu_pct,
        "ram_info":         mem_stdout,
        "confidence_score": evidence.get("confidence_score", 0),
        "found_processes":  evidence.get("found_processes", []),
        "found_connections": evidence.get("found_connections", []),
        "found_cli_args":   evidence.get("found_cli_args", []),
        "details":          evidence.get("details", []),
    }
    suspension_log.append(log_entry)
    save_suspension_log()

    # Append to per-container monitor log
    if container_name not in monitor_log:
        monitor_log[container_name] = []
    monitor_log[container_name].append({"type": "suspension", **log_entry})
    save_monitor_log()

    # ── Discord log-channel notification ─────────────────────────────────────
    channel_id = MONITOR_CONFIG.get("notification_channel_id", 0)
    if channel_id:
        try:
            channel = bot.get_channel(int(channel_id))
            if channel is None:
                channel = await bot.fetch_channel(int(channel_id))
            if channel:
                # Resolve owner mention
                try:
                    owner_user = await bot.fetch_user(int(owner_user_id))
                    owner_str  = f"{owner_user.mention} (`{owner_user}` · ID `{owner_user_id}`)"
                    owner_avatar = owner_user.display_avatar.url
                except Exception:
                    owner_user   = None
                    owner_str    = f"ID `{owner_user_id}`"
                    owner_avatar = None

                # Pull VPS spec from vps_data for extra context
                _, vps_rec = find_vps_record(container_name)
                plan_label = vps_rec.get("plan_name",  "Unknown") if vps_rec else "Unknown"
                ram_label  = vps_rec.get("ram",        "?")       if vps_rec else "?"
                cpu_label  = vps_rec.get("cpu",        "?")       if vps_rec else "?"
                ssh_port   = vps_rec.get("ssh_port",   "?")       if vps_rec else "?"

                # Determine primary detection method(s) for the title line
                methods = []
                if evidence.get("found_processes"):
                    methods.append("Process name match")
                if evidence.get("found_connections"):
                    methods.append("Mining pool connection")
                if evidence.get("found_cli_args"):
                    methods.append("Mining CLI arguments")
                if not methods:
                    methods.append("High CPU heuristic")
                method_str = " · ".join(methods)

                # Confidence bar (10 blocks, each = 10 pts)
                score     = min(evidence.get("confidence_score", 0), 100)
                filled    = score // 10
                conf_bar  = "🟥" * filled + "⬛" * (10 - filled)

                # Detail bullets
                procs = evidence.get("found_processes",  [])
                conns = evidence.get("found_connections", [])
                cargs = evidence.get("found_cli_args",   [])
                dets  = evidence.get("details",          [])

                def fmt_list(lst):
                    return "\n".join(f"• `{x}`" for x in lst) if lst else "`none`"

                now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

                embed = discord.Embed(
                    title="🚨  Mining Activity Detected — VPS Auto-Suspended",
                    description=(
                        f"Container `{container_name}` has been **automatically stopped** and marked **suspended**.\n"
                        f"**Detection method:** {method_str}"
                    ),
                    colour=discord.Colour.from_rgb(220, 20, 60),
                    timestamp=datetime.utcnow()
                )
                if owner_avatar:
                    embed.set_thumbnail(url=owner_avatar)
                embed.set_footer(text="DarkNodes Anti-Abuse System  •  Auto-Monitor")

                # Row 1 — identifiers
                embed.add_field(name="📦 Container ID",    value=f"`{container_name}`",          inline=True)
                embed.add_field(name="🔌 SSH Port",        value=f"`{ssh_port}`",                 inline=True)
                embed.add_field(name="📋 Plan",            value=f"`{plan_label}`",               inline=True)

                # Row 2 — owner + specs
                embed.add_field(name="👤 Owner",           value=owner_str,                       inline=False)
                embed.add_field(name="🧠 RAM",             value=f"`{ram_mb} MB`" if vps_rec else f"`{ram_label}`", inline=True)
                embed.add_field(name="⚡ CPU Cores",       value=f"`{cpu_label}`",                inline=True)
                embed.add_field(name="🖥️ CPU Usage",       value=f"`{cpu_pct:.1f}%`",             inline=True)

                # Row 3 — score
                embed.add_field(
                    name="🔢 Confidence Score",
                    value=f"{conf_bar}\n**{score}/100** (suspend threshold: {MONITOR_CONFIG.get('auto_suspend_threshold', 70)})",
                    inline=False
                )

                # Row 4 — evidence breakdown
                embed.add_field(name="🦠 Mining Processes",        value=fmt_list(procs), inline=True)
                embed.add_field(name="🌐 Pool Connections",         value=fmt_list(conns), inline=True)
                embed.add_field(name="💻 Suspicious CLI Args",      value=fmt_list(cargs), inline=True)

                # Row 5 — full detail log (truncated to fit field limit)
                if dets:
                    det_str = "\n".join(f"• {d}" for d in dets)
                    if len(det_str) > 1000:
                        det_str = det_str[:997] + "…"
                    embed.add_field(name="📝 Full Detection Log", value=det_str, inline=False)

                # Row 6 — status + timestamp
                embed.add_field(name="🔒 VPS Status",   value="**SUSPENDED** ⛔",    inline=True)
                embed.add_field(name="🕒 Suspended At", value=f"`{now_str}`",          inline=True)
                embed.add_field(name="📌 Action",
                                value="Use `!vps-unsuspend` to restore after review.", inline=False)

                await send_log(embed)
        except Exception as notify_err:
            logger.error(f"Failed to build/send mining alert: {notify_err}")


# ─── Smart async abuse monitor ────────────────────────────────────────────────
# Replaces the old threading CPU monitor.  Runs entirely in asyncio so it
# doesn't block the bot and can make async docker calls.

@tasks.loop(seconds=120)   # default; overridden after config load
async def abuse_monitor():
    """
    Per-container anti-mining monitor.

    Scoring:
      +30  Sustained CPU > threshold for the configured duration
      +25  Known mining process found            (×count, capped at 50)
      +25  Mining pool connection detected       (×count, capped at 50)
      +15  Mining CLI argument found             (×count, capped at 30)
      ──────────────────────────────────────────────────────────────────
      Max  160 points (effectively capped to 100 in the embed display)

    Auto-suspend fires ONLY when score ≥ auto_suspend_threshold (default 70).
    High CPU alone is worth 30 points — never enough to suspend on its own.
    """
    if not MONITOR_CONFIG.get("auto_suspend_enabled", True):
        return

    cpu_threshold     = MONITOR_CONFIG.get("cpu_threshold", 95)
    sustained_minutes = MONITOR_CONFIG.get("sustained_duration_minutes", 20)
    suspend_threshold = MONITOR_CONFIG.get("auto_suspend_threshold", 70)

    # Collect all running containers tracked by the bot
    running_containers = [
        (uid, vps)
        for uid, vps_list in vps_data.items()
        for vps in vps_list
        if vps.get("status") == "running" and not vps.get("suspension_reason")
    ]

    for owner_uid, vps in running_containers:
        container_name = vps.get("container_name", "")
        if not container_name:
            continue

        # Skip whitelisted VPSs
        if _is_whitelisted(container_name, owner_uid):
            continue

        state = get_container_state(container_name)
        if state.get("monitoring_paused"):
            continue

        try:
            # ── CPU sample ──────────────────────────────────────────────────
            cpu_pct = await _get_container_cpu(container_name)
            state["cpu_samples"].append(cpu_pct)

            # Track start of sustained high-CPU window
            if cpu_pct > cpu_threshold:
                if state["high_cpu_start"] is None:
                    state["high_cpu_start"] = datetime.utcnow()
            else:
                state["high_cpu_start"] = None  # reset when CPU drops

            # ── Sustained CPU indicator (+30 pts) ───────────────────────────
            cpu_score = 0
            if state["high_cpu_start"] is not None:
                elapsed_minutes = (datetime.utcnow() - state["high_cpu_start"]).seconds / 60
                if elapsed_minutes >= sustained_minutes:
                    cpu_score = 30
                    state["flags"].add("sustained_cpu")
                    logger.debug(
                        f"{container_name}: sustained CPU {cpu_pct:.1f}% "
                        f"for {elapsed_minutes:.1f}min (score +30)"
                    )

            # Only do deep process/network scan when CPU is elevated (saves resources)
            if cpu_pct < 50 and cpu_score == 0:
                # No concerning CPU — skip expensive scan
                state["last_confidence"] = 0
                continue

            # ── Deep scan ───────────────────────────────────────────────────
            scan = await _scan_container_for_mining(container_name)
            total_score = cpu_score + scan["confidence_score"]
            state["last_confidence"] = total_score

            # Log scan event
            if container_name not in monitor_log:
                monitor_log[container_name] = []
            monitor_log[container_name].append({
                "type":             "scan",
                "timestamp":        datetime.utcnow().isoformat(),
                "cpu_pct":          cpu_pct,
                "cpu_score":        cpu_score,
                "scan_score":       scan["confidence_score"],
                "total_score":      total_score,
                "found_processes":  scan["found_processes"],
                "found_connections": scan["found_connections"],
            })
            # Keep monitor log bounded (last 100 events per container)
            monitor_log[container_name] = monitor_log[container_name][-100:]
            save_monitor_log()

            if total_score > 0:
                logger.info(
                    f"Abuse scan {container_name}: CPU={cpu_pct:.1f}% "
                    f"score={total_score} procs={scan['found_processes']} "
                    f"conns={scan['found_connections']}"
                )

            # ── Auto-suspend decision ────────────────────────────────────────
            if total_score >= suspend_threshold and MONITOR_CONFIG.get("auto_suspend_enabled", True):
                evidence = {**scan, "confidence_score": total_score, "cpu_score": cpu_score}
                await _suspend_vps_for_mining(container_name, owner_uid, evidence, cpu_pct)
                # Reset state after suspension
                container_monitor_state.pop(container_name, None)

        except Exception as scan_err:
            logger.error(f"abuse_monitor error for {container_name}: {scan_err}")

@abuse_monitor.before_loop
async def before_abuse_monitor():
    await bot.wait_until_ready()

# ─── Bot events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    logger.info(f'{bot.user} has connected to Discord!')
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="DarkNodes | VPS Manager"))
    if not auto_expire_check.is_running():
        auto_expire_check.start()
    if not abuse_monitor.is_running():
        abuse_monitor.change_interval(seconds=MONITOR_CONFIG.get("monitoring_interval", 120))
        abuse_monitor.start()
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")
    # Pre-build the VPS image at startup so the first deployment is instant.
    # Runs as a background task so it does not block the bot from coming online.
    async def _startup_image_check():
        ok, detail = await ensure_vps_image()
        if ok:
            logger.info("Startup image check: darknodes-vps is ready.")
        else:
            logger.error(f"Startup image check FAILED — VPS creation will not work until this is resolved:\n{detail[-1000:]}")
    asyncio.create_task(_startup_image_check())
    logger.info("Bot is ready!")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=create_error_embed("Missing Argument", "Please use `!help` for command usage."))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=create_error_embed("Invalid Argument", "Please check your input and try again."))
    elif isinstance(error, commands.CheckFailure):
        pass
    else:
        logger.error(f"Command error: {error}")
        await ctx.send(embed=create_error_embed("System Error", "An error occurred. Please try again."))

# ─── ManageView ────────────────────────────────────────────────────────────────

class ManageView(discord.ui.View):
    def __init__(self, user_id, vps_list, is_shared=False, owner_id=None, is_admin=False):
        super().__init__(timeout=300)
        self.user_id      = user_id
        self.vps_list     = vps_list
        self.selected_index = None
        self.is_shared    = is_shared
        self.owner_id     = owner_id or user_id
        self.is_admin     = is_admin

        if len(vps_list) > 1:
            options = [
                discord.SelectOption(
                    label=f"VPS {i+1} ({v.get('plan', 'Custom')})",
                    description=f"Status: {v.get('status', 'unknown')}",
                    value=str(i)
                ) for i, v in enumerate(vps_list)
            ]
            self.select = discord.ui.Select(placeholder="Select a VPS to manage", options=options)
            self.select.callback = self.select_vps
            self.add_item(self.select)
            self.initial_embed = create_embed("VPS Management", "Select a VPS from the dropdown menu below.", 0x1a1a1a)
            self.initial_embed.add_field(
                name="Available VPS",
                value="\n".join([
                    f"**VPS {i+1}:** `{v['container_name']}` - Status: `{v.get('status','unknown').upper()}`"
                    for i, v in enumerate(vps_list)
                ]),
                inline=False
            )
        else:
            self.selected_index = 0
            self.initial_embed  = self.create_vps_embed(0)
            self.add_action_buttons()

    def create_vps_embed(self, index):
        vps = self.vps_list[index]
        status = vps.get('status', 'unknown')
        if status == 'suspended':
            status_color = 0xff6600
        elif status == 'running':
            status_color = 0x00ff88
        else:
            status_color = 0xff3366

        owner_text = ""
        if self.is_admin and self.owner_id != self.user_id:
            try:
                owner_user = bot.get_user(int(self.owner_id))
                owner_text = f"\n**Owner:** {owner_user.mention}"
            except Exception:
                owner_text = f"\n**Owner ID:** {self.owner_id}"

        embed = create_embed(
            f"VPS Management - VPS {index + 1}",
            f"Managing container: `{vps['container_name']}`{owner_text}",
            status_color
        )

        expires = vps.get('expires')
        if expires and expires != "Never":
            try:
                exp_dt    = datetime.fromisoformat(expires)
                days_left = (exp_dt - datetime.utcnow()).days
                expire_str = (
                    f"{expires[:10]} ({days_left}d left)"
                    if days_left >= 0
                    else f"{expires[:10]} (**EXPIRED**)"
                )
            except Exception:
                expire_str = expires
        else:
            expire_str = "Never"

        resource_info = (
            f"**Plan:** {vps.get('plan', 'Custom')}\n"
            f"**Status:** `{status.upper()}`\n"
            f"**RAM:** {vps['ram']}\n"
            f"**CPU:** {vps['cpu']} Core(s)\n"
            f"**Storage:** {vps.get('storage', '30GB')}\n"
            f"**Created:** {vps.get('created_at', '?')[:10]}\n"
            f"**Expires:** {expire_str}"
        )
        if "processor" in vps:
            resource_info += f"\n**Processor:** {vps['processor']}"
        if status == "suspended":
            resource_info += f"\n⚠️ **Suspended:** {vps.get('suspension_reason', 'Unknown reason')}"

        embed.add_field(name="📊 Resources", value=resource_info, inline=False)
        embed.add_field(name="🎮 Controls",  value="Use the buttons below to manage your VPS", inline=False)
        return embed

    def add_action_buttons(self):
        if not self.is_shared and not self.is_admin:
            reinstall_button = discord.ui.Button(label="🔄 Reinstall", style=discord.ButtonStyle.danger)
            reinstall_button.callback = lambda inter: self.action_callback(inter, 'reinstall')
            self.add_item(reinstall_button)

        start_button = discord.ui.Button(label="▶ Start", style=discord.ButtonStyle.success)
        start_button.callback = lambda inter: self.action_callback(inter, 'start')

        stop_button = discord.ui.Button(label="⏸ Stop", style=discord.ButtonStyle.secondary)
        stop_button.callback = lambda inter: self.action_callback(inter, 'stop')

        ssh_button = discord.ui.Button(label="🔑 SSH", style=discord.ButtonStyle.primary)
        ssh_button.callback = lambda inter: self.action_callback(inter, 'ssh')

        self.add_item(start_button)
        self.add_item(stop_button)
        self.add_item(ssh_button)

    async def select_vps(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id and not self.is_admin:
            await interaction.response.send_message(
                embed=create_error_embed("Access Denied", "This is not your VPS!"), ephemeral=True)
            return
        self.selected_index = int(self.select.values[0])
        new_embed = self.create_vps_embed(self.selected_index)
        self.clear_items()
        self.add_action_buttons()
        await interaction.response.edit_message(embed=new_embed, view=self)

    async def action_callback(self, interaction: discord.Interaction, action: str):
        if str(interaction.user.id) != self.user_id and not self.is_admin:
            await interaction.response.send_message(
                embed=create_error_embed("Access Denied", "This is not your VPS!"), ephemeral=True)
            return

        if self.is_shared:
            vps = vps_data[self.owner_id][self.selected_index]
        else:
            vps = self.vps_list[self.selected_index]

        container_name = vps["container_name"]

        # Block start on suspended VPS for regular users
        if vps.get("status") == "suspended" and action == "start" and not self.is_admin:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "VPS Suspended",
                    "Your VPS has been suspended. Contact an admin to unsuspend it."
                ), ephemeral=True)
            return

        if action == 'reinstall':
            if self.is_shared or self.is_admin:
                await interaction.response.send_message(
                    embed=create_error_embed("Access Denied", "Only the VPS owner can reinstall!"),
                    ephemeral=True)
                return

            confirm_embed = create_warning_embed(
                "Reinstall Warning",
                f"⚠️ **WARNING:** This will erase all data on VPS `{container_name}` and reinstall Ubuntu 22.04.\n\n"
                f"This action cannot be undone. Continue?"
            )

            class ConfirmView(discord.ui.View):
                def __init__(self, parent_view, container_name, vps, owner_id, selected_index):
                    super().__init__(timeout=60)
                    self.parent_view    = parent_view
                    self.container_name = container_name
                    self.vps            = vps
                    self.owner_id       = owner_id
                    self.selected_index = selected_index

                @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
                async def confirm(self, interaction: discord.Interaction, item: discord.ui.Button):
                    await interaction.response.defer(ephemeral=True)
                    try:
                        await interaction.followup.send(
                            embed=create_info_embed("Deleting Container", f"Removing `{self.container_name}`..."),
                            ephemeral=True)
                        try:
                            await execute_docker(f"docker stop {self.container_name}")
                        except Exception:
                            pass
                        await execute_docker(f"docker rm -f {self.container_name}")

                        await interaction.followup.send(
                            embed=create_info_embed("Recreating Container", f"Creating new container `{self.container_name}`..."),
                            ephemeral=True)
                        original_ram  = self.vps["ram"]
                        original_cpu  = self.vps["cpu"]
                        original_disk = int(self.vps.get("storage", "30GB").replace("GB", ""))
                        ram_mb        = int(original_ram.replace("GB", "")) * 1024
                        new_password  = generate_password()
                        # Preserve the container's original hostname on reinstall
                        reinstall_hostname = self.vps.get("hostname", f"{VPS_HOSTNAME}-1")
                        await create_docker_container(
                            self.container_name, ram_mb, original_cpu, 0, new_password,
                            disk_gb=original_disk, hostname=reinstall_hostname
                        )
                        self.vps["status"]            = "running"
                        self.vps["ssh_password"]      = new_password
                        self.vps["created_at"]        = datetime.now().isoformat()
                        self.vps.pop("suspension_reason",  None)
                        self.vps.pop("suspension_time",    None)
                        self.vps.pop("suspension_evidence", None)
                        save_data()
                        await interaction.followup.send(
                            embed=create_success_embed(
                                "Reinstall Complete",
                                f"VPS `{self.container_name}` reinstalled successfully!"
                            ), ephemeral=True)
                        _log = discord.Embed(
                            title="🔄  VPS Reinstalled",
                            description=(
                                f"{interaction.user.mention} fully reinstalled VPS `{self.container_name}`.\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                            ),
                            colour=discord.Colour.from_rgb(148, 0, 211),
                            timestamp=datetime.utcnow()
                        )
                        _log.set_author(name="VPS Reinstalled  •  Fresh OS")
                        try:
                            _log.set_thumbnail(url=interaction.user.display_avatar.url)
                        except Exception:
                            pass
                        _log.add_field(name="📦 Container",   value=f"`{self.container_name}`",  inline=True)
                        _log.add_field(name="🏷️ Hostname",     value=f"`{reinstall_hostname}`",   inline=True)
                        _log.add_field(name="👤 Owner",        value=f"<@{self.owner_id}>",       inline=True)
                        _log.add_field(name="🎮 Triggered By", value=interaction.user.mention,    inline=True)
                        _log.add_field(name="🧠 RAM",          value=f"`{original_ram}`",         inline=True)
                        _log.add_field(name="⚡ CPU",              value=f"`{original_cpu}`",         inline=True)
                        _log.add_field(name="💾 Disk",         value=f"`{original_disk} GB`",     inline=True)
                        _log.add_field(name="🔒 Password",     value="`✅ Regenerated`",      inline=True)
                        _log.add_field(name="🌐 Status",       value="`🟢 RUNNING`",      inline=True)
                        _log.add_field(name="⚠️ Note",       value="Previous data has been wiped. New SSH credentials were sent to the owner.", inline=False)
                        _log.set_footer(text="DarkNodes VPS Logs  •  Reinstall")
                        asyncio.create_task(send_log(_log))
                        if not self.parent_view.is_shared:
                            await interaction.message.edit(
                                embed=self.parent_view.create_vps_embed(self.parent_view.selected_index),
                                view=self.parent_view)
                    except Exception as e:
                        await interaction.followup.send(
                            embed=create_error_embed("Reinstall Failed", f"Error: {str(e)}"),
                            ephemeral=True)

                @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
                async def cancel(self, interaction: discord.Interaction, item: discord.ui.Button):
                    await interaction.response.edit_message(
                        embed=self.parent_view.create_vps_embed(self.parent_view.selected_index),
                        view=self.parent_view)

            await interaction.response.send_message(
                embed=confirm_embed,
                view=ConfirmView(self, container_name, vps, self.owner_id, self.selected_index),
                ephemeral=True)

        elif action == 'start':
            await interaction.response.defer(ephemeral=True)
            try:
                await execute_docker(f"docker start {container_name}")
                await docker_exec(container_name, "/usr/sbin/sshd || true", timeout=10)
                vps["status"] = "running"
                save_data()
                await interaction.followup.send(
                    embed=create_success_embed("VPS Started", f"VPS `{container_name}` is now running!"),
                    ephemeral=True)
                await interaction.message.edit(embed=self.create_vps_embed(self.selected_index), view=self)
                _log = discord.Embed(
                    title="▶️  VPS Started",
                    description=(
                        f"{interaction.user.mention} started VPS `{container_name}`.\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    ),
                    colour=discord.Colour.from_rgb(0, 180, 255),
                    timestamp=datetime.utcnow()
                )
                _log.set_author(name="VPS Started  •  Container Online")
                try:
                    _log.set_thumbnail(url=interaction.user.display_avatar.url)
                except Exception:
                    pass
                _log.add_field(name="📦 Container", value=f"`{container_name}`",         inline=True)
                _log.add_field(name="🏷️ Hostname",  value=f"`{vps.get('hostname', 'N/A')}`", inline=True)
                _log.add_field(name="👤 Owner",      value=f"<@{self.owner_id}>",         inline=True)
                _log.add_field(name="🎮 Actor",      value=interaction.user.mention,      inline=True)
                _log.add_field(name="🧠 RAM",        value=f"`{vps.get('ram', 'N/A')}`",  inline=True)
                _log.add_field(name="⚡ CPU",            value=f"`{vps.get('cpu', 'N/A')} Core(s)`", inline=True)
                _log.add_field(name="🌐 Status",     value="`🟢 RUNNING`",        inline=True)
                _log.set_footer(text="DarkNodes VPS Logs  •  Start Event")
                asyncio.create_task(send_log(_log))
            except Exception as e:
                await interaction.followup.send(
                    embed=create_error_embed("Start Failed", str(e)), ephemeral=True)

        elif action == 'stop':
            await interaction.response.defer(ephemeral=True)
            try:
                await execute_docker(f"docker stop {container_name}", timeout=120)
                vps["status"] = "stopped"
                save_data()
                await interaction.followup.send(
                    embed=create_success_embed("VPS Stopped", f"VPS `{container_name}` has been stopped!"),
                    ephemeral=True)
                await interaction.message.edit(embed=self.create_vps_embed(self.selected_index), view=self)
                _log = discord.Embed(
                    title="⏹️  VPS Stopped",
                    description=(
                        f"{interaction.user.mention} stopped VPS `{container_name}`.\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    ),
                    colour=discord.Colour.from_rgb(255, 140, 0),
                    timestamp=datetime.utcnow()
                )
                _log.set_author(name="VPS Stopped  •  Container Offline")
                try:
                    _log.set_thumbnail(url=interaction.user.display_avatar.url)
                except Exception:
                    pass
                _log.add_field(name="📦 Container", value=f"`{container_name}`",         inline=True)
                _log.add_field(name="🏷️ Hostname",  value=f"`{vps.get('hostname', 'N/A')}`", inline=True)
                _log.add_field(name="👤 Owner",      value=f"<@{self.owner_id}>",         inline=True)
                _log.add_field(name="🎮 Actor",      value=interaction.user.mention,      inline=True)
                _log.add_field(name="🧠 RAM",        value=f"`{vps.get('ram', 'N/A')}`",  inline=True)
                _log.add_field(name="⚡ CPU",            value=f"`{vps.get('cpu', 'N/A')} Core(s)`", inline=True)
                _log.add_field(name="🌐 Status",     value="`🔴 STOPPED`",        inline=True)
                _log.set_footer(text="DarkNodes VPS Logs  •  Stop Event")
                asyncio.create_task(send_log(_log))
            except Exception as e:
                await interaction.followup.send(
                    embed=create_error_embed("Stop Failed", str(e)), ephemeral=True)

        elif action == 'ssh':
            await interaction.response.defer(ephemeral=True)
            try:
                ssh_password = vps.get("ssh_password")
                if not ssh_password:
                    await interaction.followup.send(
                        embed=create_error_embed(
                            "SSH Error", "SSH credentials not found. Please reinstall the VPS."
                        ), ephemeral=True)
                    return

                await interaction.followup.send(
                    embed=create_info_embed("🔗 Generating Access Links", "Starting tmate and sshx sessions, please wait…"),
                    ephemeral=True)

                host_ip = await _get_server_ip()
                stored_port = vps.get("ssh_port", 0)

                # ── tmate (optional) ───────────────────────────────────────────
                tmate_info = {}
                tmate_err = ""
                try:
                    tmate_info = await get_tmate_session(container_name)
                    # Persist updated token (SSH only — web URL not exposed)
                    vps["tmate_ssh"] = tmate_info.get("ssh", "")
                    save_data()
                except Exception as _te:
                    tmate_err = str(_te)

                # ── sshx (optional) ────────────────────────────────────────────
                sshx_url = ""
                sshx_err = ""
                try:
                    sshx_url = await get_sshx_session(container_name)
                    vps["sshx_url"] = sshx_url
                    save_data()
                except Exception as _se:
                    sshx_err = str(_se)

                # ── DarkNodes-branded access embed ────────────────────────────
                ssh_embed = discord.Embed(
                    title="🔑  DarkNodes — VPS Access",
                    description=f"Session links for `{container_name}`  •  Generated just now",
                    color=0x5865F2,
                )

                # tmate SSH
                if tmate_info.get("ssh"):
                    ssh_embed.add_field(
                        name="🖥️  tmate SSH",
                        value=f"```{tmate_info['ssh']}```",
                        inline=False,
                    )
                elif tmate_err:
                    ssh_embed.add_field(
                        name="🖥️  tmate SSH",
                        value=f"⚠️ Could not start tmate: `{tmate_err[:120]}`",
                        inline=False,
                    )

                # sshx
                if sshx_url:
                    ssh_embed.add_field(name="🔗  sshx Web", value=sshx_url, inline=False)
                elif sshx_err:
                    ssh_embed.add_field(
                        name="🔗  sshx Web",
                        value=f"⚠️ Could not start sshx: `{sshx_err[:120]}`",
                        inline=False,
                    )

                if not tmate_info.get("ssh") and not sshx_url:
                    ssh_embed.add_field(
                        name="⚠️  No Sessions Available",
                        value="Both tmate and sshx failed. Ensure the VPS is running.",
                        inline=False,
                    )

                ssh_embed.add_field(
                    name="📌  Notes",
                    value=(
                        "• Links refresh each time you click **SSH**\n"
                        "• Sessions are valid until the VPS is stopped or reinstalled\n"
                        "• Use `!manage` to control your VPS"
                    ),
                    inline=False,
                )
                ssh_embed.set_footer(text="DarkNodes VPS  •  Session links are private")

                try:
                    await interaction.user.send(embed=ssh_embed)
                    await interaction.followup.send(
                        embed=create_success_embed("Access Details Sent", "Check your DMs for your session links!"),
                        ephemeral=True)
                except discord.Forbidden:
                    await interaction.followup.send(
                        embed=create_error_embed("DM Failed", "Enable DMs to receive session links!"),
                        ephemeral=True)
            except Exception as e:
                await interaction.followup.send(
                    embed=create_error_embed("SSH Error", str(e)), ephemeral=True)


# ─── Commands ──────────────────────────────────────────────────────────────────

@bot.command(name='create')
@is_admin()
async def create_vps(ctx, user: discord.Member, ram: int, cpu: int, disk: int = 30):
    """Create a custom VPS for a user (Admin only) — !create @user <ram_GB> <cpu_cores> <disk_GB>"""
    if ram <= 0 or cpu <= 0 or disk <= 0:
        await ctx.send(embed=create_error_embed(
            "Invalid Specs",
            "RAM, CPU and Disk must be positive integers.\n"
            "Usage: `!create @user <ram_GB> <cpu_cores> <disk_GB>`"
        ))
        return

    user_id = str(user.id)
    if user_id not in vps_data:
        vps_data[user_id] = []

    vps_count      = len(vps_data[user_id]) + 1
    container_name = f"vps-{user_id}-{vps_count}"
    ram_mb         = ram * 1024
    password       = generate_password()
    vps_hostname   = get_next_vps_hostname()

    progress_msg = await ctx.send(embed=_deploy_progress_embed(0))

    ssh_port = get_next_ssh_port()
    try:
        await create_docker_container(container_name, ram_mb, cpu, ssh_port, password, disk_gb=disk, hostname=vps_hostname, progress_msg=progress_msg)

        vps_info = {
            "container_name": container_name,
            "hostname":       vps_hostname,
            "ssh_port":       ssh_port,
            "ram":            f"{ram}GB",
            "cpu":            str(cpu),
            "storage":        f"{disk}GB",
            "status":         "running",
            "created_at":     datetime.now().isoformat(),
            "expires":        "Never",
            "ssh_password":   password,
            "shared_with":    []
        }
        vps_data[user_id].append(vps_info)
        save_data()

        if ctx.guild:
            vps_role = await get_or_create_vps_role(ctx.guild)
            if vps_role:
                try:
                    await user.add_roles(vps_role, reason="VPS ownership granted")
                except discord.Forbidden:
                    pass

        # success embed is shown by editing progress_msg after access methods are ready

        _log = discord.Embed(
            title="🟢  VPS Deployed",
            description=(
                f"A new VPS was deployed by {ctx.author.mention} for {user.mention}.\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            colour=discord.Colour.from_rgb(0, 220, 110),
            timestamp=datetime.utcnow()
        )
        _log.set_author(name="VPS Created  •  Admin Deploy", icon_url="https://cdn.discordapp.com/emojis/1044924827185127464.webp")
        try:
            _log.set_thumbnail(url=user.display_avatar.url)
        except Exception:
            pass
        _log.add_field(name="📦 Container",   value=f"`{container_name}`",            inline=True)
        _log.add_field(name="🏷️ Hostname",    value=f"`{vps_hostname}`",              inline=True)
        _log.add_field(name="🆔 VPS ID",      value=f"`#{vps_count}`",                inline=True)
        _log.add_field(name="👤 Owner",        value=f"{user.mention}\n`{user.id}`",   inline=True)
        _log.add_field(name="🛠️ Deployed By",  value=f"{ctx.author.mention}",          inline=True)
        _log.add_field(name="📋 Source",       value="`Admin Deploy`",                 inline=True)
        _log.add_field(name="🧠 RAM",          value=f"`{ram} GB`",                    inline=True)
        _log.add_field(name="⚡ CPU",          value=f"`{cpu} Core(s)`",               inline=True)
        _log.add_field(name="💾 Disk",         value=f"`{disk} GB`",                   inline=True)
        _log.add_field(name="🔒 Password Set", value="`✅ Generated`",                  inline=True)
        _log.add_field(name="🌐 Status",       value="`🟢 RUNNING`",                   inline=True)
        _log.add_field(name="📅 Created At",   value=f"`{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}`", inline=True)
        _log.set_footer(text="DarkNodes VPS Logs  •  Admin Deploy")
        asyncio.create_task(send_log(_log))

        # ── Step 5: access methods ────────────────────────────────────────────
        await progress_msg.edit(embed=_deploy_progress_embed(5))

        tmate_info = {}
        try:
            tmate_info = await get_tmate_session(container_name)
            vps_info["tmate_ssh"] = tmate_info.get("ssh", "")
            save_data()
        except Exception as _te:
            logger.warning(f"tmate session failed for {container_name}: {_te}")

        sshx_url = ""
        try:
            sshx_url = await get_sshx_session(container_name)
            vps_info["sshx_url"] = sshx_url
            save_data()
        except Exception as _se:
            logger.warning(f"sshx session failed for {container_name}: {_se}")

        # ── Edit progress_msg to final success embed ───────────────────────────
        await progress_msg.edit(embed=_deploy_success_embed(
            user, vps_count, container_name, str(ram), str(cpu), str(disk)
        ))

        # ── DM the owner ──────────────────────────────────────────────────────
        try:
            await user.send(embed=_vps_dm_embed(
                vps_count, container_name, f"{ram} GB", str(cpu),
                tmate_ssh=tmate_info.get("ssh", ""),
                sshx_url=sshx_url,
            ))
        except discord.Forbidden:
            pass

    except Exception as e:
        # Remove partial vps_data entry on failure
        if user_id in vps_data and vps_data[user_id]:
            last = vps_data[user_id][-1]
            if last.get("container_name") == container_name:
                vps_data[user_id].pop()
                save_data()
        # Attempt to clean up Docker container
        try:
            await execute_docker(f"docker rm -f {container_name}", timeout=15)
        except Exception:
            pass
        logger.error(f"VPS creation failed for {container_name}: {e}")
        await progress_msg.edit(embed=_deploy_progress_embed(0, failed=True))
        await ctx.send(embed=create_error_embed("Deployment Failed", f"{str(e)[:300]}"))


@bot.command(name='manage')
async def manage_vps(ctx, user: discord.Member = None):
    """Manage your VPS or another user's VPS (Admin only)"""
    if user:
        if not (str(ctx.author.id) == str(MAIN_ADMIN_ID) or str(ctx.author.id) in admin_data.get("admins", [])):
            await ctx.send(embed=create_error_embed("Access Denied", "Only admins can manage other users' VPS."))
            return
        user_id  = str(user.id)
        vps_list = vps_data.get(user_id, [])
        if not vps_list:
            await ctx.send(embed=create_error_embed("No VPS Found", f"{user.mention} doesn't have any VPS."))
            return
        view = ManageView(str(ctx.author.id), vps_list, is_admin=True, owner_id=user_id)
        await ctx.send(embed=create_info_embed(f"Managing {user.name}'s VPS", f"Managing VPS for {user.mention}"), view=view)
    else:
        user_id  = str(ctx.author.id)
        vps_list = vps_data.get(user_id, [])
        if not vps_list:
            embed = create_embed("No VPS Found", "You don't have any VPS. Use `.buywc` to purchase one.", 0xff3366)
            embed.add_field(name="Quick Actions",
                            value="• `!plans` - View plans\n• `!buywc <plan> <processor>` - Purchase VPS",
                            inline=False)
            await ctx.send(embed=embed)
            return
        view = ManageView(user_id, vps_list)
        await ctx.send(embed=view.initial_embed, view=view)


@bot.command(name='delete-vps')
@is_admin()
async def delete_vps(ctx, user: discord.Member, vps_number: int, *, reason: str = "No reason"):
    """Delete a user's VPS (Admin only)"""
    user_id = str(user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number or user doesn't have a VPS."))
        return

    vps            = vps_data[user_id][vps_number - 1]
    container_name = vps["container_name"]
    await ctx.send(embed=create_info_embed("Deleting VPS", f"Removing VPS #{vps_number}..."))

    try:
        try:
            await execute_docker(f"docker stop {container_name}")
        except Exception:
            pass
        await execute_docker(f"docker rm -f {container_name}")
        del vps_data[user_id][vps_number - 1]
        if not vps_data[user_id]:
            del vps_data[user_id]
            if ctx.guild:
                vps_role = await get_or_create_vps_role(ctx.guild)
                if vps_role and vps_role in user.roles:
                    try:
                        await user.remove_roles(vps_role, reason="No VPS ownership")
                    except discord.Forbidden:
                        pass
        save_data()
        embed = create_success_embed("VPS Deleted Successfully")
        embed.add_field(name="Owner",     value=user.mention,          inline=True)
        embed.add_field(name="VPS ID",    value=f"#{vps_number}",      inline=True)
        embed.add_field(name="Container", value=f"`{container_name}`", inline=True)
        embed.add_field(name="Reason",    value=reason,                inline=False)
        await ctx.send(embed=embed)

        _log = discord.Embed(
            title="🔴  VPS Deleted",
            description=(
                f"{ctx.author.mention} deleted VPS **#{vps_number}** belonging to {user.mention}.\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            colour=discord.Colour.from_rgb(220, 50, 50),
            timestamp=datetime.utcnow()
        )
        _log.set_author(name="VPS Deleted  •  Permanent Action")
        try:
            _log.set_thumbnail(url=user.display_avatar.url)
        except Exception:
            pass
        _log.add_field(name="📦 Container",  value=f"`{container_name}`",             inline=True)
        _log.add_field(name="🏷️ Hostname",   value=f"`{vps.get('hostname','?')}`",   inline=True)
        _log.add_field(name="🆔 VPS ID",     value=f"`#{vps_number}`",                inline=True)
        _log.add_field(name="👤 Owner",       value=f"{user.mention}\n`{user.id}`",   inline=True)
        _log.add_field(name="🛠️ Deleted By", value=ctx.author.mention,           inline=True)
        _log.add_field(name="🧠 RAM",         value=f"`{vps.get('ram','?')}`",         inline=True)
        _log.add_field(name="⚡ CPU",             value=f"`{vps.get('cpu','?')} Core(s)`", inline=True)
        _log.add_field(name="💾 Storage",     value=f"`{vps.get('storage','?')}`",     inline=True)
        _log.add_field(name="🌐 Final Status",value="`🗑️ DELETED`",      inline=True)
        _log.add_field(name="📝 Reason",      value=f"```{reason}```",                inline=False)
        _log.set_footer(text="DarkNodes VPS Logs  •  Delete Event")
        asyncio.create_task(send_log(_log))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Deletion Failed", f"Error: {str(e)}"))


@bot.command(name='list-all')
@is_admin()
async def list_all_vps(ctx):
    """List all VPS and user information (Admin only)"""
    embed        = create_embed("All VPS Information", "Complete overview of all VPS deployments", 0x1a1a1a)
    total_vps    = 0
    running_vps  = 0
    stopped_vps  = 0
    suspended_vps = 0
    vps_info     = []
    user_summary = []

    for user_id, vps_list in vps_data.items():
        try:
            user            = await bot.fetch_user(int(user_id))
            user_vps_count  = len(vps_list)
            user_running    = sum(1 for vps in vps_list if vps.get('status') == 'running')
            user_suspended  = sum(1 for vps in vps_list if vps.get('status') == 'suspended')
            total_vps      += user_vps_count
            running_vps    += user_running
            stopped_vps    += user_vps_count - user_running - user_suspended
            suspended_vps  += user_suspended
            user_summary.append(f"**{user.name}** ({user.mention}) - {user_vps_count} VPS ({user_running} running, {user_suspended} suspended)")
            for i, vps in enumerate(vps_list):
                status = vps.get('status', 'unknown')
                status_emoji = "🟢" if status == 'running' else ("🟠" if status == 'suspended' else "🔴")
                vps_info.append(
                    f"{status_emoji} **{user.name}** - VPS {i+1}: `{vps['container_name']}` "
                    f"Port:{vps.get('ssh_port','?')} - {status.upper()}"
                )
        except discord.NotFound:
            vps_info.append(f"❓ Unknown User ({user_id}) - {len(vps_list)} VPS")

    embed.add_field(
        name="System Overview",
        value=(
            f"**Total Users:** {len(vps_data)}\n"
            f"**Total VPS:** {total_vps}\n"
            f"**Running:** {running_vps}\n"
            f"**Stopped:** {stopped_vps}\n"
            f"**Suspended:** {suspended_vps}"
        ),
        inline=False
    )
    if user_summary:
        embed.add_field(name="User Summary", value="\n".join(user_summary[:10]), inline=False)
    if vps_info:
        for i in range(0, min(len(vps_info), 30), 15):
            chunk = vps_info[i:i+15]
            embed.add_field(
                name=f"VPS Deployments ({i+1}-{min(i+15, len(vps_info))})",
                value="\n".join(chunk), inline=False
            )
    await ctx.send(embed=embed)


@bot.command(name='manage-shared')
async def manage_shared_vps(ctx, owner: discord.Member, vps_number: int):
    """Manage a shared VPS"""
    owner_id = str(owner.id)
    user_id  = str(ctx.author.id)
    if owner_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[owner_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number."))
        return
    vps = vps_data[owner_id][vps_number - 1]
    if user_id not in vps.get("shared_with", []):
        await ctx.send(embed=create_error_embed("Access Denied", "You do not have access to this VPS."))
        return
    view = ManageView(user_id, [vps], is_shared=True, owner_id=owner_id)
    await ctx.send(embed=view.initial_embed, view=view)


@bot.command(name='share-user')
async def share_user(ctx, shared_user: discord.Member, vps_number: int):
    """Share VPS access with another user"""
    user_id        = str(ctx.author.id)
    shared_user_id = str(shared_user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number."))
        return
    vps = vps_data[user_id][vps_number - 1]
    if "shared_with" not in vps:
        vps["shared_with"] = []
    if shared_user_id in vps["shared_with"]:
        await ctx.send(embed=create_error_embed("Already Shared", f"{shared_user.mention} already has access!"))
        return
    vps["shared_with"].append(shared_user_id)
    save_data()
    await ctx.send(embed=create_success_embed("VPS Shared", f"VPS #{vps_number} shared with {shared_user.mention}!"))
    try:
        await shared_user.send(embed=create_embed(
            "VPS Access Granted",
            f"You have access to VPS #{vps_number} from {ctx.author.mention}. "
            f"Use `!manage-shared {ctx.author.mention} {vps_number}`",
            0x00ff88
        ))
    except discord.Forbidden:
        pass


@bot.command(name='share-ruser')
async def revoke_share(ctx, shared_user: discord.Member, vps_number: int):
    """Revoke shared VPS access"""
    user_id        = str(ctx.author.id)
    shared_user_id = str(shared_user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number."))
        return
    vps = vps_data[user_id][vps_number - 1]
    if "shared_with" not in vps or shared_user_id not in vps["shared_with"]:
        await ctx.send(embed=create_error_embed("Not Shared", f"{shared_user.mention} doesn't have access!"))
        return
    vps["shared_with"].remove(shared_user_id)
    save_data()
    await ctx.send(embed=create_success_embed(
        "Access Revoked",
        f"Access to VPS #{vps_number} revoked from {shared_user.mention}!"
    ))


@bot.command(name='buywc')
async def buy_with_credits(ctx, plan: str, processor: str = "Intel"):
    """Buy a VPS with credits"""
    user_id = str(ctx.author.id)
    prices = {
        "Starter":  {"Intel": 42,  "AMD": 83},
        "Basic":    {"Intel": 96,  "AMD": 164},
        "Standard": {"Intel": 192, "AMD": 320},
        "Pro":      {"Intel": 220, "AMD": 340}
    }
    plans = {
        "Starter":  {"ram": "4GB",  "cpu": "1", "storage": "10GB"},
        "Basic":    {"ram": "8GB",  "cpu": "1", "storage": "10GB"},
        "Standard": {"ram": "12GB", "cpu": "2", "storage": "10GB"},
        "Pro":      {"ram": "16GB", "cpu": "2", "storage": "10GB"}
    }
    if plan not in prices:
        await ctx.send(embed=create_error_embed("Invalid Plan", "Available: Starter, Basic, Standard, Pro"))
        return
    if processor not in ["Intel", "AMD"]:
        await ctx.send(embed=create_error_embed("Invalid Processor", "Choose: Intel or AMD"))
        return

    cost = prices[plan][processor]
    if user_id not in user_data:
        user_data[user_id] = {"credits": 0}
    if user_data[user_id]["credits"] < cost:
        await ctx.send(embed=create_error_embed(
            "Insufficient Credits",
            f"You need {cost} credits but have {user_data[user_id]['credits']}"
        ))
        return

    user_data[user_id]["credits"] -= cost
    if user_id not in vps_data:
        vps_data[user_id] = []

    vps_count      = len(vps_data[user_id]) + 1
    container_name = f"vps-{user_id}-{vps_count}"
    ram_str        = plans[plan]["ram"]
    cpu_str        = plans[plan]["cpu"]
    ram_mb         = int(ram_str.replace("GB", "")) * 1024
    password       = generate_password()
    vps_hostname   = get_next_vps_hostname()

    progress_msg = await ctx.send(embed=_deploy_progress_embed(0))

    ssh_port = get_next_ssh_port()
    try:
        await create_docker_container(container_name, ram_mb, cpu_str, ssh_port, password, hostname=vps_hostname, progress_msg=progress_msg)

        vps_info = {
            "plan":           plan,
            "container_name": container_name,
            "hostname":       vps_hostname,
            "ssh_port":       ssh_port,
            "ram":            ram_str,
            "cpu":            cpu_str,
            "storage":        plans[plan]["storage"],
            "status":         "running",
            "created_at":     datetime.now().isoformat(),
            "processor":      processor,
            "ssh_password":   password,
            "shared_with":    []
        }
        vps_data[user_id].append(vps_info)
        save_data()

        if ctx.guild:
            vps_role = await get_or_create_vps_role(ctx.guild)
            if vps_role:
                try:
                    await ctx.author.add_roles(vps_role, reason="VPS purchase completed")
                except discord.Forbidden:
                    pass

        embed = create_success_embed("VPS Purchased Successfully")
        embed.add_field(name="Plan",      value=f"**{plan}** ({processor})", inline=True)
        embed.add_field(name="VPS ID",    value=f"#{vps_count}",             inline=True)
        embed.add_field(name="Container", value=f"`{container_name}`",       inline=True)
        embed.add_field(name="Cost",      value=f"{cost} credits",           inline=True)
        embed.add_field(name="Resources",
                        value=f"**RAM:** {ram_str}\n**CPU:** {cpu_str} Cores\n**Storage:** 10GB",
                        inline=False)
        await ctx.send(embed=embed)

        _log = discord.Embed(
            title="🟢  VPS Deployed",
            description=(
                f"{ctx.author.mention} purchased a **{plan}** ({processor}) VPS.\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            colour=discord.Colour.from_rgb(0, 220, 110),
            timestamp=datetime.utcnow()
        )
        _log.set_author(name="VPS Created  •  User Purchase")
        try:
            _log.set_thumbnail(url=ctx.author.display_avatar.url)
        except Exception:
            pass
        _log.add_field(name="📦 Container",  value=f"`{container_name}`",                 inline=True)
        _log.add_field(name="🏷️ Hostname",   value=f"`{vps_hostname}`",                   inline=True)
        _log.add_field(name="🆔 VPS ID",     value=f"`#{vps_count}`",                     inline=True)
        _log.add_field(name="👤 Owner",       value=f"{ctx.author.mention}\n`{user_id}`",  inline=True)
        _log.add_field(name="📋 Plan",        value=f"`{plan}` ({processor})",             inline=True)
        _log.add_field(name="💳 Cost",        value=f"`{cost} credits`",                   inline=True)
        _log.add_field(name="🧠 RAM",         value=f"`{ram_str}`",                        inline=True)
        _log.add_field(name="⚡ CPU",         value=f"`{cpu_str} Cores`",                  inline=True)
        _log.add_field(name="💾 Storage",     value=f"`{plans[plan]['storage']}`",         inline=True)
        _log.add_field(name="🌐 Status",      value="`🟢 RUNNING`",                        inline=True)
        _log.add_field(name="📅 Created At",  value=f"`{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}`", inline=True)
        _log.add_field(name="🔒 Password Set",value="`✅ Generated`",                       inline=True)
        _log.set_footer(text="DarkNodes VPS Logs  •  User Purchase")
        asyncio.create_task(send_log(_log))

        # ── Step 5: access methods ────────────────────────────────────────────
        await progress_msg.edit(embed=_deploy_progress_embed(5))

        tmate_info = {}
        try:
            tmate_info = await get_tmate_session(container_name)
            vps_info["tmate_ssh"] = tmate_info.get("ssh", "")
            save_data()
        except Exception as _te:
            logger.warning(f"tmate session failed for {container_name}: {_te}")

        sshx_url = ""
        try:
            sshx_url = await get_sshx_session(container_name)
            vps_info["sshx_url"] = sshx_url
            save_data()
        except Exception as _se:
            logger.warning(f"sshx session failed for {container_name}: {_se}")

        # ── Edit progress_msg to final success embed ───────────────────────────
        await progress_msg.edit(embed=_deploy_success_embed(
            ctx.author, vps_count, container_name, ram_str, cpu_str, plans[plan]["storage"]
        ))

        # ── DM the buyer ──────────────────────────────────────────────────────
        try:
            await ctx.author.send(embed=_vps_dm_embed(
                vps_count, container_name, ram_str, cpu_str,
                tmate_ssh=tmate_info.get("ssh", ""),
                sshx_url=sshx_url,
                plan=plan,
                processor=processor,
            ))
        except discord.Forbidden:
            pass

    except Exception as e:
        # Refund credits on failure
        user_data[user_id]["credits"] += cost
        # Remove partial vps_data entry
        if user_id in vps_data and vps_data[user_id]:
            last = vps_data[user_id][-1]
            if last.get("container_name") == container_name:
                vps_data[user_id].pop()
        save_data()
        try:
            await execute_docker(f"docker rm -f {container_name}", timeout=15)
        except Exception:
            pass
        await ctx.send(embed=create_error_embed("Purchase Failed", f"Error: {str(e)}\n\nCredits refunded."))


@bot.command(name='buyc')
async def buy_credits(ctx):
    """Get payment information"""
    embed = create_embed("💳 Purchase Credits", "Choose your payment method below:", 0x1a1a1a)
    embed.add_field(name="🇮🇳 UPI",    value="```\nContact admin for UPI details\n```",    inline=False)
    embed.add_field(name="💰 PayPal",  value="```\nContact admin for PayPal details\n```", inline=False)
    embed.add_field(name="₿ Crypto",  value="BTC, ETH, USDT accepted",                    inline=False)
    embed.add_field(name="📋 Next Steps",
                    value="1. Pay\n2. Contact admin with transaction ID\n3. Receive credits",
                    inline=False)
    try:
        await ctx.author.send(embed=embed)
        await ctx.send(embed=create_success_embed("Information Sent", "Payment details sent to your DMs!"))
    except discord.Forbidden:
        await ctx.send(embed=create_error_embed("DM Failed", "Enable DMs to receive payment info!"))


@bot.command(name='plans')
async def show_plans(ctx):
    """Show available VPS plans"""
    embed = create_embed("💎 VPS Plans - DarkNodes", "Choose your perfect VPS plan:", 0x1a1a1a)
    plans_info = [
        ("🥉 Starter",  "**RAM:** 4GB\n**CPU:** 1 Core\n**Storage:** 10GB\n**Intel:** 42 credits\n**AMD:** 83 credits"),
        ("🥈 Basic",    "**RAM:** 8GB\n**CPU:** 1 Core\n**Storage:** 10GB\n**Intel:** 96 credits\n**AMD:** 164 credits"),
        ("🥇 Standard", "**RAM:** 12GB\n**CPU:** 2 Cores\n**Storage:** 10GB\n**Intel:** 192 credits\n**AMD:** 320 credits"),
        ("💎 Pro",      "**RAM:** 16GB\n**CPU:** 2 Cores\n**Storage:** 10GB\n**Intel:** 220 credits\n**AMD:** 340 credits"),
    ]
    for name, value in plans_info:
        embed.add_field(name=name, value=value, inline=True)
    embed.add_field(name="How to Buy",
                    value="Use `!buywc <plan> <Intel/AMD>` to purchase\nUse `!buyc` for payment info",
                    inline=False)
    await ctx.send(embed=embed)


@bot.command(name='credits')
async def check_credits(ctx):
    """Check your credit balance"""
    user_id = str(ctx.author.id)
    if user_id not in user_data:
        user_data[user_id] = {"credits": 0}
        save_data()
    credits = user_data[user_id].get("credits", 0)
    embed = create_info_embed("💰 Credit Balance", f"{ctx.author.mention}, you have **{credits}** credits.")
    await ctx.send(embed=embed)


@bot.command(name='adminc')
@is_admin()
async def admin_add_credits(ctx, user: discord.Member, amount: int):
    """Add credits to a user (Admin only)"""
    user_id = str(user.id)
    if user_id not in user_data:
        user_data[user_id] = {"credits": 0}
    user_data[user_id]["credits"] += amount
    save_data()
    await ctx.send(embed=create_success_embed(
        "Credits Added",
        f"Added **{amount}** credits to {user.mention}. "
        f"New balance: **{user_data[user_id]['credits']}**"
    ))


@bot.command(name='adminrc')
@is_admin()
async def admin_remove_credits(ctx, user: discord.Member, amount: str):
    """Remove credits from a user (Admin only). Use 'all' to remove all."""
    user_id = str(user.id)
    if user_id not in user_data:
        user_data[user_id] = {"credits": 0}
    if amount.lower() == "all":
        removed = user_data[user_id]["credits"]
        user_data[user_id]["credits"] = 0
    else:
        removed = int(amount)
        user_data[user_id]["credits"] = max(0, user_data[user_id]["credits"] - removed)
    save_data()
    await ctx.send(embed=create_success_embed(
        "Credits Removed",
        f"Removed **{removed}** credits from {user.mention}. "
        f"New balance: **{user_data[user_id]['credits']}**"
    ))


@bot.command(name='userinfo')
@is_admin()
async def user_info(ctx, user: discord.Member):
    """Get detailed user information (Admin only)"""
    user_id = str(user.id)
    credits = user_data.get(user_id, {}).get("credits", 0)
    embed   = create_embed(f"👤 User Info - {user.name}", "", 0x1a1a1a)
    embed.add_field(name="User",       value=f"{user.mention}\n**ID:** {user.id}", inline=False)
    embed.add_field(name="💰 Credits", value=f"**{credits}**",                     inline=True)
    vps_list = vps_data.get(user_id, [])
    if vps_list:
        vps_text = "\n".join([
            f"VPS {i+1}: `{v['container_name']}` | Port:{v.get('ssh_port','?')} | {v.get('status','?').upper()}"
            for i, v in enumerate(vps_list)
        ])
        embed.add_field(name="🖥️ VPS", value=vps_text, inline=False)
    else:
        embed.add_field(name="🖥️ VPS", value="No VPS owned", inline=False)
    is_admin_user = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
    embed.add_field(name="🛡️ Admin", value="Yes" if is_admin_user else "No", inline=True)
    await ctx.send(embed=embed)


@bot.command(name='serverstats')
@is_admin()
async def server_stats(ctx):
    """Show server statistics (Admin only)"""
    total_vps   = sum(len(v) for v in vps_data.values())
    running_vps = sum(1 for vl in vps_data.values() for v in vl if v.get('status') == 'running')
    susp_vps    = sum(1 for vl in vps_data.values() for v in vl if v.get('status') == 'suspended')
    total_credits = sum(u.get('credits', 0) for u in user_data.values())
    total_ram   = sum(int(v['ram'].replace('GB', '')) for vl in vps_data.values() for v in vl)
    total_cpu   = sum(int(v['cpu']) for vl in vps_data.values() for v in vl)

    embed = create_embed("📊 Server Statistics", "Current server overview", 0x1a1a1a)
    embed.add_field(name="👥 Users",    value=f"**Total:** {len(user_data)}\n**Admins:** {len(admin_data.get('admins', [])) + 1}", inline=False)
    embed.add_field(name="🖥️ VPS",     value=f"**Total:** {total_vps}\n**Running:** {running_vps}\n**Stopped:** {total_vps - running_vps - susp_vps}\n**Suspended:** {susp_vps}", inline=False)
    embed.add_field(name="💰 Economy", value=f"**Total Credits:** {total_credits}", inline=False)
    embed.add_field(name="📈 Resources", value=f"**Total RAM:** {total_ram}GB\n**Total CPU:** {total_cpu} cores", inline=False)
    await ctx.send(embed=embed)


@bot.command(name='vpsinfo')
@is_admin()
async def vps_info(ctx, container_name: str = None):
    """Get VPS information (Admin only)"""
    if not container_name:
        all_vps = []
        for uid, vl in vps_data.items():
            try:
                u = await bot.fetch_user(int(uid))
                for i, v in enumerate(vl):
                    all_vps.append(
                        f"**{u.name}** - VPS {i+1}: `{v['container_name']}` "
                        f"Port:{v.get('ssh_port','?')} - {v.get('status','?').upper()}"
                    )
            except Exception:
                pass
        embed = create_embed("🖥️ All VPS", f"Total: {len(all_vps)}", 0x1a1a1a)
        for i in range(0, len(all_vps), 20):
            embed.add_field(name=f"VPS List ({i+1}-{i+20})", value="\n".join(all_vps[i:i+20]), inline=False)
        await ctx.send(embed=embed)
    else:
        found_vps  = None
        found_user = None
        for uid, vl in vps_data.items():
            for v in vl:
                if v['container_name'] == container_name:
                    found_vps  = v
                    found_user = await bot.fetch_user(int(uid))
                    break
            if found_vps:
                break
        if not found_vps:
            await ctx.send(embed=create_error_embed("Not Found", f"No VPS with name `{container_name}`"))
            return
        embed = create_embed(f"🖥️ VPS - {container_name}", f"Owned by {found_user.mention}", 0x1a1a1a)
        embed.add_field(name="Specs",    value=f"**RAM:** {found_vps['ram']}\n**CPU:** {found_vps['cpu']} Cores", inline=True)
        embed.add_field(name="Status",   value=f"**{found_vps.get('status','?').upper()}**",                       inline=True)
        embed.add_field(name="SSH Port", value=f"`{found_vps.get('ssh_port','N/A')}`",                            inline=True)
        embed.add_field(name="Created",  value=found_vps.get('created_at', 'Unknown'),                            inline=False)
        if found_vps.get("suspension_reason"):
            embed.add_field(name="⚠️ Suspension", value=found_vps["suspension_reason"], inline=False)
        await ctx.send(embed=embed)


@bot.command(name='restart-vps')
@is_admin()
async def restart_vps(ctx, container_name: str):
    """Restart a VPS (Admin only)"""
    await ctx.send(embed=create_info_embed("Restarting VPS", f"Restarting `{container_name}`..."))
    try:
        await execute_docker(f"docker restart {container_name}")
        await asyncio.sleep(3)
        await docker_exec(container_name, "/usr/sbin/sshd || true", timeout=10)
        for vl in vps_data.values():
            for v in vl:
                if v['container_name'] == container_name:
                    v['status'] = 'running'
                    save_data()
                    break
        await ctx.send(embed=create_success_embed("VPS Restarted", f"`{container_name}` restarted successfully!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Restart Failed", str(e)))


@bot.command(name='backup-vps')
@is_admin()
async def backup_vps(ctx, container_name: str):
    """Create a Docker image snapshot of a VPS (Admin only)"""
    snapshot_name = f"{container_name}-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    await ctx.send(embed=create_info_embed("Creating Backup", f"Committing snapshot of `{container_name}`..."))
    try:
        await execute_docker(f"docker commit {container_name} {snapshot_name}")
        await ctx.send(embed=create_success_embed("Backup Created", f"Image `{snapshot_name}` created!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Backup Failed", str(e)))


@bot.command(name='restore-vps')
@is_admin()
async def restore_vps(ctx, container_name: str, snapshot_name: str):
    """Restore a VPS from a Docker snapshot image (Admin only)"""
    await ctx.send(embed=create_info_embed("Restoring VPS", f"Restoring `{container_name}` from `{snapshot_name}`..."))
    try:
        found_vps = None
        for vl in vps_data.values():
            for v in vl:
                if v['container_name'] == container_name:
                    found_vps = v
                    break
        if not found_vps:
            await ctx.send(embed=create_error_embed("Not Found", f"No VPS data for `{container_name}`"))
            return

        try:
            await execute_docker(f"docker stop {container_name}")
        except Exception:
            pass
        await execute_docker(f"docker rm -f {container_name}")

        ssh_port = found_vps.get("ssh_port", get_next_ssh_port())
        # Snapshots are committed from darknodes-vps containers whose CMD is
        # /lib/systemd/systemd, so we do NOT override it with sleep infinity here.
        # Reuse existing DinD volumes for the restored container
        _rvol_docker = f"{container_name}-docker"
        _rvol_home   = f"{container_name}-home"
        _rvol_root   = f"{container_name}-root"
        _rvol_opt    = f"{container_name}-opt"
        run_cmd = (
            f"docker run -d "
            f"--name {container_name} "
            f"--hostname {found_vps.get('hostname', VPS_HOSTNAME)} "
            f"-p {ssh_port}:22 "
            f"--restart=unless-stopped "
            f"--privileged "
            f"--cgroupns=host "
            f"--tmpfs /run:exec,mode=755,size=256m "
            f"--tmpfs /run/lock:size=64m "
            f"--tmpfs /tmp:exec,size=512m "
            f"-v {_rvol_docker}:/var/lib/docker "
            f"-v {_rvol_home}:/home "
            f"-v {_rvol_root}:/root "
            f"-v {_rvol_opt}:/opt "
            f"-e container=docker "
            f"{snapshot_name}"
        )
        await execute_docker(run_cmd)
        # Give systemd + dockerd time to start before checking SSH
        await asyncio.sleep(10)
        # Ensure SSH is running (dockerd wait handled by systemd)
        await docker_exec(
            container_name,
            "systemctl restart ssh 2>/dev/null || /usr/sbin/sshd || true",
            timeout=20
        )
        found_vps["status"] = "running"
        found_vps.pop("suspension_reason",  None)
        found_vps.pop("suspension_time",    None)
        found_vps.pop("suspension_evidence", None)
        save_data()
        await ctx.send(embed=create_success_embed("VPS Restored", f"`{container_name}` restored from `{snapshot_name}`!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Restore Failed", str(e)))


@bot.command(name='list-snapshots')
@is_admin()
async def list_snapshots(ctx, container_name: str):
    """List Docker image snapshots for a VPS (Admin only)"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "images", "--format", "{{.Repository}}:{{.Tag}} ({{.Size}})",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        all_images = stdout.decode().strip().split('\n')
        snapshots  = [img for img in all_images if img.startswith(container_name + "-backup-")]
        if snapshots:
            embed = create_embed(f"📸 Snapshots for {container_name}", f"Found {len(snapshots)} snapshots", 0x1a1a1a)
            embed.add_field(name="Snapshots", value="\n".join([f"• `{s}`" for s in snapshots]), inline=False)
        else:
            embed = create_info_embed("No Snapshots", f"No snapshots found for `{container_name}`")
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Error", str(e)))


@bot.command(name='exec')
@is_admin()
async def execute_command(ctx, container_name: str, *, command: str):
    """Execute a command inside a VPS container (Admin only)"""
    await ctx.send(embed=create_info_embed("Executing Command", f"Running in `{container_name}`..."))
    try:
        stdout, stderr, rc = await docker_exec(container_name, command, timeout=30)
        embed = create_embed(f"Command Output - {container_name}", f"Command: `{command}`", 0x1a1a1a)
        if stdout:
            out = stdout[:1000] + "\n...(truncated)" if len(stdout) > 1000 else stdout
            embed.add_field(name="📤 Output",    value=f"```\n{out}\n```", inline=False)
        if stderr:
            err = stderr[:1000] + "\n...(truncated)" if len(stderr) > 1000 else stderr
            embed.add_field(name="⚠️ Stderr",   value=f"```\n{err}\n```", inline=False)
        embed.add_field(name="🔄 Exit Code", value=f"**{rc}**",            inline=False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Execution Failed", str(e)))


@bot.command(name='stop-vps-all')
@is_admin()
async def stop_all_vps(ctx):
    """Stop all VPS containers (Admin only)"""
    await ctx.send(embed=create_warning_embed("Stopping All VPS",
                                              "⚠️ This will stop ALL running VPS. Continue?"))

    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)

        @discord.ui.button(label="Stop All VPS", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, item: discord.ui.Button):
            await interaction.response.defer()
            stopped_count = 0
            errors        = []
            for vl in vps_data.values():
                for v in vl:
                    if v.get('status') == 'running':
                        try:
                            proc = await asyncio.create_subprocess_exec(
                                "docker", "stop", v['container_name'],
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                            )
                            await proc.communicate()
                            v['status'] = 'stopped'
                            stopped_count += 1
                        except Exception as e:
                            errors.append(str(e))
            save_data()
            embed = create_success_embed("All VPS Stopped", f"Stopped **{stopped_count}** containers.")
            if errors:
                embed.add_field(name="Errors", value="\n".join(errors[:5]), inline=False)
            await interaction.followup.send(embed=embed)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, item: discord.ui.Button):
            await interaction.response.edit_message(embed=create_info_embed("Cancelled", "Operation cancelled."))

    await ctx.send(view=ConfirmView())


@bot.command(name='cpu-monitor')
@is_admin()
async def cpu_monitor_control(ctx, action: str = "status"):
    """Control abuse monitoring (Admin only) — replaces legacy CPU-only monitor"""
    global MONITOR_CONFIG
    if action.lower() == "status":
        enabled = MONITOR_CONFIG.get("auto_suspend_enabled", True)
        embed   = create_embed(
            "🛡️ Abuse Monitor Status",
            f"Status: **{'Active' if enabled else 'Disabled'}**",
            0x00ccff if enabled else 0xffaa00
        )
        embed.add_field(name="CPU Threshold",       value=f"{MONITOR_CONFIG.get('cpu_threshold', 95)}%",     inline=True)
        embed.add_field(name="Sustained Duration",  value=f"{MONITOR_CONFIG.get('sustained_duration_minutes', 20)}min", inline=True)
        embed.add_field(name="Suspend Threshold",   value=f"Score ≥ {MONITOR_CONFIG.get('auto_suspend_threshold', 70)}", inline=True)
        embed.add_field(name="Scan Interval",       value=f"{MONITOR_CONFIG.get('monitoring_interval', 120)}s", inline=True)
        embed.add_field(name="Suspensions Logged",  value=str(len(suspension_log)),                           inline=True)
        await ctx.send(embed=embed)
    elif action.lower() == "enable":
        MONITOR_CONFIG["auto_suspend_enabled"] = True
        save_mining_config()
        await ctx.send(embed=create_success_embed("Abuse Monitor Enabled", "Automatic suspension is now active."))
    elif action.lower() == "disable":
        MONITOR_CONFIG["auto_suspend_enabled"] = False
        save_mining_config()
        await ctx.send(embed=create_warning_embed("Abuse Monitor Disabled", "Automatic suspension is now OFF."))
    else:
        await ctx.send(embed=create_error_embed("Invalid Action", "Use: `!cpu-monitor <status|enable|disable>`"))


@bot.command(name='admin-add')
@is_main_admin()
async def admin_add(ctx, user: discord.Member):
    user_id = str(user.id)
    if user_id not in admin_data["admins"]:
        admin_data["admins"].append(user_id)
        save_data()
    await ctx.send(embed=create_success_embed("Admin Added", f"{user.mention} is now an admin."))


@bot.command(name='admin-remove')
@is_main_admin()
async def admin_remove(ctx, user: discord.Member):
    user_id = str(user.id)
    if user_id in admin_data["admins"]:
        admin_data["admins"].remove(user_id)
        save_data()
    await ctx.send(embed=create_success_embed("Admin Removed", f"{user.mention} is no longer an admin."))


@bot.command(name='admin-list')
@is_main_admin()
async def admin_list(ctx):
    admins = []
    for aid in admin_data.get("admins", []):
        try:
            u = await bot.fetch_user(int(aid))
            admins.append(f"• {u.mention} ({u.name})")
        except Exception:
            admins.append(f"• Unknown ({aid})")
    embed = create_info_embed("Admin List", "\n".join(admins) if admins else "No admins")
    await ctx.send(embed=embed)


# ─── Anti-mining admin commands ───────────────────────────────────────────────

@bot.command(name='vps-suspend')
@is_admin()
async def vps_suspend(ctx, user: discord.Member, vps_number: int, *, reason: str = "Manual admin suspension"):
    """Manually suspend a VPS (Admin only) — !vps-suspend @user <vps#> [reason]"""
    user_id  = str(user.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Not Found", f"No VPS #{vps_number} for {user.mention}."))
        return

    vps            = vps_list[vps_number - 1]
    container_name = vps["container_name"]

    await ctx.send(embed=create_info_embed("Suspending VPS", f"Stopping and suspending `{container_name}`..."))
    try:
        await execute_docker(f"docker stop --time=5 {container_name}", timeout=30)
    except Exception:
        pass

    vps["status"]            = "suspended"
    vps["suspension_reason"] = reason
    vps["suspension_time"]   = datetime.utcnow().isoformat()
    save_data()

    log_entry = {
        "timestamp":        datetime.utcnow().isoformat(),
        "container_name":   container_name,
        "owner_user_id":    user_id,
        "type":             "manual",
        "reason":           reason,
        "admin_id":         str(ctx.author.id)
    }
    suspension_log.append(log_entry)
    save_suspension_log()

    embed = create_error_embed("VPS Suspended", f"`{container_name}` has been suspended.")
    embed.add_field(name="Owner",  value=user.mention,            inline=True)
    embed.add_field(name="VPS #",  value=str(vps_number),         inline=True)
    embed.add_field(name="Reason", value=reason,                  inline=False)
    embed.add_field(name="Admin",  value=ctx.author.mention,      inline=True)
    await ctx.send(embed=embed)

    _log = discord.Embed(
        title="⛔  VPS Suspended",
        description=(
            f"VPS **#{vps_number}** belonging to {user.mention} was **manually suspended** by an admin.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        colour=discord.Colour.from_rgb(220, 20, 60),
        timestamp=datetime.utcnow()
    )
    _log.set_author(name="VPS Suspended  •  Manual Admin Action")
    try:
        _log.set_thumbnail(url=user.display_avatar.url)
    except Exception:
        pass
    _log.add_field(name="📦 Container", value=f"`{container_name}`",             inline=True)
    _log.add_field(name="🆔 VPS ID",    value=f"`#{vps_number}`",                inline=True)
    _log.add_field(name="🏷️ Hostname",  value=f"`{vps.get('hostname','?')}`",  inline=True)
    _log.add_field(name="👤 Owner",      value=f"{user.mention}\n`{user.id}`",   inline=True)
    _log.add_field(name="🛠️ Admin", value=ctx.author.mention,              inline=True)
    _log.add_field(name="🧠 RAM",        value=f"`{vps.get('ram','?')}`",         inline=True)
    _log.add_field(name="⚡ CPU",            value=f"`{vps.get('cpu','?')} Core(s)`", inline=True)
    _log.add_field(name="🔒 Status",     value="`⛔ SUSPENDED`",             inline=True)
    _log.add_field(name="📌 Action",     value="User has been DM'd. Use `!vps-unsuspend @user <#>` to restore.", inline=True)
    _log.add_field(name="📝 Reason",     value=f"```{reason}```",                inline=False)
    _log.set_footer(text="DarkNodes VPS Logs  •  Manual Suspension")
    asyncio.create_task(send_log(_log))

    try:
        await user.send(embed=create_error_embed(
            "⚠️ VPS Suspended",
            f"Your VPS **#{vps_number}** (`{container_name}`) has been suspended by an admin.\n"
            f"**Reason:** {reason}\n\nContact an admin to appeal."
        ))
    except discord.Forbidden:
        pass


@bot.command(name='vps-unsuspend')
@is_admin()
async def vps_unsuspend(ctx, user: discord.Member, vps_number: int):
    """Unsuspend a VPS (Admin only) — !vps-unsuspend @user <vps#>"""
    user_id  = str(user.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Not Found", f"No VPS #{vps_number} for {user.mention}."))
        return

    vps            = vps_list[vps_number - 1]
    container_name = vps["container_name"]

    if vps.get("status") != "suspended":
        await ctx.send(embed=create_error_embed("Not Suspended", f"`{container_name}` is not currently suspended."))
        return

    try:
        await execute_docker(f"docker start {container_name}", timeout=30)
        await asyncio.sleep(2)
        await docker_exec(container_name, "/usr/sbin/sshd || true", timeout=10)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Start Failed", f"Could not start container: {e}"))
        return

    vps["status"] = "running"
    vps.pop("suspension_reason",  None)
    vps.pop("suspension_time",    None)
    vps.pop("suspension_evidence", None)
    save_data()

    # Clear monitoring state so the container gets a clean slate
    container_monitor_state.pop(container_name, None)

    embed = create_success_embed("VPS Unsuspended", f"`{container_name}` is now running again.")
    embed.add_field(name="Owner", value=user.mention,       inline=True)
    embed.add_field(name="VPS #", value=str(vps_number),    inline=True)
    embed.add_field(name="Admin", value=ctx.author.mention, inline=True)
    await ctx.send(embed=embed)

    _log = discord.Embed(
        title="✅  VPS Unsuspended",
        description=(
            f"VPS **#{vps_number}** belonging to {user.mention} has been **restored** and is live again.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        colour=discord.Colour.from_rgb(0, 220, 110),
        timestamp=datetime.utcnow()
    )
    _log.set_author(name="VPS Unsuspended  •  Container Restored")
    try:
        _log.set_thumbnail(url=user.display_avatar.url)
    except Exception:
        pass
    _log.add_field(name="📦 Container", value=f"`{container_name}`",             inline=True)
    _log.add_field(name="🆔 VPS ID",    value=f"`#{vps_number}`",                inline=True)
    _log.add_field(name="🏷️ Hostname",  value=f"`{vps.get('hostname','?')}`",  inline=True)
    _log.add_field(name="👤 Owner",      value=f"{user.mention}\n`{user.id}`",   inline=True)
    _log.add_field(name="🛠️ Admin", value=ctx.author.mention,              inline=True)
    _log.add_field(name="🧠 RAM",        value=f"`{vps.get('ram','?')}`",         inline=True)
    _log.add_field(name="⚡ CPU",            value=f"`{vps.get('cpu','?')} Core(s)`", inline=True)
    _log.add_field(name="🔓 Status",     value="`🟢 RUNNING`",           inline=True)
    _log.add_field(name="📌 Note",       value="Monitoring state cleared. Container has a fresh abuse-detection baseline.", inline=True)
    _log.set_footer(text="DarkNodes VPS Logs  •  Unsuspend Event")
    asyncio.create_task(send_log(_log))

    try:
        await user.send(embed=create_success_embed(
            "✅ VPS Unsuspended",
            f"Your VPS **#{vps_number}** (`{container_name}`) has been unsuspended and is running again."
        ))
    except discord.Forbidden:
        pass


@bot.command(name='vps-monitor')
@is_admin()
async def vps_monitor(ctx, container_name: str, action: str = "status"):
    """
    Control per-container monitoring (Admin only)
    !vps-monitor <container> status|pause|resume
    """
    uid, vps = find_vps_record(container_name)
    if not vps:
        await ctx.send(embed=create_error_embed("Not Found", f"No VPS record for `{container_name}`."))
        return

    state = get_container_state(container_name)

    if action.lower() == "status":
        conf   = state.get("last_confidence", 0)
        paused = state.get("monitoring_paused", False)
        cpu_samples = list(state.get("cpu_samples", []))
        avg_cpu = sum(cpu_samples) / len(cpu_samples) if cpu_samples else 0

        embed = create_info_embed(f"📊 Monitor — {container_name}", "")
        embed.add_field(name="Monitoring",        value="⏸ Paused" if paused else "▶ Active", inline=True)
        embed.add_field(name="Last Confidence",   value=f"{conf}/100",                         inline=True)
        embed.add_field(name="Avg CPU (recent)",  value=f"{avg_cpu:.1f}%",                     inline=True)
        embed.add_field(name="Active Flags",      value=", ".join(state.get("flags", set())) or "none", inline=False)

        # Last 5 scan events
        events = monitor_log.get(container_name, [])[-5:]
        if events:
            lines = []
            for ev in events:
                ts    = ev.get("timestamp", "?")[:16]
                score = ev.get("total_score", ev.get("confidence_score", 0))
                lines.append(f"`{ts}` score={score}")
            embed.add_field(name="Recent Scans", value="\n".join(lines), inline=False)

        await ctx.send(embed=embed)

    elif action.lower() == "pause":
        state["monitoring_paused"] = True
        await ctx.send(embed=create_warning_embed(
            "Monitoring Paused",
            f"Abuse monitoring for `{container_name}` is **paused**.\n"
            "Use `!vps-monitor <container> resume` to re-enable."
        ))

    elif action.lower() == "resume":
        state["monitoring_paused"] = False
        container_monitor_state.pop(container_name, None)  # fresh state
        await ctx.send(embed=create_success_embed(
            "Monitoring Resumed",
            f"Abuse monitoring for `{container_name}` is now **active** again."
        ))

    else:
        await ctx.send(embed=create_error_embed("Invalid Action", "Use: `status`, `pause`, or `resume`"))


@bot.command(name='vps-whitelist')
@is_admin()
async def vps_whitelist(ctx, target_type: str, *, value: str):
    """
    Whitelist a user/container from auto-suspension (Admin only)
    !vps-whitelist user @mention
    !vps-whitelist container <container_name>
    !vps-whitelist process <process_name>
    """
    target_type = target_type.lower()

    if target_type == "user":
        # Accept mention or raw ID
        member = None
        try:
            member = await commands.MemberConverter().convert(ctx, value)
        except Exception:
            pass
        uid = str(member.id) if member else value.strip()
        wl  = MONITOR_CONFIG.setdefault("whitelisted_users", [])
        if uid not in wl:
            wl.append(uid)
            save_mining_config()
        name = member.mention if member else uid
        await ctx.send(embed=create_success_embed("User Whitelisted", f"{name} is now exempt from auto-suspension."))

    elif target_type == "container":
        wl = MONITOR_CONFIG.setdefault("whitelisted_containers", [])
        if value not in wl:
            wl.append(value)
            save_mining_config()
        await ctx.send(embed=create_success_embed("Container Whitelisted", f"`{value}` will not be auto-suspended."))

    elif target_type == "process":
        wl = MONITOR_CONFIG.setdefault("whitelisted_processes", [])
        if value not in wl:
            wl.append(value)
            save_mining_config()
        await ctx.send(embed=create_success_embed("Process Whitelisted", f"Process `{value}` is now safe-listed."))

    else:
        await ctx.send(embed=create_error_embed(
            "Invalid Type", "Use: `user`, `container`, or `process`\n"
            "Example: `!vps-whitelist user @someone`"
        ))


@bot.command(name='vps-unwhitelist')
@is_admin()
async def vps_unwhitelist(ctx, target_type: str, *, value: str):
    """Remove a whitelist entry (Admin only)"""
    target_type = target_type.lower()
    key_map = {"user": "whitelisted_users", "container": "whitelisted_containers", "process": "whitelisted_processes"}
    if target_type not in key_map:
        await ctx.send(embed=create_error_embed("Invalid Type", "Use: `user`, `container`, or `process`"))
        return

    key = key_map[target_type]
    wl  = MONITOR_CONFIG.get(key, [])

    # For user type, try to resolve mention → ID
    uid = value.strip()
    if target_type == "user":
        try:
            member = await commands.MemberConverter().convert(ctx, value)
            uid = str(member.id)
        except Exception:
            pass

    if uid in wl:
        wl.remove(uid)
        MONITOR_CONFIG[key] = wl
        save_mining_config()
        await ctx.send(embed=create_success_embed("Whitelist Updated", f"`{uid}` removed from whitelist."))
    else:
        await ctx.send(embed=create_error_embed("Not Found", f"`{uid}` was not in the {target_type} whitelist."))


@bot.command(name='vps-mining-config')
@is_admin()
async def vps_mining_config(ctx, key: str = None, *, value: str = None):
    """
    View or update mining monitor config (Admin only)
    !vps-mining-config                   — show all settings
    !vps-mining-config cpu_threshold 90  — update a setting
    """
    int_keys   = {"cpu_threshold", "sustained_duration_minutes", "auto_suspend_threshold",
                  "monitoring_interval", "notification_channel_id"}
    bool_keys  = {"auto_suspend_enabled"}

    if key is None:
        # Show current config
        embed = create_info_embed("⚙️ Mining Monitor Config", "Current configuration values:")
        for k, v in MONITOR_CONFIG.items():
            if isinstance(v, list):
                display = f"{len(v)} entries"
            else:
                display = str(v)
            embed.add_field(name=k, value=f"`{display}`", inline=True)
        await ctx.send(embed=embed)
        return

    if key not in MONITOR_CONFIG:
        await ctx.send(embed=create_error_embed("Unknown Key", f"`{key}` is not a valid config key."))
        return

    if value is None:
        current = MONITOR_CONFIG[key]
        await ctx.send(embed=create_info_embed(f"Config: {key}", f"Current value: `{current}`"))
        return

    # Parse value
    try:
        if key in int_keys:
            parsed = int(value)
        elif key in bool_keys:
            parsed = value.lower() in ("true", "1", "yes", "on")
        else:
            parsed = value
        MONITOR_CONFIG[key] = parsed
        save_mining_config()
        # Restart monitor with new interval if interval changed
        if key == "monitoring_interval" and abuse_monitor.is_running():
            abuse_monitor.change_interval(seconds=parsed)
        await ctx.send(embed=create_success_embed(
            "Config Updated", f"`{key}` → `{parsed}`"
        ))
    except ValueError:
        await ctx.send(embed=create_error_embed("Invalid Value", f"Could not parse `{value}` for key `{key}`."))


@bot.command(name='vps-scan')
@is_admin()
async def vps_scan(ctx, container_name: str):
    """Manually trigger an abuse scan on a container (Admin only)"""
    uid, vps = find_vps_record(container_name)
    if not vps:
        await ctx.send(embed=create_error_embed("Not Found", f"No VPS record for `{container_name}`."))
        return

    msg = await ctx.send(embed=create_info_embed("Scanning", f"Running abuse scan on `{container_name}`..."))

    cpu_pct = await _get_container_cpu(container_name)
    scan    = await _scan_container_for_mining(container_name)

    # Add cpu_score for display (don't auto-suspend from manual scan)
    cpu_score    = 30 if cpu_pct > MONITOR_CONFIG.get("cpu_threshold", 95) else 0
    total_score  = cpu_score + scan["confidence_score"]

    color = 0x00ff88 if total_score < 30 else (0xffaa00 if total_score < 60 else 0xff3366)
    embed = create_embed(f"🔍 Scan Result — {container_name}", "", color)
    embed.add_field(name="⚙️ CPU %",            value=f"{cpu_pct:.1f}%",                         inline=True)
    embed.add_field(name="🔢 Confidence Score", value=f"**{total_score}/100**",                   inline=True)
    embed.add_field(name="🛡️ Suspend Threshold", value=f"{MONITOR_CONFIG.get('auto_suspend_threshold', 70)}", inline=True)
    embed.add_field(name="🦠 Suspicious Processes",
                    value=", ".join(scan["found_processes"]) or "none",   inline=False)
    embed.add_field(name="🌐 Pool Connections",
                    value=", ".join(scan["found_connections"]) or "none", inline=False)
    embed.add_field(name="💻 Mining CLI Args",
                    value=", ".join(scan["found_cli_args"]) or "none",    inline=False)
    if scan["details"]:
        embed.add_field(name="📋 Details",
                        value="\n".join(scan["details"][:10]),            inline=False)
    embed.add_field(
        name="🟡 Note",
        value="Manual scans are informational only — they do **not** auto-suspend.",
        inline=False
    )
    await msg.edit(embed=embed)


@bot.command(name='vps-status')
@is_admin()
async def vps_status_cmd(ctx):
    """Show full monitoring status overview (Admin only)"""
    embed = create_embed("🛡️ Monitoring Overview", "Live anti-abuse status for all containers", 0x1a1a1a)

    enabled  = MONITOR_CONFIG.get("auto_suspend_enabled", True)
    threshold = MONITOR_CONFIG.get("auto_suspend_threshold", 70)

    embed.add_field(
        name="Monitor Config",
        value=(
            f"**Auto-Suspend:** {'✅ Enabled' if enabled else '❌ Disabled'}\n"
            f"**CPU Threshold:** {MONITOR_CONFIG.get('cpu_threshold', 95)}%\n"
            f"**Suspend Score:** ≥{threshold}\n"
            f"**Scan Interval:** {MONITOR_CONFIG.get('monitoring_interval', 120)}s"
        ),
        inline=False
    )

    # Containers with elevated confidence scores
    flagged_lines = []
    for name, state in container_monitor_state.items():
        conf = state.get("last_confidence", 0)
        if conf > 0:
            paused = "⏸" if state.get("monitoring_paused") else ""
            flagged_lines.append(f"`{name}` — score {conf} {paused}")
    embed.add_field(
        name=f"🔶 Containers with Non-Zero Score ({len(flagged_lines)})",
        value="\n".join(flagged_lines[:15]) if flagged_lines else "None",
        inline=False
    )

    # Suspension counts
    today = datetime.utcnow().strftime("%Y-%m-%d")
    today_suspensions = sum(1 for e in suspension_log if e.get("timestamp", "").startswith(today))
    embed.add_field(
        name="📊 Suspension Stats",
        value=(
            f"**Total Suspensions:** {len(suspension_log)}\n"
            f"**Today:** {today_suspensions}\n"
            f"**Currently Suspended:** {sum(1 for vl in vps_data.values() for v in vl if v.get('status')=='suspended')}"
        ),
        inline=False
    )

    # Whitelist summary
    embed.add_field(
        name="🟩 Whitelist",
        value=(
            f"**Users:** {len(MONITOR_CONFIG.get('whitelisted_users', []))}\n"
            f"**Containers:** {len(MONITOR_CONFIG.get('whitelisted_containers', []))}\n"
            f"**Processes:** {len(MONITOR_CONFIG.get('whitelisted_processes', []))}"
        ),
        inline=False
    )
    await ctx.send(embed=embed)


# ─── Log channel helper ────────────────────────────────────────────────────────

async def send_log(embed: discord.Embed):
    """
    Post an embed to the configured log channel.
    Silently does nothing if no channel is set or the channel is unreachable.
    Call via asyncio.create_task(send_log(...)) from event handlers so the
    Discord response is never delayed waiting for the log to send.
    """
    channel_id = MONITOR_CONFIG.get("notification_channel_id", 0)
    if not channel_id:
        return
    try:
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            channel = await bot.fetch_channel(int(channel_id))
        if channel:
            await channel.send(embed=embed)
    except Exception as exc:
        logger.error(f"send_log: failed to post to channel {channel_id}: {exc}")


# ─── Slash Commands ────────────────────────────────────────────────────────────

@bot.tree.command(name="setlogschannel", description="Set the channel where all VPS event logs are posted (Admin only)")
@discord.app_commands.describe(channel="The text channel that will receive all VPS event log embeds")
async def setlogschannel(interaction: discord.Interaction, channel: discord.TextChannel):
    """Admin-only slash command: choose the log channel for all VPS events."""
    user_id = str(interaction.user.id)
    is_admin_user = (
        user_id == str(MAIN_ADMIN_ID) or
        user_id in admin_data.get("admins", [])
    )
    if not is_admin_user:
        await interaction.response.send_message(
            embed=create_error_embed("Permission Denied", "Only admins can configure the log channel."),
            ephemeral=True
        )
        return

    MONITOR_CONFIG["notification_channel_id"] = channel.id
    save_mining_config()

    embed = discord.Embed(
        title="✅  Log Channel Set",
        description=(
            f"All VPS event logs will now be posted to {channel.mention}.\n\n"
            f"**Events logged:**\n"
            f"🟢 VPS Created  •  🔴 VPS Deleted  •  ▶️ Started  •  ⏹️ Stopped\n"
            f"🔄 Reinstalled  •  ⛔ Suspended (auto & manual)  •  ✅ Unsuspended  •  🚨 Mining Detected"
        ),
        colour=discord.Colour.green(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="📋 Channel", value=f"`#{channel.name}` (ID `{channel.id}`)", inline=False)
    embed.set_footer(text="DarkNodes  •  /setlogschannel")
    await interaction.response.send_message(embed=embed)


@bot.command(name='setlogschannel')
async def setlogschannel_prefix(ctx, channel: discord.TextChannel = None):
    """Set the VPS log channel via prefix command (Admin only) — !setlogschannel #channel"""
    user_id = str(ctx.author.id)
    is_admin_user = (
        user_id == str(MAIN_ADMIN_ID) or
        user_id in admin_data.get("admins", [])
    )
    if not is_admin_user:
        await ctx.send(embed=create_error_embed("Permission Denied", "Only admins can configure the log channel."))
        return
    if channel is None:
        current_id = MONITOR_CONFIG.get("notification_channel_id", 0)
        if current_id:
            ch = bot.get_channel(int(current_id))
            ch_str = ch.mention if ch else f"`{current_id}` (not found)"
            await ctx.send(embed=create_info_embed("Current Log Channel", f"Logs are currently being sent to: {ch_str}"))
        else:
            await ctx.send(embed=create_info_embed("No Log Channel Set", "Use `!setlogschannel #channel` to set one."))
        return

    MONITOR_CONFIG["notification_channel_id"] = channel.id
    save_mining_config()

    embed = discord.Embed(
        title="✅  Log Channel Configured",
        description=(
            f"All VPS event logs will now be posted to {channel.mention}.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        colour=discord.Colour.green(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="📋 Channel", value=f"{channel.mention}\n`#{channel.name}` (ID `{channel.id}`)", inline=False)
    embed.add_field(
        name="📣 Events Logged",
        value=(
            "🟢 VPS Created  •  🔴 VPS Deleted  •  ▶️ Started  •  ⏹️ Stopped\n"
            "🔄 Reinstalled  •  ⛔ Suspended (auto & manual)  •  ✅ Unsuspended\n"
            "🚨 Mining Detected  •  All admin actions"
        ),
        inline=False
    )
    embed.set_footer(text="DarkNodes  •  !setlogschannel")
    await ctx.send(embed=embed)

    # Send a test embed to confirm the channel works
    test_embed = discord.Embed(
        title="📡  Log Channel Test",
        description=f"This channel (`#{channel.name}`) is now receiving DarkNodes VPS logs.",
        colour=discord.Colour.blurple(),
        timestamp=datetime.utcnow()
    )
    test_embed.add_field(name="Set By", value=ctx.author.mention, inline=True)
    test_embed.set_footer(text="DarkNodes VPS Logs  •  Channel Verified")
    try:
        await channel.send(embed=test_embed)
    except discord.Forbidden:
        await ctx.send(embed=create_error_embed("Permission Error", f"I cannot send messages to {channel.mention}. Please check my permissions."))


# ─── Scrollable Help System ────────────────────────────────────────────────────

def build_help_pages(is_user_admin, is_user_main_admin):
    pages = {}

    # Page 2: User Commands
    user_embed = create_embed("👤 User Commands", "VPS management commands for all users:", 0x00ff88)
    user_embed.add_field(name="🖥️ VPS Management", value=(
        "`!manage` — Manage your VPS (Start/Stop/SSH/Reinstall)\n"
        "`!manage [@user]` — Admin: manage another user's VPS\n"
        "`!share-user @user <#>` — Share VPS access\n"
        "`!share-ruser @user <#>` — Revoke shared access\n"
        "`!manage-shared @owner <#>` — Access shared VPS"
    ), inline=False)
    user_embed.add_field(name="🏷️ VPS Tools", value=(
        "`!rename-vps <#> <new_name>` — Give your VPS a nickname\n"
        "`!vps-note <#> <note>` — Add a note to your VPS\n"
        "`!ping-vps <#>` — Ping your VPS container\n"
        "`!uptime-vps <#>` — Check VPS uptime\n"
        "`!myinfo` — View your profile & VPS summary"
    ), inline=False)
    pages["user"] = user_embed

    # Page 3: Credits & Plans
    credits_embed = create_embed("💰 Credits & Plans", "Purchase plans and manage credits:", 0xffaa00)
    credits_embed.add_field(name="💳 Commands", value=(
        "`!plans` — View available VPS plans & prices\n"
        "`!buyc` — Get payment info (UPI/PayPal/Crypto)\n"
        "`!buywc <plan> <Intel/AMD>` — Buy VPS with credits\n"
        "`!credits` — Check your credit balance\n"
        "`!transfer @user <amount>` — Send credits to another user"
    ), inline=False)
    credits_embed.add_field(name="📦 Available Plans", value=(
        "🥉 **Starter** — 4GB RAM | 1 CPU | Intel: 42cr / AMD: 83cr\n"
        "🥈 **Basic** — 8GB RAM | 1 CPU | Intel: 96cr / AMD: 164cr\n"
        "🥇 **Standard** — 12GB RAM | 2 CPU | Intel: 192cr / AMD: 320cr\n"
        "💎 **Pro** — 16GB RAM | 2 CPU | Intel: 220cr / AMD: 340cr"
    ), inline=False)
    pages["credits"] = credits_embed

    # Page 4: VPS Tools
    tools_embed = create_embed("🔧 VPS Tools", "Extra tools to manage & monitor your VPS:", 0x00ccff)
    tools_embed.add_field(name="📊 Monitoring", value=(
        "`!ping-vps <#>` — Ping VPS container (check if alive)\n"
        "`!uptime-vps <#>` — Show how long VPS has been running\n"
        "`!myinfo` — Your full profile: credits, VPS list, notes"
    ), inline=False)
    tools_embed.add_field(name="🏷️ Customization", value=(
        "`!rename-vps <#> <name>` — Set a nickname for your VPS\n"
        "`!vps-note <#> <text>` — Add/update a note on your VPS\n"
        "  e.g. `!vps-note 1 My Minecraft server`"
    ), inline=False)
    tools_embed.add_field(name="💸 Economy", value=(
        "`!transfer @user <amount>` — Send credits to a friend\n"
        "`!leaderboard` — Top 10 credit holders\n"
        "`!botstatus` — Show bot stats & uptime"
    ), inline=False)
    pages["tools"] = tools_embed

    # Page 5: Extras
    extras_embed = create_embed("📢 Extras & Fun", "Announcements, leaderboard, and more:", 0xff6b9d)
    extras_embed.add_field(name="📣 Announcements", value=(
        "`!announce <message>` — Admin: broadcast to all VPS owners via DM\n"
        "`!botstatus` — Bot uptime, total VPS, active users\n"
        "`!leaderboard` — Top credit holders on the server"
    ), inline=False)
    extras_embed.add_field(name="ℹ️ Info", value=(
        "`!help` — Open this help menu\n"
        "`!plans` — VPS plan pricing\n"
        "`!myinfo` — Your personal dashboard"
    ), inline=False)
    pages["extras"] = extras_embed

    # Page 6: Admin Panel
    if is_user_admin:
        admin_embed = create_embed("🛡️ Admin Panel", "Admin-only VPS management commands:", 0xff3366)
        admin_embed.add_field(name="🖥️ VPS Control", value=(
            "`!create @user <ram_GB> <cpu_cores> <disk_GB>` — Create custom Docker VPS\n"
            "`!delete-vps @user <#> <reason>` — Delete a user's VPS\n"
            "`!restart-vps <container>` — Restart a VPS container\n"
            "`!stop-vps-all` — Emergency stop ALL VPS\n"
            "`!exec <container> <cmd>` — Run command inside VPS"
        ), inline=False)
        admin_embed.add_field(name="💾 Backup & Restore", value=(
            "`!backup-vps <container>` — Create Docker image snapshot\n"
            "`!restore-vps <container> <snapshot>` — Restore from snapshot\n"
            "`!list-snapshots <container>` — List all snapshots"
        ), inline=False)
        admin_embed.add_field(name="📊 Info & Economy", value=(
            "`!userinfo @user` — Full user info + VPS list\n"
            "`!serverstats` — Server overview stats\n"
            "`!vpsinfo [container]` — VPS details\n"
            "`!list-all` — All VPS overview\n"
            "`!adminc @user <amount>` — Add credits\n"
            "`!adminrc @user <amount/all>` — Remove credits\n"
            "`!announce <msg>` — DM all VPS owners\n"
            "`!cpu-monitor <status|enable|disable>` — Abuse monitor control\n"
            "`!maintenance <on/off>` — Toggle maintenance mode"
        ), inline=False)
        admin_embed.add_field(name="⏳ Expire Management", value=(
            "`!setexpire @user <vps#> <days>` — Set VPS expiry\n"
            "`!extendexpire @user <vps#> <days>` — Extend expiry\n"
            "`!removeexpire @user <vps#>` — Remove expiry (set to Never)\n"
            "`!checkexpire [@user]` — Check expiry status"
        ), inline=False)
        admin_embed.add_field(name="🚨 Anti-Mining & Abuse", value=(
            "`!vps-suspend @user <#> [reason]` — Manually suspend a VPS\n"
            "`!vps-unsuspend @user <#>` — Unsuspend a VPS\n"
            "`!vps-monitor <container> <status|pause|resume>` — Per-container monitor control\n"
            "`!vps-whitelist <user|container|process> <value>` — Add to whitelist\n"
            "`!vps-unwhitelist <user|container|process> <value>` — Remove from whitelist\n"
            "`!vps-mining-config [key] [value]` — View/edit monitor settings\n"
            "`!vps-scan <container>` — Manual abuse scan\n"
            "`!vps-status` — Full monitoring overview\n"
            "`!setlogschannel #channel` — Set the VPS log channel\n`/setlogschannel #channel` — Same via slash command"
        ), inline=False)
        pages["admin"] = admin_embed

    # Page 7: Main Admin
    if is_user_main_admin:
        mainadmin_embed = create_embed("👑 Main Admin", "Exclusive main admin commands:", 0xffd700)
        mainadmin_embed.add_field(name="👥 Admin Management", value=(
            "`!admin-add @user` — Promote user to admin\n"
            "`!admin-remove @user` — Remove admin role\n"
            "`!admin-list` — View all admins"
        ), inline=False)
        pages["mainadmin"] = mainadmin_embed

    return pages


@bot.command(name='help')
async def show_help(ctx):
    """Show scrollable categorized help"""
    user_id             = str(ctx.author.id)
    is_user_admin       = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
    is_user_main_admin  = user_id == str(MAIN_ADMIN_ID)

    pages = build_help_pages(is_user_admin, is_user_main_admin)

    options = [
        discord.SelectOption(label="👤 User Commands",    description="VPS manage, share, tools",       value="user",    emoji="👤"),
        discord.SelectOption(label="💰 Credits & Plans",  description="Buy VPS, check plans",            value="credits", emoji="💰"),
        discord.SelectOption(label="🔧 VPS Tools",        description="Rename, notes, ping, uptime",     value="tools",   emoji="🔧"),
        discord.SelectOption(label="📢 Extras",           description="Announcements, leaderboard",      value="extras",  emoji="📢"),
    ]
    if is_user_admin:
        options.append(discord.SelectOption(label="🛡️ Admin Panel", description="Admin VPS & abuse commands", value="admin",    emoji="🛡️"))
    if is_user_main_admin:
        options.append(discord.SelectOption(label="👑 Main Admin",  description="Admin management",           value="mainadmin", emoji="👑"))

    class HelpSelect(discord.ui.Select):
        def __init__(self):
            super().__init__(placeholder="📂 Select a category...", options=options, min_values=1, max_values=1)

        async def callback(self, interaction: discord.Interaction):
            selected = self.values[0]
            embed    = pages.get(selected, pages["user"])
            await interaction.response.edit_message(embed=embed, view=self.view)

    class HelpView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=180)
            self.add_item(HelpSelect())

    await ctx.send(embed=pages["user"], view=HelpView())


# ─── New Cool Features ──────────────────────────────────────────────────────────

BOT_START_TIME  = datetime.now()
maintenance_mode = False

@bot.command(name='rename-vps')
async def rename_vps(ctx, vps_number: int, *, new_name: str):
    """Give your VPS a custom nickname"""
    user_id  = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    if len(new_name) > 30:
        await ctx.send(embed=create_error_embed("Name Too Long", "Nickname must be 30 characters or less."))
        return
    vps_list[vps_number - 1]["nickname"] = new_name
    save_data()
    await ctx.send(embed=create_success_embed("VPS Renamed", f"VPS #{vps_number} is now called **{new_name}**!"))


@bot.command(name='vps-note')
async def vps_note(ctx, vps_number: int, *, note: str):
    """Add a note to your VPS"""
    user_id  = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vps_list[vps_number - 1]["note"] = note[:200]
    save_data()
    await ctx.send(embed=create_success_embed("Note Saved", f"Note added to VPS #{vps_number}:\n> {note[:200]}"))


@bot.command(name='ping-vps')
async def ping_vps(ctx, vps_number: int):
    """Ping your VPS container to check if it's alive"""
    user_id  = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vps       = vps_list[vps_number - 1]
    container = vps["container_name"]
    nickname  = vps.get("nickname", f"VPS #{vps_number}")
    msg       = await ctx.send(embed=create_info_embed("Pinging...", f"Checking `{container}`..."))
    start     = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "--format={{.State.Running}}", container,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        elapsed    = int((time.time() - start) * 1000)
        is_running = stdout.decode().strip() == "true"
        if is_running:
            embed = create_success_embed("🏓 Pong!", f"**{nickname}** is alive!\n⚡ Response: `{elapsed}ms`")
        else:
            embed = create_error_embed("💀 No Response", f"**{nickname}** container is not running.")
        await msg.edit(embed=embed)
    except Exception as e:
        await msg.edit(embed=create_error_embed("Ping Failed", str(e)))


@bot.command(name='uptime-vps')
async def uptime_vps(ctx, vps_number: int):
    """Check VPS uptime"""
    user_id  = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", "VPS not found."))
        return
    vps       = vps_list[vps_number - 1]
    container = vps["container_name"]
    nickname  = vps.get("nickname", f"VPS #{vps_number}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "--format={{.State.StartedAt}}", container,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _       = await asyncio.wait_for(proc.communicate(), timeout=10)
        started_at_str  = stdout.decode().strip()
        started_at      = datetime.fromisoformat(started_at_str[:19])
        uptime_delta    = datetime.utcnow() - started_at
        days            = uptime_delta.days
        hours, rem      = divmod(uptime_delta.seconds, 3600)
        minutes, seconds = divmod(rem, 60)
        uptime_str      = f"{days}d {hours}h {minutes}m {seconds}s"
        embed = create_success_embed(f"⏱️ Uptime — {nickname}", f"Container has been running for:\n```{uptime_str}```")
        embed.add_field(name="Started At", value=f"`{started_at_str[:19]} UTC`", inline=False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Uptime Error", str(e)))


@bot.command(name='myinfo')
async def my_info(ctx):
    """Show your personal dashboard"""
    user_id  = str(ctx.author.id)
    credits  = user_data.get(user_id, {}).get("credits", 0)
    vps_list = vps_data.get(user_id, [])
    embed    = create_embed(f"👤 {ctx.author.name}'s Dashboard", "", 0x5865F2)
    embed.set_thumbnail(url=ctx.author.display_avatar.url)
    embed.add_field(name="💰 Credits",    value=f"**{credits}**",          inline=True)
    embed.add_field(name="🖥️ VPS Count",  value=f"**{len(vps_list)}**",    inline=True)
    embed.add_field(name="📅 Account",    value=f"Joined: {ctx.author.created_at.strftime('%Y-%m-%d')}", inline=True)
    if vps_list:
        vps_text = ""
        for i, v in enumerate(vps_list):
            nickname    = v.get("nickname", f"VPS {i+1}")
            status      = v.get("status", "unknown")
            status_icon = "🟢" if status == "running" else ("🟠" if status == "suspended" else "🔴")
            note        = f" — _{v['note']}_" if v.get("note") else ""
            suspension  = f" ⚠️ *Suspended*" if status == "suspended" else ""
            vps_text   += f"{status_icon} **{nickname}** (`{v['container_name']}`){note}{suspension}\n"
        embed.add_field(name="🖥️ Your VPS", value=vps_text, inline=False)
    else:
        embed.add_field(name="🖥️ Your VPS", value="No VPS yet. Use `!buywc` to get one!", inline=False)
    await ctx.send(embed=embed)


@bot.command(name='transfer')
async def transfer_credits(ctx, target: discord.Member, amount: int):
    """Transfer credits to another user"""
    if amount <= 0:
        await ctx.send(embed=create_error_embed("Invalid Amount", "Amount must be positive."))
        return
    if target.id == ctx.author.id:
        await ctx.send(embed=create_error_embed("Invalid Target", "You can't transfer to yourself!"))
        return
    sender_id = str(ctx.author.id)
    target_id = str(target.id)
    if sender_id not in user_data:
        user_data[sender_id] = {"credits": 0}
    if user_data[sender_id]["credits"] < amount:
        await ctx.send(embed=create_error_embed(
            "Insufficient Credits",
            f"You only have **{user_data[sender_id]['credits']}** credits."
        ))
        return
    if target_id not in user_data:
        user_data[target_id] = {"credits": 0}
    user_data[sender_id]["credits"] -= amount
    user_data[target_id]["credits"] += amount
    save_data()
    embed = create_success_embed("💸 Transfer Complete",
                                 f"{ctx.author.mention} sent **{amount}** credits to {target.mention}!")
    embed.add_field(name="Your Balance", value=f"**{user_data[sender_id]['credits']}** credits", inline=True)
    await ctx.send(embed=embed)
    try:
        await target.send(embed=create_info_embed(
            "💰 Credits Received",
            f"You received **{amount}** credits from {ctx.author.mention}!\n"
            f"New balance: **{user_data[target_id]['credits']}**"
        ))
    except discord.Forbidden:
        pass


@bot.command(name='leaderboard')
async def leaderboard(ctx):
    """Show top 10 credit holders"""
    sorted_users = sorted(user_data.items(), key=lambda x: x[1].get("credits", 0), reverse=True)[:10]
    embed   = create_embed("🏆 Credit Leaderboard", "Top 10 credit holders:", 0xffd700)
    medals  = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines   = []
    for i, (uid, data) in enumerate(sorted_users):
        try:
            u    = await bot.fetch_user(int(uid))
            name = u.name
        except Exception:
            name = f"User#{uid[:4]}"
        lines.append(f"{medals[i]} **{name}** — {data.get('credits', 0)} credits")
    embed.add_field(name="Rankings", value="\n".join(lines) if lines else "No data yet.", inline=False)
    await ctx.send(embed=embed)


@bot.command(name='botstatus')
async def bot_status(ctx):
    """Show bot status and stats"""
    uptime_delta = datetime.now() - BOT_START_TIME
    days         = uptime_delta.days
    hours, rem   = divmod(uptime_delta.seconds, 3600)
    minutes, _   = divmod(rem, 60)
    total_vps    = sum(len(v) for v in vps_data.values())
    running_vps  = sum(1 for vl in vps_data.values() for v in vl if v.get("status") == "running")
    embed = create_embed("🤖 Bot Status", "DarkNodes VPS Manager", 0x00ff88)
    embed.add_field(name="⏱️ Uptime",       value=f"`{days}d {hours}h {minutes}m`",            inline=True)
    embed.add_field(name="🖥️ Total VPS",    value=f"**{total_vps}** ({running_vps} running)",  inline=True)
    embed.add_field(name="👥 Users",         value=f"**{len(user_data)}**",                     inline=True)
    embed.add_field(name="🔧 Maintenance",  value="🔴 ON" if maintenance_mode else "🟢 OFF",   inline=True)
    embed.add_field(name="📡 Latency",       value=f"`{round(bot.latency * 1000)}ms`",          inline=True)
    embed.add_field(name="🛡️ Abuse Monitor", value="✅ Active" if MONITOR_CONFIG.get("auto_suspend_enabled", True) else "❌ Disabled", inline=True)
    await ctx.send(embed=embed)


@bot.command(name='announce')
@is_admin()
async def announce(ctx, *, message: str):
    """Send an announcement DM to all VPS owners (Admin only)"""
    sent   = 0
    failed = 0
    announce_embed = create_embed("📢 Announcement", message, 0xffaa00)
    announce_embed.add_field(name="From", value=f"**DarkNodes Team** ({ctx.author.mention})", inline=False)
    status_msg = await ctx.send(embed=create_info_embed("Sending Announcement", "Broadcasting to all VPS owners..."))
    for uid in vps_data.keys():
        try:
            user = await bot.fetch_user(int(uid))
            await user.send(embed=announce_embed)
            sent += 1
            await asyncio.sleep(0.5)
        except Exception:
            failed += 1
    await status_msg.edit(embed=create_success_embed(
        "Announcement Sent",
        f"✅ Delivered to **{sent}** users\n❌ Failed: **{failed}** (DMs closed)"
    ))


@bot.command(name='maintenance')
@is_admin()
async def maintenance_toggle(ctx, mode: str):
    """Toggle maintenance mode (Admin only)"""
    global maintenance_mode
    if mode.lower() == "on":
        maintenance_mode = True
        await bot.change_presence(
            status=discord.Status.idle,
            activity=discord.Activity(type=discord.ActivityType.watching, name="🔴 Under Maintenance")
        )
        await ctx.send(embed=create_warning_embed(
            "🔴 Maintenance Mode ON",
            "Bot is now in maintenance mode.\n"
            "• ALL commands blocked for non-admins\n"
            "• DM commands also blocked\n"
            "• Bot status set to Idle"
        ))
    elif mode.lower() == "off":
        maintenance_mode = False
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(type=discord.ActivityType.watching, name="DarkNodes | VPS Manager")
        )
        await ctx.send(embed=create_success_embed("🟢 Maintenance Mode OFF", "Bot is back to normal operation."))
    else:
        await ctx.send(embed=create_error_embed("Invalid", "Use: `!maintenance on` or `!maintenance off`"))


# ─── Maintenance mode global check ────────────────────────────────────────────

@bot.check
async def maintenance_check(ctx):
    global maintenance_mode
    if not maintenance_mode:
        return True

    user_id       = str(ctx.author.id)
    is_user_admin = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])

    if is_user_admin and ctx.command and ctx.command.name == 'maintenance':
        return True

    if not is_user_admin:
        await ctx.send(embed=create_warning_embed(
            "🔴 Under Maintenance",
            "The bot is currently under maintenance.\n"
            "All commands are disabled until maintenance is complete."
        ))
        return False

    return True


# ─── Block ALL bot commands in DMs ────────────────────────────────────────────

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if isinstance(message.channel, discord.DMChannel):
        if message.content.startswith(bot.command_prefix):
            block_embed = create_warning_embed(
                "❌ DM Commands Disabled",
                "Bot commands are **not allowed in DMs**.\n\n"
                "Please use bot commands in the **server channel** only.\n"
                "👉 Go to the server and use commands there!"
            )
            block_embed.set_footer(text="DarkNodes | Server-only bot")
            await message.channel.send(embed=block_embed)
            return

    if maintenance_mode and isinstance(message.channel, discord.TextChannel):
        user_id       = str(message.author.id)
        is_user_admin = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
        if not is_user_admin and message.content.startswith(bot.command_prefix):
            await message.channel.send(embed=create_warning_embed(
                "🔴 Under Maintenance",
                "The bot is currently under maintenance.\nAll commands are disabled for users."
            ))
            return

    await bot.process_commands(message)


# ─── Typo aliases ─────────────────────────────────────────────────────────────

@bot.command(name='mangage')
async def manage_typo(ctx):
    await ctx.send(embed=create_info_embed("Command Correction", "Did you mean `!manage`?"))

@bot.command(name='stats')
async def stats_alias(ctx):
    if str(ctx.author.id) == str(MAIN_ADMIN_ID) or str(ctx.author.id) in admin_data.get("admins", []):
        await server_stats(ctx)
    else:
        await ctx.send(embed=create_error_embed("Access Denied", "Admin only."))


# ─── Expire System ─────────────────────────────────────────────────────────────

@bot.command(name='setexpire')
@is_admin()
async def set_expire(ctx, user: discord.Member, vps_number: int, days: int):
    """Set VPS expiry — !setexpire @user <vps#> <days>"""
    import datetime as dt
    user_id  = str(user.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Not Found", f"{user.mention} has no VPS #{vps_number}."))
        return
    exp_date = (datetime.utcnow() + dt.timedelta(days=days)).isoformat()
    vps_list[vps_number - 1]['expires'] = exp_date
    save_data()
    vps_name = vps_list[vps_number - 1]['container_name']
    await ctx.send(embed=create_success_embed(
        "Expiry Set",
        f"✅ {user.mention}'s **VPS #{vps_number}** (`{vps_name}`) expires on `{exp_date[:10]}` ({days}d from now)."
    ))
    try:
        await user.send(embed=create_warning_embed(
            "⏳ VPS Expiry Set",
            f"Your **VPS #{vps_number}** (`{vps_name}`) has been set to expire on **{exp_date[:10]}** ({days} days).\n"
            f"Contact an admin to extend it before it expires!"
        ))
    except discord.Forbidden:
        pass


@bot.command(name='extendexpire')
@is_admin()
async def extend_expire(ctx, user: discord.Member, vps_number: int, days: int):
    """Extend VPS expiry — !extendexpire @user <vps#> <days>"""
    import datetime as dt
    user_id  = str(user.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Not Found", f"{user.mention} has no VPS #{vps_number}."))
        return
    vps     = vps_list[vps_number - 1]
    current = vps.get('expires', 'Never')
    if current == 'Never' or not current:
        base = datetime.utcnow()
    else:
        try:
            base = datetime.fromisoformat(current)
            if base < datetime.utcnow():
                base = datetime.utcnow()
        except Exception:
            base = datetime.utcnow()
    new_exp  = (base + dt.timedelta(days=days)).isoformat()
    vps['expires'] = new_exp
    save_data()
    vps_name = vps['container_name']
    await ctx.send(embed=create_success_embed(
        "Expiry Extended",
        f"✅ {user.mention}'s **VPS #{vps_number}** (`{vps_name}`) extended by **{days} days**.\nNew expiry: `{new_exp[:10]}`"
    ))
    try:
        await user.send(embed=create_success_embed(
            "✅ VPS Extended",
            f"Your **VPS #{vps_number}** (`{vps_name}`) has been extended by **{days} days**!\nNew expiry: **{new_exp[:10]}**"
        ))
    except discord.Forbidden:
        pass


@bot.command(name='checkexpire')
async def check_expire(ctx, user: discord.Member = None):
    """Check VPS expiry — users check own, admins can check others"""
    is_user_admin = str(ctx.author.id) == str(MAIN_ADMIN_ID) or str(ctx.author.id) in admin_data.get("admins", [])
    target        = user if (user and is_user_admin) else ctx.author
    user_id       = str(target.id)
    if user_id not in vps_data or not vps_data[user_id]:
        await ctx.send(embed=create_error_embed("Not Found", f"{target.mention} has no VPS."))
        return
    embed = create_info_embed(f"⏳ VPS Expiry — {target.display_name}", "")
    for i, vps in enumerate(vps_data[user_id]):
        expires = vps.get('expires', 'Never')
        if expires and expires != 'Never':
            try:
                exp_dt    = datetime.fromisoformat(expires)
                days_left = (exp_dt - datetime.utcnow()).days
                if days_left < 0:
                    status = f"❌ **EXPIRED** {abs(days_left)}d ago"
                elif days_left <= 3:
                    status = f"⚠️ Expires in **{days_left}d** — {expires[:10]}"
                else:
                    status = f"✅ Expires on `{expires[:10]}` ({days_left}d left)"
            except Exception:
                status = expires
        else:
            status = "♾️ Never (No expiry set)"
        embed.add_field(
            name=f"VPS #{i+1} — `{vps['container_name']}`",
            value=status, inline=False
        )
    await ctx.send(embed=embed)


@bot.command(name='removeexpire')
@is_admin()
async def remove_expire(ctx, user: discord.Member, vps_number: int):
    """Remove expiry from a specific VPS — !removeexpire @user <vps#>"""
    user_id  = str(user.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Not Found", f"{user.mention} has no VPS #{vps_number}."))
        return
    vps_list[vps_number - 1]['expires'] = 'Never'
    save_data()
    vps_name = vps_list[vps_number - 1]['container_name']
    await ctx.send(embed=create_success_embed(
        "Expiry Removed",
        f"✅ {user.mention}'s **VPS #{vps_number}** (`{vps_name}`) expiry set to **Never**."
    ))


# ─── Auto expire checker — runs every hour ─────────────────────────────────────

@tasks.loop(hours=1)
async def auto_expire_check():
    now = datetime.utcnow()
    for user_id, vps_list in list(vps_data.items()):
        for vps in vps_list:
            expires = vps.get('expires', 'Never')
            if not expires or expires == 'Never':
                continue
            try:
                exp_dt    = datetime.fromisoformat(expires)
                days_left = (exp_dt - now).days

                if days_left == 3:
                    try:
                        u = await bot.fetch_user(int(user_id))
                        await u.send(embed=create_warning_embed(
                            "⚠️ VPS Expiring Soon",
                            f"Your VPS `{vps['container_name']}` expires in **3 days** on `{expires[:10]}`!\n"
                            "Contact an admin to extend it."
                        ))
                    except Exception:
                        pass

                elif days_left == 1:
                    try:
                        u = await bot.fetch_user(int(user_id))
                        await u.send(embed=create_error_embed(
                            "🚨 VPS Expiring Tomorrow!",
                            f"Your VPS `{vps['container_name']}` expires **tomorrow** (`{expires[:10]}`)!\n"
                            "Contact an admin IMMEDIATELY to avoid losing access."
                        ))
                    except Exception:
                        pass

                elif days_left < 0:
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "docker", "stop", vps['container_name'],
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                        )
                        await proc.communicate()
                        vps['status'] = 'stopped'
                    except Exception:
                        pass
                    try:
                        u = await bot.fetch_user(int(user_id))
                        await u.send(embed=create_error_embed(
                            "❌ VPS Expired",
                            f"Your VPS `{vps['container_name']}` has expired and been **stopped**.\n"
                            "Contact an admin to renew it."
                        ))
                    except Exception:
                        pass
            except Exception:
                continue
    save_data()


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    token = ""
    bot.run(token)
