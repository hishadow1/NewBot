"""
DarkNodes Node System  —  WebSocket-relay edition
──────────────────────────────────────────────────
All communication between the bot and remote agents flows through a persistent
WebSocket connection initiated OUTBOUND by the node agent.  The node does NOT
need a public IP, open ports, or port forwarding of any kind.

Architecture
────────────
• Local Node  — auto-created on first boot; uses the bot machine's own Docker.
• Remote Node — any machine running node_agent.py.

Registration flow
-----------------
1. Admin runs  /node add  in Discord (ephemeral reply shows a one-line command).
2. Admin copies the command to the remote machine and runs it:
       python3 node_agent.py --bot-url ws://<BOT_IP>:7700 --token <REG_TOKEN>
3. Agent opens a WebSocket to the bot and sends:
       {"type": "register", "token": "<REG_TOKEN>", "hostname": "...", "public_ip": "..."}
4. Bot validates the token, creates the node record, and replies:
       {"type": "registered", "node_id": "...", "secret": "..."}
5. Agent saves credentials to node_agent.json and the connection stays open.

Ongoing messages (all over the persistent WebSocket)
-----------------------------------------------------
Agent → Bot:
    {"type": "heartbeat",  "cpu": 45.2, "ram_used_mb": 1024, "ram_total_mb": 4096, "running_vps": 2}
    {"type": "result",     "cmd_id": "...", "success": true, "output": "..."}
    {"type": "log",        "level": "info", "message": "..."}

Bot → Agent:
    {"type": "ok"}                                   — heartbeat ack
    {"type": "command",  "cmd_id": "...", "command": "..."}   — run this shell command
    {"type": "ping"}                                 — keepalive

Reconnection
------------
If the connection drops the agent automatically reconnects with exponential
backoff (1 s → 60 s max).  Pending commands are flushed on reconnect.

Integration with bot.py
------------------------
Call order (same pattern as template_system):

    node_system.init(docker_exec, run_docker_command,
                     get_logo_url, get_brand_name,
                     MAIN_ADMIN_ID, admin_data)
    node_system.register_commands(bot)
    # inside on_ready:
    asyncio.create_task(node_system.startup())
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional

import discord
from discord import app_commands

try:
    from websockets.asyncio.server import serve as ws_serve, ServerConnection
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False
    ServerConnection = Any  # type: ignore

# ── Logger ────────────────────────────────────────────────────────────────────
logger = logging.getLogger("vps_bot.nodes")

# ── Constants ─────────────────────────────────────────────────────────────────
_BASE             = os.path.dirname(os.path.abspath(__file__))
NODES_FILE        = os.path.join(_BASE, "nodes.json")
TOKENS_FILE       = os.path.join(_BASE, "node_tokens.json")
TOKEN_EXPIRY_MIN  = 60                  # minutes a registration token is valid
OFFLINE_SECS      = 90                  # no heartbeat → node is offline
SELECT_TIMEOUT_S  = 90                  # seconds user has to pick a node
LOCAL_NODE_ID     = "local"
WS_HOST           = "0.0.0.0"
WS_PORT           = int(os.environ.get("NODE_WS_PORT", "7700"))

# ── Injected by init() ────────────────────────────────────────────────────────
_docker_exec:    Optional[Callable] = None
_run_docker:     Optional[Callable] = None
_get_logo:       Optional[Callable] = None
_get_brand:      Optional[Callable] = None
_get_server_ip:  Optional[Callable] = None   # returns the bot server's public IP
_main_admin_id:  str                = ""
_admin_data:     Optional[dict]     = None
_bot:            Optional[Any]      = None

# ── In-memory state ───────────────────────────────────────────────────────────
nodes:            Dict[str, Dict]        = {}   # {node_id: node_record}
_tokens:          Dict[str, Dict]        = {}   # {reg_token: {expires_at, ...}}
_pending_cmds:    Dict[str, List]        = {}   # {node_id: [{cmd_id, command}]}
_cmd_results:     Dict[str, Dict]        = {}   # {cmd_id: {success, output}}
_ws_connections:  Dict[str, Any]         = {}   # {node_id: websocket}
_ws_server:       Any                    = None


# ══════════════════════════════════════════════════════════════════════════════
# Persistence
# ══════════════════════════════════════════════════════════════════════════════

def _load_nodes() -> None:
    global nodes
    try:
        with open(NODES_FILE) as fh:
            nodes = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        nodes = {}


def _save_nodes() -> None:
    try:
        with open(NODES_FILE, "w") as fh:
            json.dump(nodes, fh, indent=2)
    except Exception as exc:
        logger.error(f"[nodes] save_nodes failed: {exc}")


def _load_tokens() -> None:
    global _tokens
    try:
        with open(TOKENS_FILE) as fh:
            _tokens = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        _tokens = {}


def _save_tokens() -> None:
    try:
        with open(TOKENS_FILE, "w") as fh:
            json.dump(_tokens, fh, indent=2)
    except Exception as exc:
        logger.error(f"[nodes] save_tokens failed: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# Local node stats  (pure stdlib)
# ══════════════════════════════════════════════════════════════════════════════

def _read_cpu() -> float:
    try:
        def _snap():
            with open("/proc/stat") as f:
                parts = list(map(int, f.readline().split()[1:]))
            return sum(parts), parts[3]
        t1, i1 = _snap(); time.sleep(0.25); t2, i2 = _snap()
        return round((1 - (i2 - i1) / max(t2 - t1, 1)) * 100, 1)
    except Exception:
        return 0.0


def _read_ram() -> tuple[int, int]:
    try:
        info: dict = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = int(v.split()[0])
        total = info.get("MemTotal", 0) // 1024
        avail = info.get("MemAvailable", info.get("MemFree", 0)) // 1024
        return total - avail, total
    except Exception:
        return 0, 0


async def _count_local_vps() -> int:
    if not _run_docker:
        return 0
    out, _, rc = await _run_docker(
        'docker ps --filter "label=darknodes.vps=true" -q', timeout=10
    )
    if rc != 0:
        return 0
    return len([l for l in out.strip().splitlines() if l.strip()])


async def _collect_local_stats() -> Dict[str, Any]:
    cpu      = await asyncio.to_thread(_read_cpu)
    used, tot = await asyncio.to_thread(_read_ram)
    vps      = await _count_local_vps()
    return {"cpu": cpu, "ram_used_mb": used, "ram_total_mb": tot, "running_vps": vps}


# ══════════════════════════════════════════════════════════════════════════════
# Node helpers
# ══════════════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _node_online(node: dict) -> bool:
    if node.get("type") == "local":
        return True
    # Also consider online if the WebSocket is currently open
    if node.get("id") in _ws_connections:
        return True
    last = node.get("last_seen")
    if not last:
        return False
    try:
        return (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds() < OFFLINE_SECS
    except Exception:
        return False


def list_online_nodes() -> Dict[str, Dict]:
    return {nid: n for nid, n in nodes.items() if _node_online(n)}


def get_node(node_id: str) -> Optional[Dict]:
    return nodes.get(node_id)


def auto_select_node() -> str:
    candidates = list(list_online_nodes().items())
    if not candidates:
        return LOCAL_NODE_ID

    def _score(item):
        s    = item[1].get("stats", {})
        cpu  = s.get("cpu", 100.0)
        tot  = max(s.get("ram_total_mb", 1), 1)
        free = (tot - s.get("ram_used_mb", tot)) / tot * 100
        return (cpu, -free)

    return sorted(candidates, key=_score)[0][0]


# ══════════════════════════════════════════════════════════════════════════════
# Local node initialisation
# ══════════════════════════════════════════════════════════════════════════════

async def _init_local_node() -> None:
    if LOCAL_NODE_ID in nodes:
        return
    hostname = socket.gethostname()
    nodes[LOCAL_NODE_ID] = {
        "id":         LOCAL_NODE_ID,
        "name":       hostname,
        "type":       "local",
        "hostname":   hostname,
        "created_at": _now_iso(),
        "last_seen":  _now_iso(),
        "stats":      {},
        "secret":     "",
    }
    _save_nodes()
    logger.info(f"[nodes] Local node initialised (hostname={hostname})")


# ══════════════════════════════════════════════════════════════════════════════
# WebSocket server — persistent encrypted tunnel for node agents
# ══════════════════════════════════════════════════════════════════════════════

async def _ws_send(ws: Any, payload: dict) -> None:
    """Safely send a JSON message to a WebSocket connection."""
    try:
        await ws.send(json.dumps(payload))
    except Exception as exc:
        logger.debug(f"[nodes] ws_send failed: {exc}")


async def _ws_flush_pending(node_id: str) -> None:
    """Push any queued commands to a newly connected node."""
    ws = _ws_connections.get(node_id)
    if not ws:
        return
    cmds = _pending_cmds.pop(node_id, [])
    for cmd in cmds:
        await _ws_send(ws, {
            "type":    "command",
            "cmd_id":  cmd["cmd_id"],
            "command": cmd["command"],
        })


async def _ws_handler(websocket: Any) -> None:
    """
    Handle a single WebSocket connection from a node agent.

    First message must be either:
      {"type": "register", "token": "...", "hostname": "...", "public_ip": "..."}
      {"type": "auth",     "node_id": "...", "secret": "..."}

    Subsequent messages are heartbeats and command results.
    """
    node_id: Optional[str] = None
    remote = getattr(websocket, "remote_address", ("?", "?"))
    logger.info(f"[nodes] WebSocket connection from {remote[0]}:{remote[1]}")

    try:
        # ── Step 1: authenticate ──────────────────────────────────────────────
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=30)
        except asyncio.TimeoutError:
            await _ws_send(websocket, {"type": "error", "message": "Auth timeout"})
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await _ws_send(websocket, {"type": "error", "message": "Invalid JSON"})
            return

        msg_type = msg.get("type", "")

        if msg_type == "register":
            # New node registration
            token    = msg.get("token", "")
            hostname = msg.get("hostname", "unknown")
            pub_ip   = msg.get("public_ip", "")

            token_data = _tokens.get(token)
            if not token_data:
                await _ws_send(websocket, {"type": "error", "message": "Invalid registration token"})
                logger.warning(f"[nodes] Invalid token from {remote[0]}: {token[:16]}…")
                return

            # Check expiry
            try:
                exp = datetime.fromisoformat(token_data["expires_at"])
                if datetime.utcnow() > exp:
                    await _ws_send(websocket, {"type": "error", "message": "Registration token expired"})
                    del _tokens[token]
                    _save_tokens()
                    return
            except Exception:
                pass

            node_id = secrets.token_hex(6)
            secret  = secrets.token_hex(24)

            nodes[node_id] = {
                "id":         node_id,
                "name":       hostname,
                "type":       "remote",
                "hostname":   hostname,
                "public_ip":  pub_ip,
                "created_at": _now_iso(),
                "last_seen":  _now_iso(),
                "stats":      {},
                "secret":     secret,
            }
            _save_nodes()

            # Consume the token
            del _tokens[token]
            _save_tokens()

            await _ws_send(websocket, {
                "type":    "registered",
                "node_id": node_id,
                "secret":  secret,
            })
            logger.info(f"[nodes] Remote node registered: {node_id} ({hostname} @ {pub_ip or 'unknown IP'})")

        elif msg_type == "auth":
            # Reconnecting node
            node_id = msg.get("node_id", "")
            secret  = msg.get("secret", "")

            node = nodes.get(node_id)
            if not node or node.get("secret") != secret:
                await _ws_send(websocket, {"type": "error", "message": "Invalid credentials"})
                logger.warning(f"[nodes] Auth failed for node_id={node_id} from {remote[0]}")
                return

            await _ws_send(websocket, {"type": "auth_ok"})
            logger.info(f"[nodes] Node reconnected: {node_id} ({node.get('name', '?')} @ {remote[0]})")

        else:
            await _ws_send(websocket, {"type": "error", "message": f"Expected register or auth, got '{msg_type}'"})
            return

        # ── Step 2: store connection and flush pending commands ───────────────
        _ws_connections[node_id] = websocket
        await _ws_flush_pending(node_id)

        # ── Step 3: message loop ──────────────────────────────────────────────
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type", "")

            if mtype == "heartbeat":
                try:
                    node = nodes.get(node_id, {})
                    node["stats"] = {
                        "cpu":          float(msg.get("cpu", 0)),
                        "ram_used_mb":  int(msg.get("ram_used_mb", 0)),
                        "ram_total_mb": int(msg.get("ram_total_mb", 0)),
                        "running_vps":  int(msg.get("running_vps", 0)),
                    }
                    node["last_seen"] = _now_iso()
                    _save_nodes()
                except Exception as exc:
                    logger.debug(f"[nodes] heartbeat parse error: {exc}")
                await _ws_send(websocket, {"type": "ok"})

            elif mtype == "result":
                cmd_id  = msg.get("cmd_id", "")
                success = bool(msg.get("success", False))
                output  = msg.get("output", "")
                error   = msg.get("error", "")
                _cmd_results[cmd_id] = {
                    "success": success,
                    "output":  output,
                    "error":   error if not success else "",
                }
                logger.debug(f"[nodes] Result for cmd {cmd_id}: success={success}")

            elif mtype == "log":
                level = msg.get("level", "info")
                text  = msg.get("message", "")
                log_fn = getattr(logger, level, logger.info)
                log_fn(f"[node:{node_id}] {text}")

            elif mtype == "pong":
                pass  # keepalive response

            else:
                logger.debug(f"[nodes] Unknown message type from {node_id}: {mtype}")

    except Exception as exc:
        logger.info(f"[nodes] WebSocket connection closed for node {node_id}: {type(exc).__name__}")

    finally:
        if node_id and _ws_connections.get(node_id) is websocket:
            del _ws_connections[node_id]
            logger.info(f"[nodes] Node {node_id} disconnected — will reconnect automatically")


# ══════════════════════════════════════════════════════════════════════════════
# Remote command execution  (used by the deploy / manage flows in bot.py)
# ══════════════════════════════════════════════════════════════════════════════

async def remote_execute(node_id: str, command: str, timeout: int = 120) -> str:
    """
    Run a shell command on a remote node and return its stdout.

    If the node is currently connected via WebSocket the command is pushed
    immediately.  If the node is offline the command is queued and will be
    delivered the next time the agent reconnects.
    """
    cmd_id = secrets.token_hex(8)

    ws = _ws_connections.get(node_id)
    if ws:
        await _ws_send(ws, {
            "type":    "command",
            "cmd_id":  cmd_id,
            "command": command,
        })
    else:
        # Queue for delivery on next connection
        _pending_cmds.setdefault(node_id, []).append({
            "cmd_id":  cmd_id,
            "command": command,
        })
        logger.info(f"[nodes] Node {node_id} offline — command {cmd_id} queued")

    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(0.5)
        result = _cmd_results.get(cmd_id)
        if result is not None:
            del _cmd_results[cmd_id]
            if not result.get("success"):
                raise RuntimeError(result.get("error") or "Remote command failed")
            return result.get("output", "")

    # Tidy up the pending queue if still there
    if node_id in _pending_cmds:
        _pending_cmds[node_id] = [
            c for c in _pending_cmds[node_id] if c["cmd_id"] != cmd_id
        ]
    raise RuntimeError(f"Remote command timed out after {timeout}s (cmd_id={cmd_id})")


# ══════════════════════════════════════════════════════════════════════════════
# Keepalive loop — ping all connected nodes every 30 s
# ══════════════════════════════════════════════════════════════════════════════

async def _keepalive_loop() -> None:
    while True:
        await asyncio.sleep(30)
        dead = []
        for nid, ws in list(_ws_connections.items()):
            try:
                await _ws_send(ws, {"type": "ping"})
            except Exception:
                dead.append(nid)
        for nid in dead:
            if _ws_connections.get(nid) is not None:
                del _ws_connections[nid]


# ══════════════════════════════════════════════════════════════════════════════
# Local stats refresh loop
# ══════════════════════════════════════════════════════════════════════════════

async def _local_stats_loop() -> None:
    while True:
        try:
            stats = await _collect_local_stats()
            if LOCAL_NODE_ID in nodes:
                nodes[LOCAL_NODE_ID]["stats"]     = stats
                nodes[LOCAL_NODE_ID]["last_seen"] = _now_iso()
                _save_nodes()
        except Exception as exc:
            logger.debug(f"[nodes] local stats loop error: {exc}")
        await asyncio.sleep(30)


# ══════════════════════════════════════════════════════════════════════════════
# Discord embed helpers
# ══════════════════════════════════════════════════════════════════════════════

def _brand() -> str:
    return _get_brand() if _get_brand else "DarkNodes"


def _logo() -> str:
    return _get_logo() if _get_logo else ""


def _embed(title: str, color: int = 0x5865F2) -> discord.Embed:
    e    = discord.Embed(title=title, color=color,
                         timestamp=datetime.now(timezone.utc).replace(tzinfo=None))
    logo = _logo()
    if logo:
        e.set_author(name=f"{_brand()} Node System", icon_url=logo)
        e.set_thumbnail(url=logo)
    e.set_footer(text=f"{_brand()}  •  Node System")
    return e


def _err(title: str, desc: str) -> discord.Embed:
    e = _embed(f"❌  {title}", 0xED4245)
    e.description = desc
    return e


def _status_dot(node: dict) -> str:
    return "🟢" if _node_online(node) else "🔴"


def _stats_line(node: dict) -> str:
    s   = node.get("stats", {})
    cpu = s.get("cpu", "—")
    u   = s.get("ram_used_mb")
    t   = s.get("ram_total_mb")
    ram = f"{u}/{t} MB" if u is not None and t else "—"
    vps = s.get("running_vps", "—")
    return f"CPU `{cpu}%`  RAM `{ram}`  VPSes `{vps}`"


def _get_ws_url() -> str:
    """Return the WebSocket URL the node agent should connect to."""
    # Prefer explicit env var
    ws_url = os.environ.get("NODE_WS_URL", "")
    if ws_url:
        return ws_url
    # Fall back to SERVER_IP if known
    server_ip = os.environ.get("SERVER_IP", "")
    if server_ip:
        return f"ws://{server_ip}:{WS_PORT}"
    return f"ws://<YOUR_BOT_IP>:{WS_PORT}"


# ══════════════════════════════════════════════════════════════════════════════
# Node selection UI
# ══════════════════════════════════════════════════════════════════════════════

class _NodeSelectView(discord.ui.View):
    def __init__(self, online: Dict[str, Dict]):
        super().__init__(timeout=SELECT_TIMEOUT_S)
        self._chosen: Optional[str] = None
        self._event   = asyncio.Event()

        opts = [discord.SelectOption(
            label="⭐  Auto Select (healthiest node)",
            value="__auto__",
            description="Bot picks the node with most free resources",
            default=True,
        )]
        for nid, n in online.items():
            s     = n.get("stats", {})
            cpu   = s.get("cpu", "?")
            u     = s.get("ram_used_mb")
            t     = s.get("ram_total_mb")
            ram   = f"{u}/{t}MB" if u is not None and t else "?"
            tag   = "(local)" if nid == LOCAL_NODE_ID else "(remote)"
            opts.append(discord.SelectOption(
                label=f"{n.get('name', nid)} {tag}",
                value=nid,
                description=f"CPU {cpu}%  RAM {ram}  VPSes {s.get('running_vps','?')}",
            ))

        sel = discord.ui.Select(placeholder="Choose a deployment node…", options=opts, row=0)
        sel.callback = self._on_select
        self.add_item(sel)
        self._sel = sel

        ok = discord.ui.Button(label="Deploy Here", style=discord.ButtonStyle.success,
                               row=1, emoji="🚀")
        ok.callback = self._on_ok
        self.add_item(ok)

        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger,
                                   row=1, emoji="✖")
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_select(self, i: discord.Interaction):
        await i.response.defer()

    async def _on_ok(self, i: discord.Interaction):
        val = self._sel.values[0] if self._sel.values else "__auto__"
        self._chosen = auto_select_node() if val == "__auto__" else val
        await i.response.defer()
        self._event.set()
        self.stop()

    async def _on_cancel(self, i: discord.Interaction):
        self._chosen = None
        await i.response.send_message("❌ Deployment cancelled.", ephemeral=True)
        self._event.set()
        self.stop()

    async def on_timeout(self):
        self._event.set()


async def maybe_select_node(ctx) -> Optional[str]:
    """
    Returns a node_id for deployment.
    • Single node → returns immediately (no UI shown).
    • Multiple nodes → shows a Discord selection UI and waits.
    • Returns None if cancelled / timed out.
    """
    online = list_online_nodes()
    if len(online) <= 1:
        return next(iter(online), LOCAL_NODE_ID)

    embed = _embed("🖥️  Select Deployment Node", 0x5865F2)
    embed.description = (
        "Multiple nodes are available.\n"
        "Pick where this VPS should be deployed, or let the bot choose."
    )
    for nid, n in online.items():
        tag = "`local`" if nid == LOCAL_NODE_ID else "`remote`"
        embed.add_field(
            name=f"{_status_dot(n)}  {n.get('name', nid)}  {tag}",
            value=_stats_line(n),
            inline=False,
        )

    view = _NodeSelectView(online)
    msg  = await ctx.send(embed=embed, view=view)
    await view._event.wait()
    try:
        await msg.delete()
    except Exception:
        pass
    return view._chosen


# ══════════════════════════════════════════════════════════════════════════════
# Admin check helper
# ══════════════════════════════════════════════════════════════════════════════

def _is_admin(interaction: discord.Interaction) -> bool:
    uid = str(interaction.user.id)
    return uid == _main_admin_id or (
        bool(_admin_data) and uid in _admin_data.get("admins", [])
    )


# ══════════════════════════════════════════════════════════════════════════════
# Slash commands   /node  group
# ══════════════════════════════════════════════════════════════════════════════

def register_commands(bot) -> None:
    """Register /node slash commands."""

    node_group = app_commands.Group(
        name="node",
        description="Manage DarkNodes deployment nodes",
    )

    # ── /node add ─────────────────────────────────────────────────────────────
    @node_group.command(
        name="add",
        description="Generate a one-time token to register a new remote node (Admin only)",
    )
    async def node_add(interaction: discord.Interaction):
        if not _is_admin(interaction):
            await interaction.response.send_message(
                embed=_err("Access Denied", "Only admins can add nodes."), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)

        # Generate a short-lived registration token
        token      = secrets.token_urlsafe(24)
        expires_at = (datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRY_MIN)).isoformat()
        _tokens[token] = {"expires_at": expires_at}
        _save_tokens()

        ws_url  = _get_ws_url()
        exp_ts  = int((datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRY_MIN)).timestamp())

        embed = _embed("➕  New Node Registration", 0x57F287)
        embed.description = (
            "Run the **Node Agent** on the remote machine to connect it.\n"
            "No public IP or open ports are needed on the node — "
            "it only makes an outbound connection to this bot."
        )
        embed.add_field(
            name="🖥️  First-time setup (one command)",
            value=(
                f"```bash\n"
                f"# Download & run on the remote machine\n"
                f"python3 node_agent.py \\\n"
                f"  --bot-url {ws_url} \\\n"
                f"  --token   {token}\n"
                f"```"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔗  Bot WebSocket URL",
            value=f"`{ws_url}`",
            inline=True,
        )
        embed.add_field(
            name="⏰  Token Expires",
            value=f"<t:{exp_ts}:R>",
            inline=True,
        )
        embed.add_field(
            name="ℹ️  Requirements",
            value=(
                "• Python 3.8+  and  `pip install websockets`\n"
                "• Outbound internet access only — **no public IP required**\n"
                "• Works behind NAT, CGNAT, VPN, firewalls\n"
                "• Token is single-use and expires automatically"
            ),
            inline=False,
        )

        if "<YOUR_BOT_IP>" in ws_url:
            embed.add_field(
                name="⚠️  Set your bot's IP",
                value=(
                    "The `NODE_WS_URL` (or `SERVER_IP`) environment variable is not set.\n"
                    "Replace `<YOUR_BOT_IP>` in the command above with this bot server's public IP."
                ),
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /node remove ─────────────────────────────────────────────────────────
    @node_group.command(
        name="remove",
        description="Remove a registered node (Admin only)",
    )
    @app_commands.describe(node_id="Node ID shown in /node list")
    async def node_remove(interaction: discord.Interaction, node_id: str):
        if not _is_admin(interaction):
            await interaction.response.send_message(
                embed=_err("Access Denied", "Only admins can remove nodes."), ephemeral=True
            )
            return

        if node_id == LOCAL_NODE_ID:
            await interaction.response.send_message(
                embed=_err("Cannot Remove", "The local node cannot be removed."), ephemeral=True
            )
            return

        node = nodes.pop(node_id, None)
        if not node:
            await interaction.response.send_message(
                embed=_err("Node Not Found", f"No node with ID `{node_id}`."), ephemeral=True
            )
            return

        # Close active WebSocket if connected
        ws = _ws_connections.pop(node_id, None)
        if ws:
            try:
                await ws.close(code=1001, reason="Node removed by admin")
            except Exception:
                pass

        _save_nodes()
        e = _embed("🗑️  Node Removed", 0x57F287)
        e.add_field(name="ID",   value=f"`{node_id}`",             inline=True)
        e.add_field(name="Name", value=f"`{node.get('name','?')}`", inline=True)
        await interaction.response.send_message(embed=e)

    # ── /node rename ──────────────────────────────────────────────────────────
    @node_group.command(
        name="rename",
        description="Give a node a custom display name (Admin only)",
    )
    @app_commands.describe(
        node_id="Node ID shown in /node list",
        name="New display name",
    )
    async def node_rename(interaction: discord.Interaction, node_id: str, name: str):
        if not _is_admin(interaction):
            await interaction.response.send_message(
                embed=_err("Access Denied", "Only admins can rename nodes."), ephemeral=True
            )
            return

        name = name.strip()[:64]
        if not name:
            await interaction.response.send_message(
                embed=_err("Invalid Name", "Name cannot be empty."), ephemeral=True
            )
            return

        node = nodes.get(node_id)
        if not node:
            await interaction.response.send_message(
                embed=_err("Node Not Found", f"No node with ID `{node_id}`."),
                ephemeral=True,
            )
            return

        old   = node.get("name", node_id)
        node["name"] = name
        _save_nodes()

        e = _embed("✏️  Node Renamed", 0x57F287)
        e.add_field(name="ID",       value=f"`{node_id}`", inline=True)
        e.add_field(name="Old Name", value=f"`{old}`",     inline=True)
        e.add_field(name="New Name", value=f"`{name}`",    inline=True)
        await interaction.response.send_message(embed=e)

    # ── /node list ────────────────────────────────────────────────────────────
    @node_group.command(
        name="list",
        description="Show all registered nodes and their current status",
    )
    async def node_list(interaction: discord.Interaction):
        await interaction.response.defer()

        if not nodes:
            await interaction.followup.send(
                embed=_err("No Nodes", "No nodes registered yet. Use `/node add` to add one.")
            )
            return

        embed = _embed("🖥️  Node List", 0x5865F2)
        embed.description = f"**{len(nodes)}** node(s) registered"

        for nid, n in nodes.items():
            online  = _node_online(n)
            status  = "🟢 **Online**" if online else "🔴 **Offline**"
            tag     = "`Local`" if nid == LOCAL_NODE_ID else "`Remote`"
            s       = n.get("stats", {})
            cpu     = f"`{s.get('cpu', '—')}%`"
            u, t    = s.get("ram_used_mb"), s.get("ram_total_mb")
            ram     = f"`{u}/{t} MB`" if u is not None and t else "`—`"
            vps_c   = f"`{s.get('running_vps', '—')}`"
            ip_line = f"\n**IP:** `{n['public_ip']}`" if n.get("public_ip") else ""
            ws_line = "\n**Tunnel:** `🔌 connected`" if nid in _ws_connections else ""

            embed.add_field(
                name=f"{_status_dot(n)}  {n.get('name', nid)}  {tag}",
                value=(
                    f"**ID:** `{nid}`\n"
                    f"**Status:** {status}{ip_line}{ws_line}\n"
                    f"**CPU:** {cpu}  **RAM:** {ram}  **VPSes:** {vps_c}"
                ),
                inline=False,
            )

        await interaction.followup.send(embed=embed)

    # ── /node status ──────────────────────────────────────────────────────────
    @node_group.command(
        name="status",
        description="Show detailed status for a specific node",
    )
    @app_commands.describe(node_id="Node ID (leave blank for all)")
    async def node_status(interaction: discord.Interaction, node_id: str = ""):
        await interaction.response.defer()

        target = {node_id: nodes[node_id]} if node_id and node_id in nodes else nodes
        if not target:
            await interaction.followup.send(
                embed=_err("Not Found", f"Node `{node_id}` does not exist.")
            )
            return

        for nid, n in target.items():
            online    = _node_online(n)
            connected = nid in _ws_connections
            s         = n.get("stats", {})
            e = _embed(f"🖥️  {n.get('name', nid)}", 0x57F287 if online else 0xED4245)
            e.add_field(name="ID",      value=f"`{nid}`",                  inline=True)
            e.add_field(name="Type",    value=n.get("type", "?"),          inline=True)
            e.add_field(name="Status",  value="🟢 Online" if online else "🔴 Offline", inline=True)
            e.add_field(name="Tunnel",  value="🔌 Connected" if connected else "⭕ Disconnected", inline=True)
            e.add_field(name="Host",    value=f"`{n.get('hostname','?')}`", inline=True)
            e.add_field(name="IP",      value=f"`{n.get('public_ip','—')}`", inline=True)
            if s:
                e.add_field(name="CPU",     value=f"`{s.get('cpu','—')}%`",   inline=True)
                u, t = s.get("ram_used_mb"), s.get("ram_total_mb")
                e.add_field(name="RAM",     value=f"`{u}/{t} MB`" if u and t else "`—`", inline=True)
                e.add_field(name="VPSes",   value=f"`{s.get('running_vps','—')}`", inline=True)
            e.add_field(name="Last Seen", value=f"`{n.get('last_seen','never')}`", inline=False)
            await interaction.followup.send(embed=e)

    bot.tree.add_command(node_group)
    logger.info("[nodes] /node command group registered")


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def init(
    docker_exec_fn:  Callable,
    run_docker_fn:   Callable,
    get_logo_fn:     Callable,
    get_brand_fn:    Callable,
    main_admin_id:   str,
    admin_data_ref:  Optional[dict] = None,
    get_server_ip_fn: Optional[Callable] = None,
) -> None:
    """Inject bot helpers. Call BEFORE register_commands() and startup()."""
    global _docker_exec, _run_docker, _get_logo, _get_brand, _main_admin_id, _admin_data, _get_server_ip
    _docker_exec   = docker_exec_fn
    _run_docker    = run_docker_fn
    _get_logo      = get_logo_fn
    _get_brand     = get_brand_fn
    _main_admin_id = str(main_admin_id)
    _admin_data    = admin_data_ref
    _get_server_ip = get_server_ip_fn

    _load_nodes()
    _load_tokens()
    logger.info("[nodes] node_system initialised")


async def startup(bot_instance=None) -> None:
    """
    Call from on_ready as:  asyncio.create_task(node_system.startup())

    • Ensures the local node exists.
    • Starts the local stats refresh loop.
    • Starts the WebSocket server for incoming node agent connections.
    """
    global _bot, _ws_server
    if bot_instance is not None:
        _bot = bot_instance

    await _init_local_node()
    asyncio.create_task(_local_stats_loop())
    asyncio.create_task(_keepalive_loop())

    if not _WS_AVAILABLE:
        logger.error(
            "[nodes] 'websockets' package is not installed — remote nodes will not work.\n"
            "        Install it with:  pip install websockets"
        )
        return

    try:
        _ws_server = await ws_serve(_ws_handler, WS_HOST, WS_PORT)
        logger.info(
            f"[nodes] WebSocket server listening on {WS_HOST}:{WS_PORT}  "
            f"— agents connect to {_get_ws_url()}"
        )
    except OSError as exc:
        logger.error(
            f"[nodes] Failed to start WebSocket server on port {WS_PORT}: {exc}\n"
            f"        Set NODE_WS_PORT env var to use a different port."
        )
