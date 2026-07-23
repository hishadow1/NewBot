#!/usr/bin/env python3
"""
DarkNodes Node Agent  —  WebSocket edition
──────────────────────────────────────────
Connects this machine to your DarkNodes bot via a persistent, encrypted
WebSocket tunnel.  The node only needs outbound internet access — no public IP,
no open ports, no port forwarding, no SSH access from the bot required.

Works behind NAT, CGNAT, VPN, cloud private networks, and any firewall that
allows outbound HTTPS/WebSocket connections.

────────────────────────────────────────────────────────────────────────────────
QUICK START
────────────────────────────────────────────────────────────────────────────────

1. Install the one dependency (if not already present):
       pip install websockets

2. First-time registration (get --bot-url and --token from /node add in Discord):
       python3 node_agent.py --bot-url ws://your-bot-server:7700 --token <TOKEN>

3. Every run after that (credentials are saved to node_agent.json):
       python3 node_agent.py --bot-url ws://your-bot-server:7700

   Or with a custom config file:
       python3 node_agent.py --config /etc/darknodes/agent.json

────────────────────────────────────────────────────────────────────────────────
OPTIONAL FLAGS
────────────────────────────────────────────────────────────────────────────────
  --bot-url    wss://host:port   WebSocket URL of the DarkNodes bot server
  --token      TOKEN             One-time registration token from /node add
  --name       NAME              Custom display name (default: machine hostname)
  --interval   SECONDS           Heartbeat interval (default: 15)
  --config     PATH              Path to credentials file (default: node_agent.json)

────────────────────────────────────────────────────────────────────────────────
RUN AS A SYSTEM SERVICE  (optional, for auto-start on boot)
────────────────────────────────────────────────────────────────────────────────

Create /etc/systemd/system/darknodes-agent.service:

    [Unit]
    Description=DarkNodes Node Agent
    After=network-online.target
    Wants=network-online.target

    [Service]
    Type=simple
    ExecStart=/usr/bin/python3 /opt/darknodes/node_agent.py \\
                  --bot-url ws://your-bot-server:7700
    WorkingDirectory=/opt/darknodes
    Restart=always
    RestartSec=10

    [Install]
    WantedBy=multi-user.target

Then:  systemctl enable --now darknodes-agent
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

# ── Dependency check ──────────────────────────────────────────────────────────
try:
    import websockets                                    # noqa: F401
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    print(
        "\n[ERROR] The 'websockets' package is required but not installed.\n"
        "        Run:  pip install websockets\n",
        file=sys.stderr,
    )
    sys.exit(1)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger("darknodes.agent")

DEFAULT_CONFIG   = "node_agent.json"
DEFAULT_INTERVAL = 15   # seconds between heartbeats
RECONNECT_MIN    = 1    # initial reconnect delay (seconds)
RECONNECT_MAX    = 60   # maximum reconnect delay (seconds)
COMMAND_TIMEOUT  = 120  # default shell command timeout


# ══════════════════════════════════════════════════════════════════════════════
# System stats (pure stdlib — no extra deps)
# ══════════════════════════════════════════════════════════════════════════════

def _get_public_ip() -> str:
    """Best-effort public IP detection."""
    for svc in [
        "https://api.ipify.org",
        "https://icanhazip.com",
        "https://ifconfig.me/ip",
    ]:
        try:
            req = urllib.request.Request(svc, headers={"User-Agent": "DarkNodes-Agent/2.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.read().decode().strip()
        except Exception:
            continue
    return ""


def _cpu_percent() -> float:
    """Approximate CPU usage % via /proc/stat (two 300ms samples)."""
    def _read():
        try:
            with open("/proc/stat") as fh:
                line = fh.readline()
            fields = list(map(int, line.split()[1:]))
            return sum(fields), fields[3]
        except Exception:
            return 0, 0

    t1, i1 = _read()
    time.sleep(0.3)
    t2, i2 = _read()
    dt = t2 - t1 or 1
    return round((1 - (i2 - i1) / dt) * 100, 1)


def _ram_mb() -> tuple[int, int]:
    """Return (used_mb, total_mb) from /proc/meminfo."""
    try:
        info: dict = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, val = line.partition(":")
                info[key.strip()] = int(val.split()[0])
        total  = info.get("MemTotal",     0) // 1024
        avail  = info.get("MemAvailable", info.get("MemFree", 0)) // 1024
        return total - avail, total
    except Exception:
        return 0, 0


def _running_vps_count() -> int:
    """Count Docker containers carrying the darknodes.vps label."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "label=darknodes.vps=true", "-q"],
            capture_output=True, text=True, timeout=10,
        )
        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        return len(lines)
    except Exception:
        return 0


async def _collect_stats() -> dict:
    """Gather system stats in a thread so we don't block the event loop."""
    loop = asyncio.get_event_loop()
    cpu       = await loop.run_in_executor(None, _cpu_percent)
    used, tot = await loop.run_in_executor(None, _ram_mb)
    vps       = await loop.run_in_executor(None, _running_vps_count)
    return {
        "cpu":          cpu,
        "ram_used_mb":  used,
        "ram_total_mb": tot,
        "running_vps":  vps,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Shell command execution
# ══════════════════════════════════════════════════════════════════════════════

async def _run_command(command: str, timeout: int = COMMAND_TIMEOUT) -> tuple[bool, str, str]:
    """
    Run a shell command asynchronously.
    Returns (success, stdout, stderr).
    """
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return False, "", f"Command timed out after {timeout}s"

        success = proc.returncode == 0
        return success, stdout.decode(errors="replace").strip(), stderr.decode(errors="replace").strip()

    except Exception as exc:
        return False, "", str(exc)


# ══════════════════════════════════════════════════════════════════════════════
# Config persistence
# ══════════════════════════════════════════════════════════════════════════════

def _load_config(path: str) -> dict:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_config(path: str, data: dict) -> None:
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
    logger.info(f"Credentials saved to {path}")


# ══════════════════════════════════════════════════════════════════════════════
# WebSocket message helpers
# ══════════════════════════════════════════════════════════════════════════════

async def _send(ws, payload: dict) -> None:
    await ws.send(json.dumps(payload))


async def _recv(ws, timeout: float = 30.0) -> dict:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


# ══════════════════════════════════════════════════════════════════════════════
# Agent session  (one WebSocket connection lifetime)
# ══════════════════════════════════════════════════════════════════════════════

class _AgentSession:
    """Manages a single WebSocket connection to the bot."""

    def __init__(
        self,
        ws,
        node_id:  str,
        secret:   str,
        interval: int,
        config_path: str,
    ):
        self._ws          = ws
        self._node_id     = node_id
        self._secret      = secret
        self._interval    = interval
        self._config_path = config_path
        self._running     = True

    async def run(self) -> None:
        """Run heartbeat + message receive loops concurrently."""
        try:
            await asyncio.gather(
                self._heartbeat_loop(),
                self._receive_loop(),
            )
        except Exception:
            pass
        finally:
            self._running = False

    async def _heartbeat_loop(self) -> None:
        """Send a heartbeat every --interval seconds."""
        while self._running:
            try:
                stats = await _collect_stats()
                await _send(self._ws, {
                    "type":         "heartbeat",
                    "cpu":          stats["cpu"],
                    "ram_used_mb":  stats["ram_used_mb"],
                    "ram_total_mb": stats["ram_total_mb"],
                    "running_vps":  stats["running_vps"],
                })
                logger.debug(
                    f"Heartbeat sent — CPU={stats['cpu']}%  "
                    f"RAM={stats['ram_used_mb']}/{stats['ram_total_mb']}MB  "
                    f"VPS={stats['running_vps']}"
                )
            except Exception as exc:
                logger.warning(f"Heartbeat failed: {exc}")
                break
            await asyncio.sleep(self._interval)

    async def _receive_loop(self) -> None:
        """Process incoming messages from the bot."""
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(f"Received malformed message: {raw[:200]}")
                continue

            mtype = msg.get("type", "")

            if mtype == "ok":
                pass  # heartbeat ack

            elif mtype == "command":
                # Execute the command in the background so we don't block
                asyncio.create_task(self._handle_command(msg))

            elif mtype == "ping":
                await _send(self._ws, {"type": "pong"})

            elif mtype == "error":
                logger.error(f"Server error: {msg.get('message', '?')}")

            else:
                logger.debug(f"Unknown message type: {mtype}")

    async def _handle_command(self, msg: dict) -> None:
        """Execute a command and send the result back."""
        cmd_id  = msg.get("cmd_id", "unknown")
        command = msg.get("command", "")
        timeout = int(msg.get("timeout", COMMAND_TIMEOUT))

        logger.info(f"Executing [{cmd_id}]: {command[:120]}")
        success, stdout, stderr = await _run_command(command, timeout=timeout)

        output = stdout if success else (stderr or stdout)
        logger.info(
            f"Result [{cmd_id}]: {'✅ ok' if success else '❌ failed'}  "
            f"({len(output)} bytes)"
        )

        try:
            await _send(self._ws, {
                "type":    "result",
                "cmd_id":  cmd_id,
                "success": success,
                "output":  output,
                "error":   stderr if not success else "",
            })
        except Exception as exc:
            logger.warning(f"Failed to send result for [{cmd_id}]: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# Registration
# ══════════════════════════════════════════════════════════════════════════════

async def _register(ws, token: str, name: str) -> tuple[str, str]:
    """
    Send a register message and return (node_id, secret).
    Raises RuntimeError on failure.
    """
    hostname  = name or socket.gethostname()
    public_ip = await asyncio.get_event_loop().run_in_executor(None, _get_public_ip)

    logger.info(f"Registering as '{hostname}' (public IP: {public_ip or 'unknown'}) …")
    await _send(ws, {
        "type":      "register",
        "token":     token,
        "hostname":  hostname,
        "public_ip": public_ip,
    })

    resp = await _recv(ws, timeout=30)
    if resp.get("type") == "error":
        raise RuntimeError(f"Registration rejected: {resp.get('message', 'unknown error')}")
    if resp.get("type") != "registered":
        raise RuntimeError(f"Unexpected response: {resp}")

    node_id = resp["node_id"]
    secret  = resp["secret"]
    logger.info(f"✅  Registered — node_id={node_id}")
    return node_id, secret


async def _auth(ws, node_id: str, secret: str) -> None:
    """
    Authenticate an existing node after reconnect.
    Raises RuntimeError on failure.
    """
    await _send(ws, {
        "type":    "auth",
        "node_id": node_id,
        "secret":  secret,
    })
    resp = await _recv(ws, timeout=30)
    if resp.get("type") == "error":
        raise RuntimeError(f"Auth rejected: {resp.get('message', 'unknown error')}")
    if resp.get("type") != "auth_ok":
        raise RuntimeError(f"Unexpected auth response: {resp}")
    logger.info(f"✅  Authenticated as node {node_id}")


# ══════════════════════════════════════════════════════════════════════════════
# Main connection loop  (reconnects automatically)
# ══════════════════════════════════════════════════════════════════════════════

async def run_agent(
    bot_url:     str,
    node_id:     str,
    secret:      str,
    token:       str,
    name:        str,
    interval:    int,
    config_path: str,
) -> None:
    """
    Connect to the bot and maintain the connection indefinitely.
    Reconnects with exponential back-off on any failure.
    """
    backoff = RECONNECT_MIN

    while True:
        logger.info(f"Connecting to {bot_url} …")
        try:
            async with ws_connect(
                bot_url,
                open_timeout=20,
                ping_interval=30,
                ping_timeout=20,
                close_timeout=5,
            ) as ws:
                backoff = RECONNECT_MIN  # reset on successful connect

                if node_id:
                    # Reconnecting — authenticate with saved credentials
                    await _auth(ws, node_id, secret)
                else:
                    # First run — register and save credentials
                    if not token:
                        raise RuntimeError(
                            "--token is required for first registration.\n"
                            "Get one from your Discord server with /node add"
                        )
                    node_id, secret = await _register(ws, token, name)
                    _save_config(config_path, {
                        "bot_url": bot_url,
                        "node_id": node_id,
                        "secret":  secret,
                        "name":    name or socket.gethostname(),
                    })

                logger.info(
                    f"🟢  Connected  —  node_id={node_id}  "
                    f"heartbeat={interval}s  bot={bot_url}"
                )
                session = _AgentSession(ws, node_id, secret, interval, config_path)
                await session.run()
                logger.info("Connection closed by server.")

        except (OSError, ConnectionRefusedError, TimeoutError) as exc:
            logger.warning(f"Cannot reach bot ({exc}) — retrying in {backoff}s …")
        except RuntimeError as exc:
            logger.error(str(exc))
            # If registration/auth failed due to bad credentials, wait longer
            backoff = min(backoff * 2, RECONNECT_MAX)
        except Exception as exc:
            logger.warning(f"Connection error ({type(exc).__name__}: {exc}) — retrying in {backoff}s …")

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, RECONNECT_MAX)


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="DarkNodes Node Agent — connect this machine to your DarkNodes bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--bot-url",
        help="WebSocket URL of the bot, e.g.  ws://1.2.3.4:7700  or  wss://mybot.example.com",
    )
    parser.add_argument(
        "--token", default="",
        help="One-time registration token from /node add (required on first run)",
    )
    parser.add_argument(
        "--name", default="",
        help="Custom display name for this node (default: machine hostname)",
    )
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL,
        help=f"Heartbeat interval in seconds (default: {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG,
        help=f"Path to credentials file (default: {DEFAULT_CONFIG})",
    )
    args = parser.parse_args()

    # ── Load or create credentials ────────────────────────────────────────────
    cfg = _load_config(args.config)

    bot_url = args.bot_url or cfg.get("bot_url", "")
    if not bot_url:
        parser.error(
            "--bot-url is required.\n"
            "Example: python3 node_agent.py --bot-url ws://<BOT_IP>:7700 --token <TOKEN>"
        )

    node_id = cfg.get("node_id", "")
    secret  = cfg.get("secret",  "")
    name    = args.name or cfg.get("name", "")
    token   = args.token or cfg.get("token", "")

    if node_id:
        logger.info(f"Resuming as node {node_id} (loaded from {args.config})")
    elif not token:
        parser.error(
            "--token is required for first registration.\n"
            "Get one from your Discord server with /node add"
        )

    logger.info(
        f"DarkNodes Node Agent starting\n"
        f"  Bot URL : {bot_url}\n"
        f"  Node    : {node_id or '(registering…)'}\n"
        f"  Config  : {args.config}\n"
        f"  Interval: {args.interval}s"
    )

    try:
        asyncio.run(run_agent(
            bot_url=bot_url,
            node_id=node_id,
            secret=secret,
            token=token,
            name=name,
            interval=args.interval,
            config_path=args.config,
        ))
    except KeyboardInterrupt:
        logger.info("Agent stopped by user.")


if __name__ == "__main__":
    main()
