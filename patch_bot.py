"""
Patches bot.py with all new features:
  - Analytics data helpers
  - Scheduled backups data helpers
  - Shared access token helpers
  - VPS scanner / fix / cleanup helpers
  - /fix, /cleanup, /schedule-backup, /share-vps, /accept-access, /analytics, /guided-setup commands
  - Background loops (scheduled_backup_loop, smart_notification_loop)
  - Cleanup button in both ManageView.add_action_buttons copies
  - Loop starts in both on_ready handlers
All insertions are done bottom-to-top so earlier offsets remain valid.
"""
import re, sys

with open("bot.py", "r") as f:
    src = f.read()

lines = src.splitlines(keepends=True)
total = len(lines)
print(f"Starting with {total} lines")

# ─────────────────────────────────────────────────────────────────────────────
# Helper: insert a block of text before a given 1-based line number
# ─────────────────────────────────────────────────────────────────────────────
def insert_before(line_no: int, text: str):
    """Insert text before line_no (1-based)."""
    global lines
    lines.insert(line_no - 1, text if text.endswith("\n") else text + "\n")

def insert_after_last_match(pattern: str, text: str, occurrence: int = 1) -> int:
    """Insert text after the Nth occurrence of pattern. Returns insertion line (1-based)."""
    global lines
    count = 0
    for i, ln in enumerate(lines):
        if pattern in ln:
            count += 1
            if count == occurrence:
                lines.insert(i + 1, text if text.endswith("\n") else text + "\n")
                return i + 2  # 1-based line after insertion
    raise ValueError(f"Pattern not found (occurrence {occurrence}): {pattern!r}")

# ─────────────────────────────────────────────────────────────────────────────
# Find key line numbers BEFORE any mutations
# ─────────────────────────────────────────────────────────────────────────────
def find_line(pattern: str, occurrence: int = 1) -> int:
    """Return 1-based line number of the Nth line containing pattern."""
    count = 0
    for i, ln in enumerate(lines):
        if pattern in ln:
            count += 1
            if count == occurrence:
                return i + 1
    raise ValueError(f"Pattern not found (occ {occurrence}): {pattern!r}")

# Anchors (found once, used for bottom-to-top insertion)
LINE_set_all_embed_colors_end  = find_line("_save_bot_config(_bot_config)", 1)  # inside set_all_embed_colors
LINE_first_add_action_ssh      = find_line("self.add_item(ssh_button)", 1)
LINE_first_on_ready_abuse_end  = find_line("abuse_monitor.start()", 1)
LINE_slash_cmds_before_main1   = find_line('if __name__ == "__main__":', 1)
LINE_second_add_action_ssh     = find_line("self.add_item(ssh_button)", 2)
LINE_second_on_ready_abuse_end = find_line("abuse_monitor.start()", 2)
LINE_second_auto_expire        = find_line("@tasks.loop(hours=1)", 2)   # second auto_expire_check loop

print(f"set_all_embed_colors end:  line {LINE_set_all_embed_colors_end}")
print(f"first  ssh_button add:     line {LINE_first_add_action_ssh}")
print(f"first  abuse_monitor.start: line {LINE_first_on_ready_abuse_end}")
print(f"first  __main__:           line {LINE_slash_cmds_before_main1}")
print(f"second ssh_button add:     line {LINE_second_add_action_ssh}")
print(f"second abuse_monitor.start: line {LINE_second_on_ready_abuse_end}")
print(f"second auto_expire loop:   line {LINE_second_auto_expire}")

# ─────────────────────────────────────────────────────────────────────────────
# BLOCK A: Data helpers + VPS scanner/fix/cleanup (insert after set_all_embed_colors)
# ─────────────────────────────────────────────────────────────────────────────
BLOCK_A = '''
# ──────────────────────────────────────────────────────────────────────────────
# ANALYTICS DATA
# ──────────────────────────────────────────────────────────────────────────────
_ANALYTICS_FILE = "analytics_data.json"
_DEFAULT_ANALYTICS: dict = {
    "total_installs": 0,
    "successful_installs": 0,
    "failed_installs": 0,
    "install_times": [],
    "template_counts": {},
    "installs_history": [],
}

def load_analytics() -> dict:
    try:
        with open(_ANALYTICS_FILE) as _f:
            _d = json.load(_f)
        for _k, _v in _DEFAULT_ANALYTICS.items():
            _d.setdefault(_k, _v)
        return _d
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULT_ANALYTICS)

def save_analytics(data: dict) -> None:
    with open(_ANALYTICS_FILE, "w") as _f:
        json.dump(data, _f, indent=2)

def record_install(user_id: str, template: str, success: bool, duration_s: float) -> None:
    """Record an installation event for analytics tracking."""
    _d = load_analytics()
    _d["total_installs"] += 1
    if success:
        _d["successful_installs"] += 1
        _d["install_times"].append(round(duration_s, 1))
        _d["install_times"] = _d["install_times"][-500:]
    else:
        _d["failed_installs"] += 1
    _d["template_counts"][template] = _d["template_counts"].get(template, 0) + 1
    _d["installs_history"].append({
        "timestamp": _utcnow().isoformat(),
        "user_id": str(user_id),
        "template": template,
        "success": success,
        "duration": round(duration_s, 1),
    })
    _d["installs_history"] = _d["installs_history"][-1000:]
    save_analytics(_d)

# ──────────────────────────────────────────────────────────────────────────────
# SCHEDULED BACKUPS DATA
# ──────────────────────────────────────────────────────────────────────────────
_SCHEDULES_FILE = "backup_schedules.json"

def load_backup_schedules() -> dict:
    try:
        with open(_SCHEDULES_FILE) as _f:
            return json.load(_f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_backup_schedules(data: dict) -> None:
    with open(_SCHEDULES_FILE, "w") as _f:
        json.dump(data, _f, indent=2)

def _next_backup_dt(frequency: str, from_dt=None):
    _now = from_dt or _utcnow()
    if frequency == "daily":
        return _now + timedelta(days=1)
    if frequency == "weekly":
        return _now + timedelta(weeks=1)
    return _now + timedelta(days=30)   # monthly

# ──────────────────────────────────────────────────────────────────────────────
# SHARED ACCESS TOKEN DATA
# ──────────────────────────────────────────────────────────────────────────────
_SHARED_ACCESS_FILE = "shared_access.json"
_SHARED_PERMS = ("view", "restart", "full")

def load_shared_access() -> dict:
    try:
        with open(_SHARED_ACCESS_FILE) as _f:
            return json.load(_f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_shared_access(data: dict) -> None:
    with open(_SHARED_ACCESS_FILE, "w") as _f:
        json.dump(data, _f, indent=2)

def _prune_expired_tokens(data: dict) -> dict:
    _now = _utcnow()
    return {
        _k: _v for _k, _v in data.items()
        if not _v.get("expires_at")
        or datetime.fromisoformat(_v["expires_at"]) > _now
    }

# ──────────────────────────────────────────────────────────────────────────────
# VPS TROUBLESHOOTER — scanner, fix applier, cleanup runner
# ──────────────────────────────────────────────────────────────────────────────

async def scan_vps_issues(container_name: str) -> list:
    """Scan a container for common issues. Returns a list of issue dicts."""
    _checks = {
        "disk":      "df / --output=pcent 2>/dev/null | tail -1 | tr -d \\' \\'",
        "services":  "systemctl list-units --state=failed --no-legend 2>/dev/null | wc -l",
        "zombies":   "ps aux 2>/dev/null | awk \\'$8==\"Z\"\\' | wc -l",
        "load":      "awk \\'{print $1}\\' /proc/loadavg 2>/dev/null || echo 0",
        "mem_free":  "awk \\'/MemAvailable/{print $2}\\' /proc/meminfo 2>/dev/null || echo 999999",
        "mem_total": "awk \\'/MemTotal/{print $2}\\' /proc/meminfo 2>/dev/null || echo 1",
        "ssh_root":  "grep -E \\'^PermitRootLogin\\' /etc/ssh/sshd_config 2>/dev/null | awk \\'{print $2}\\' || echo no",
        "upgrades":  "apt-get -s upgrade 2>/dev/null | grep -c \\'^Inst\\' || echo 0",
    }
    _results: dict = {}
    for _key, _cmd in _checks.items():
        try:
            _results[_key] = str(await docker_exec(container_name, _cmd, timeout=12)).strip()
        except Exception:
            _results[_key] = ""

    _issues = []

    try:
        _pct = int(_results.get("disk", "0"))
        if _pct >= 85:
            _sev = "🔴 Critical" if _pct >= 95 else "🟡 Warning"
            _issues.append({"type": "disk_full", "severity": _sev,
                            "title": "💽 Disk Nearly Full",
                            "detail": f"Disk is **{_pct}%** used.",
                            "fix_label": "🧹 Clean Disk", "fixable": True})
    except Exception:
        pass

    try:
        _n = int(_results.get("services", "0"))
        if _n > 0:
            _issues.append({"type": "failed_services", "severity": "🟡 Warning",
                            "title": "🔴 Failed Services",
                            "detail": f"**{_n}** systemd service(s) are in a failed state.",
                            "fix_label": "🔄 Restart Services", "fixable": True})
    except Exception:
        pass

    try:
        _n = int(_results.get("zombies", "0"))
        if _n > 0:
            _issues.append({"type": "zombie_processes", "severity": "ℹ️ Info",
                            "title": "🧟 Zombie Processes",
                            "detail": f"**{_n}** zombie process(es) detected (harmless but worth monitoring).",
                            "fix_label": None, "fixable": False})
    except Exception:
        pass

    try:
        _load = float(_results.get("load", "0"))
        if _load > 3.0:
            _issues.append({"type": "high_load", "severity": "🟡 Warning",
                            "title": "📈 High CPU Load",
                            "detail": f"1-minute load average is **{_load:.2f}** (high).",
                            "fix_label": None, "fixable": False})
    except Exception:
        pass

    try:
        _free_kb  = int(_results.get("mem_free",  "999999"))
        _total_kb = int(_results.get("mem_total", "1")) or 1
        _pct_used = 100 - (_free_kb / _total_kb * 100)
        if _pct_used >= 90:
            _sev = "🔴 Critical" if _pct_used >= 95 else "🟡 Warning"
            _issues.append({"type": "oom_risk", "severity": _sev,
                            "title": "🧠 RAM Near Limit",
                            "detail": f"RAM is **{_pct_used:.0f}%** used.",
                            "fix_label": "🧹 Drop Cache", "fixable": True})
    except Exception:
        pass

    _ssh_val = _results.get("ssh_root", "no").strip().lower()
    if _ssh_val in ("yes", "prohibit-password", "without-password"):
        _issues.append({"type": "ssh_config", "severity": "ℹ️ Info",
                        "title": "🔒 SSH Root Login Enabled",
                        "detail": f"`PermitRootLogin {_ssh_val}` — root login is allowed via SSH.",
                        "fix_label": "🔒 Disable Root Login", "fixable": True})

    try:
        _n = int(_results.get("upgrades", "0"))
        if _n > 0:
            _issues.append({"type": "no_updates", "severity": "ℹ️ Info",
                            "title": "📦 Security Updates Pending",
                            "detail": f"**{_n}** package update(s) are available.",
                            "fix_label": "⬆️ Apply Updates", "fixable": True})
    except Exception:
        pass

    return _issues


async def apply_vps_fix(container_name: str, fix_type: str) -> str:
    """Apply an automated fix to a container. Returns a human-readable result."""
    _cmds = {
        "disk_full": (
            "journalctl --vacuum-size=50M 2>/dev/null ; "
            "apt-get clean -y 2>/dev/null ; "
            "rm -rf /tmp/* /var/tmp/* 2>/dev/null ; "
            "echo freed"
        ),
        "failed_services": (
            "systemctl list-units --state=failed --no-legend 2>/dev/null "
            "| awk \\'NR>0{print $2}\\' | xargs -r systemctl restart 2>/dev/null ; echo done"
        ),
        "oom_risk": "sync ; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null ; echo done",
        "ssh_config": (
            "sed -i \\'s/^PermitRootLogin.*/PermitRootLogin no/\\' /etc/ssh/sshd_config 2>/dev/null ; "
            "systemctl reload ssh 2>/dev/null || service ssh reload 2>/dev/null ; echo done"
        ),
        "no_updates": (
            "DEBIAN_FRONTEND=noninteractive apt-get upgrade -y "
            "-o Dpkg::Options::=\\'--force-confold\\' 2>&1 | tail -3"
        ),
    }
    _cmd = _cmds.get(fix_type)
    if not _cmd:
        return "No automated fix is available for this issue."
    try:
        _out = await docker_exec(container_name, _cmd, timeout=180)
        return str(_out).strip() or "✅ Fix applied successfully."
    except Exception as _e:
        return f"❌ Fix failed: {_e}"


async def run_vps_cleanup(container_name: str) -> dict:
    """Run a full cleanup on a container. Returns a dict with freed_mb."""
    _before_cmd = "df / --output=used 2>/dev/null | tail -1 || echo 0"
    _cleanup_cmd = (
        "journalctl --vacuum-size=10M 2>/dev/null ; "
        "apt-get autoremove -y 2>/dev/null ; "
        "apt-get clean 2>/dev/null ; "
        "find /var/log -name \\'*.gz\\' -delete 2>/dev/null ; "
        "find /var/log -name \\'*.1\\'  -delete 2>/dev/null ; "
        "rm -rf /tmp/* /var/tmp/* 2>/dev/null ; "
        "df / --output=used 2>/dev/null | tail -1 || echo 0"
    )
    try:
        _before = int(str(await docker_exec(container_name, _before_cmd, timeout=10)).strip())
    except Exception:
        _before = 0
    try:
        _after_raw = str(await docker_exec(container_name, _cleanup_cmd, timeout=180)).strip()
        _after = int(_after_raw.split("\\n")[-1].strip())
    except Exception:
        _after = _before
    _freed_kb = max(0, _before - _after)
    return {"freed_mb": round(_freed_kb / 1024, 1), "before_kb": _before, "after_kb": _after}

'''

# ─────────────────────────────────────────────────────────────────────────────
# BLOCK B: All new slash commands + UI classes + background loops
# (inserted before first __main__)
# ─────────────────────────────────────────────────────────────────────────────
BLOCK_B = '''
# ══════════════════════════════════════════════════════════════════════════════
# NEW FEATURES — /fix, /cleanup, /schedule-backup, /share-vps, /analytics,
#                /guided-setup, /accept-access + background loops
# ══════════════════════════════════════════════════════════════════════════════

# ─── /fix — AI Troubleshooter ─────────────────────────────────────────────────

def _build_scan_embed(container_name: str, issues: list) -> discord.Embed:
    if not issues:
        return create_embed(
            f"✅ VPS Health Check — {container_name}",
            "No issues detected. Everything looks great!",
            0x57F287,
        )
    _emb = create_embed(
        f"🤖 AI Troubleshooter — {container_name}",
        f"Found **{len(issues)}** issue(s). Select a fix below.",
        0xED4245,
    )
    for _iss in issues:
        _emb.add_field(
            name=f"{_iss[\'severity\']} — {_iss[\'title\']}",
            value=_iss["detail"],
            inline=False,
        )
    return _emb


class _FixView(discord.ui.View):
    def __init__(self, container_name: str, issues: list, user_id: int):
        super().__init__(timeout=120)
        self.container_name = container_name
        self.issues = issues
        self.user_id = user_id
        _row = 0
        for _iss in issues:
            if not _iss["fixable"]:
                continue
            _btn = discord.ui.Button(
                label=_iss["fix_label"],
                style=discord.ButtonStyle.primary,
                row=min(_row // 4, 3),
            )
            _row += 1
            async def _cb(_inter: discord.Interaction, __type=_iss["type"]):
                if _inter.user.id != self.user_id:
                    await _inter.response.send_message("This panel isn\'t yours.", ephemeral=True)
                    return
                await _inter.response.defer(ephemeral=True, thinking=True)
                _result = await apply_vps_fix(self.container_name, __type)
                await _inter.followup.send(
                    embed=create_embed("🔧 Fix Applied", _result, 0x57F287),
                    ephemeral=True,
                )
            _btn.callback = _cb
            self.add_item(_btn)
        _rescan_btn = discord.ui.Button(
            label="🔄 Re-scan",
            style=discord.ButtonStyle.secondary,
            row=4,
        )
        async def _rescan(_inter: discord.Interaction):
            if _inter.user.id != self.user_id:
                await _inter.response.send_message("This panel isn\'t yours.", ephemeral=True)
                return
            await _inter.response.defer(ephemeral=True, thinking=True)
            _new_issues = await scan_vps_issues(self.container_name)
            _emb = _build_scan_embed(self.container_name, _new_issues)
            _new_view = _FixView(self.container_name, _new_issues, self.user_id)
            await _inter.followup.send(embed=_emb, view=_new_view, ephemeral=True)
        _rescan_btn.callback = _rescan
        self.add_item(_rescan_btn)


@bot.tree.command(name="fix", description="Scan a VPS for issues and apply one-click fixes")
@app_commands.describe(vps_number="Which VPS to scan (1, 2, 3…). Defaults to your first VPS.")
async def fix_cmd(interaction: discord.Interaction, vps_number: int = 1):
    _uid = str(interaction.user.id)
    _user_vps = vps_data.get(_uid, [])
    if not _user_vps:
        await interaction.response.send_message(
            embed=create_error_embed("No VPS", "You don\'t have any VPS to scan."), ephemeral=True)
        return
    _idx = max(0, min(vps_number - 1, len(_user_vps) - 1))
    _container = _user_vps[_idx]["container_name"]
    await interaction.response.defer(ephemeral=True, thinking=True)
    _issues = await scan_vps_issues(_container)
    _emb = _build_scan_embed(_container, _issues)
    _view = _FixView(_container, _issues, interaction.user.id)
    await interaction.followup.send(embed=_emb, view=_view, ephemeral=True)


# ─── /cleanup — One-Click Cleanup ─────────────────────────────────────────────

@bot.tree.command(name="cleanup", description="Remove old logs, caches, and orphaned files from a VPS")
@app_commands.describe(vps_number="Which VPS to clean (1, 2, 3…). Defaults to your first VPS.")
async def cleanup_cmd(interaction: discord.Interaction, vps_number: int = 1):
    _uid = str(interaction.user.id)
    _user_vps = vps_data.get(_uid, [])
    if not _user_vps:
        await interaction.response.send_message(
            embed=create_error_embed("No VPS", "You don\'t have any VPS to clean."), ephemeral=True)
        return
    _idx = max(0, min(vps_number - 1, len(_user_vps) - 1))
    _container = _user_vps[_idx]["container_name"]
    await interaction.response.defer(ephemeral=True, thinking=True)
    _result = await run_vps_cleanup(_container)
    _freed = _result["freed_mb"]
    _emb = create_embed(
        f"🧹 Cleanup Complete — {_container}",
        f"Removed old logs, caches, temp files, and unused packages.\n\n**Space Freed:** `{_freed} MB`",
        0x57F287,
    )
    _emb.add_field(name="What was cleaned", value=(
        "• Journal logs (trimmed to 10 MB)\n"
        "• APT package cache & autoremove\n"
        "• Rotated log files (`.gz`, `.1`)\n"
        "• `/tmp` and `/var/tmp` contents"
    ), inline=False)
    await interaction.followup.send(embed=_emb, ephemeral=True)


# ─── /schedule-backup — Scheduled Backups ─────────────────────────────────────

_FREQ_LABELS = {"daily": "📅 Every Day", "weekly": "📆 Every Week", "monthly": "🗓️ Every Month"}

def _schedule_key(uid: str, container: str) -> str:
    return f"{uid}:{container}"


class _BackupFreqView(discord.ui.View):
    def __init__(self, uid: str, container: str, channel_id, user_id: int):
        super().__init__(timeout=120)
        self.uid = uid
        self.container = container
        self.channel_id = channel_id
        self.user_id = user_id
        for _freq, _label in _FREQ_LABELS.items():
            _btn = discord.ui.Button(label=_label, style=discord.ButtonStyle.primary)
            async def _cb(_inter: discord.Interaction, __freq=_freq):
                if _inter.user.id != self.user_id:
                    await _inter.response.send_message("Not your panel.", ephemeral=True)
                    return
                _schedules = load_backup_schedules()
                _key = _schedule_key(self.uid, self.container)
                _next = _next_backup_dt(__freq)
                _schedules[_key] = {
                    "user_id":        self.uid,
                    "container_name": self.container,
                    "frequency":      __freq,
                    "last_run":       None,
                    "next_run":       _next.isoformat(),
                    "channel_id":     self.channel_id,
                    "enabled":        True,
                }
                save_backup_schedules(_schedules)
                _emb = create_embed(
                    "💾 Backup Scheduled",
                    f"Auto-backups for `{self.container}` set to **{_FREQ_LABELS[__freq]}**.\n"
                    f"First backup: <t:{int(_next.timestamp())}:R>",
                    0x57F287,
                )
                await _inter.response.edit_message(embed=_emb, view=None)
            _btn.callback = _cb
            self.add_item(_btn)

        _cancel_btn = discord.ui.Button(label="❌ Cancel Schedule", style=discord.ButtonStyle.danger)
        async def _cancel(_inter: discord.Interaction):
            if _inter.user.id != self.user_id:
                await _inter.response.send_message("Not your panel.", ephemeral=True)
                return
            _schedules = load_backup_schedules()
            _key = _schedule_key(self.uid, self.container)
            if _key in _schedules:
                del _schedules[_key]
                save_backup_schedules(_schedules)
                _msg = f"Scheduled backups for `{self.container}` have been cancelled."
            else:
                _msg = f"No active backup schedule found for `{self.container}`."
            await _inter.response.edit_message(
                embed=create_embed("🗑️ Schedule Removed", _msg, 0xED4245), view=None)
        _cancel_btn.callback = _cancel
        self.add_item(_cancel_btn)


@bot.tree.command(name="schedule-backup", description="Schedule automatic backups for your VPS")
@app_commands.describe(vps_number="Which VPS to schedule backups for (1, 2, 3…)")
async def schedule_backup_cmd(interaction: discord.Interaction, vps_number: int = 1):
    _uid = str(interaction.user.id)
    _user_vps = vps_data.get(_uid, [])
    if not _user_vps:
        await interaction.response.send_message(
            embed=create_error_embed("No VPS", "You don\'t have any VPS to back up."), ephemeral=True)
        return
    _idx = max(0, min(vps_number - 1, len(_user_vps) - 1))
    _container = _user_vps[_idx]["container_name"]
    _schedules = load_backup_schedules()
    _current = _schedules.get(_schedule_key(_uid, _container))
    _desc = f"Choose how often to automatically back up `{_container}`.\n\n"
    if _current:
        _freq_label = _FREQ_LABELS.get(_current["frequency"], _current["frequency"])
        _next_ts = int(datetime.fromisoformat(_current["next_run"]).timestamp())
        _desc += f"**Current:** {_freq_label} — next backup <t:{_next_ts}:R>\n\n"
    else:
        _desc += "No active schedule yet.\n\n"
    _desc += "Select a frequency below:"
    _emb = create_embed("💾 Schedule Backups", _desc, get_embed_color_for("general"))
    _view = _BackupFreqView(_uid, _container, interaction.channel_id, interaction.user.id)
    await interaction.response.send_message(embed=_emb, view=_view, ephemeral=True)


# ─── /share-vps — Shareable Access ────────────────────────────────────────────

import secrets as _secrets_mod

_SHARE_PERM_LABELS = {
    "view":    "👁️ View Only — see status and SSH details, no actions",
    "restart": "🔄 Restart — view + start/stop",
    "full":    "🛠️ Full Management — all controls except reinstall",
}


class _ShareDurationModal(discord.ui.Modal, title="Grant VPS Access"):
    grantee_id = discord.ui.TextInput(
        label="Discord User ID to grant access to",
        placeholder="123456789012345678",
        max_length=20,
    )
    duration_hours = discord.ui.TextInput(
        label="Duration in hours (0 = permanent)",
        placeholder="24",
        max_length=6,
        default="24",
    )

    def __init__(self, uid: str, container: str, perm: str, user_id: int):
        super().__init__()
        self.uid = uid
        self.container = container
        self.perm = perm
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            _hours = int(self.duration_hours.value.strip())
        except ValueError:
            await interaction.response.send_message("Invalid duration — enter a number.", ephemeral=True)
            return
        _grantee = self.grantee_id.value.strip()
        if not _grantee.isdigit():
            await interaction.response.send_message("Invalid Discord User ID.", ephemeral=True)
            return
        _token = _secrets_mod.token_hex(12)
        _expires_dt = (_utcnow() + timedelta(hours=_hours)) if _hours > 0 else None
        _tokens = load_shared_access()
        _tokens[_token] = {
            "owner_id":       self.uid,
            "grantee_id":     _grantee,
            "container_name": self.container,
            "permission":     self.perm,
            "expires_at":     _expires_dt.isoformat() if _expires_dt else None,
            "created_at":     _utcnow().isoformat(),
        }
        save_shared_access(_tokens)
        _exp_txt = (f"<t:{int(_expires_dt.timestamp())}:R>" if _expires_dt else "**Permanent**")
        _perm_txt = _SHARE_PERM_LABELS.get(self.perm, self.perm).split(" — ")[0]
        _emb = create_embed(
            "🔗 Access Token Created",
            (f"**Container:** `{self.container}`\n"
             f"**For user:** <@{_grantee}>\n"
             f"**Permission:** {_perm_txt}\n"
             f"**Expires:** {_exp_txt}\n\n"
             f"Send this token to <@{_grantee}> — they can use `/accept-access` to activate it.\n"
             f"```\n{_token}\n```"),
            0x57F287,
        )
        await interaction.response.edit_message(embed=_emb, view=None)


class _SharePermView(discord.ui.View):
    def __init__(self, uid: str, container: str, user_id: int):
        super().__init__(timeout=120)
        self.uid = uid
        self.container = container
        self.user_id = user_id
        for _perm, _label in _SHARE_PERM_LABELS.items():
            _short = _label.split(" — ")[0]
            _btn = discord.ui.Button(label=_short, style=discord.ButtonStyle.primary)
            async def _cb(_inter: discord.Interaction, __perm=_perm):
                if _inter.user.id != self.user_id:
                    await _inter.response.send_message("Not your panel.", ephemeral=True)
                    return
                await _inter.response.send_modal(
                    _ShareDurationModal(self.uid, self.container, __perm, self.user_id))
            _btn.callback = _cb
            self.add_item(_btn)

        _revoke_btn = discord.ui.Button(label="🗑️ Revoke All Access", style=discord.ButtonStyle.danger)
        async def _revoke(_inter: discord.Interaction):
            if _inter.user.id != self.user_id:
                await _inter.response.send_message("Not your panel.", ephemeral=True)
                return
            _tokens = load_shared_access()
            _before = len(_tokens)
            _tokens = {_k: _v for _k, _v in _tokens.items()
                       if not (_v["owner_id"] == self.uid and _v["container_name"] == self.container)}
            save_shared_access(_tokens)
            _removed = _before - len(_tokens)
            await _inter.response.edit_message(
                embed=create_embed("🗑️ Access Revoked",
                                   f"Revoked **{_removed}** access token(s) for `{self.container}`.",
                                   0xED4245),
                view=None,
            )
        _revoke_btn.callback = _revoke
        self.add_item(_revoke_btn)


@bot.tree.command(name="share-vps", description="Grant another Discord user temporary access to your VPS")
@app_commands.describe(vps_number="Which VPS to share (1, 2, 3…)")
async def share_vps_cmd(interaction: discord.Interaction, vps_number: int = 1):
    _uid = str(interaction.user.id)
    _user_vps = vps_data.get(_uid, [])
    if not _user_vps:
        await interaction.response.send_message(
            embed=create_error_embed("No VPS", "You don\'t have any VPS to share."), ephemeral=True)
        return
    _idx = max(0, min(vps_number - 1, len(_user_vps) - 1))
    _container = _user_vps[_idx]["container_name"]
    _tokens = _prune_expired_tokens(load_shared_access())
    _active = [_v for _v in _tokens.values()
               if _v["owner_id"] == _uid and _v["container_name"] == _container]
    _desc = f"Choose a permission level to grant for `{_container}`.\n\n"
    if _active:
        _lines = []
        for _v in _active:
            _exp = (f"<t:{int(datetime.fromisoformat(_v[\'expires_at\']).timestamp())}:R>"
                    if _v.get("expires_at") else "permanent")
            _pshort = _SHARE_PERM_LABELS.get(_v["permission"], _v["permission"]).split(" — ")[0]
            _lines.append(f"• <@{_v[\'grantee_id\']}> — {_pshort} — expires {_exp}")
        _desc += f"**Active tokens ({len(_active)}):**\n" + "\\n".join(_lines) + "\\n\\n"
    _desc += "\\n".join(f"• {_v}" for _v in _SHARE_PERM_LABELS.values())
    _emb = create_embed("🔗 Share VPS Access", _desc, get_embed_color_for("general"))
    _view = _SharePermView(_uid, _container, interaction.user.id)
    await interaction.response.send_message(embed=_emb, view=_view, ephemeral=True)


@bot.tree.command(name="accept-access", description="Activate a VPS access token sent to you by the owner")
@app_commands.describe(token="The access token you received from the VPS owner")
async def accept_access_cmd(interaction: discord.Interaction, token: str):
    _tokens = _prune_expired_tokens(load_shared_access())
    _entry = _tokens.get(token.strip())
    if not _entry:
        await interaction.response.send_message(
            embed=create_error_embed("Invalid Token", "This token is invalid or has expired."),
            ephemeral=True)
        return
    _gid = str(interaction.user.id)
    if _entry["grantee_id"] != _gid:
        await interaction.response.send_message(
            embed=create_error_embed("Wrong Account", "This token was issued for a different Discord account."),
            ephemeral=True)
        return
    _owner_vps_list = vps_data.get(_entry["owner_id"], [])
    _target = next((_v for _v in _owner_vps_list if _v["container_name"] == _entry["container_name"]), None)
    if not _target:
        await interaction.response.send_message(
            embed=create_error_embed("VPS Not Found", "The VPS this token refers to no longer exists."),
            ephemeral=True)
        return
    _shared = _target.setdefault("shared_with", {})
    _shared[_gid] = _entry["permission"]
    save_data()
    _perm_txt = _SHARE_PERM_LABELS.get(_entry["permission"], _entry["permission"]).split(" — ")[0]
    _exp_txt = (f"<t:{int(datetime.fromisoformat(_entry[\'expires_at\']).timestamp())}:R>"
                if _entry.get("expires_at") else "permanent")
    _emb = create_embed(
        "✅ Access Activated",
        (f"You now have **{_perm_txt}** access to `{_entry[\'container_name\']}`.\n"
         f"Access expires: {_exp_txt}\n\nUse `/manage` to open your VPS panel."),
        0x57F287,
    )
    await interaction.response.send_message(embed=_emb, ephemeral=True)


# ─── /analytics — Installation Analytics (Admin) ──────────────────────────────

@bot.tree.command(name="analytics", description="View installation and platform analytics (Admin only)")
async def analytics_cmd(interaction: discord.Interaction):
    _uid = str(interaction.user.id)
    if _uid != str(MAIN_ADMIN_ID) and _uid not in admin_data.get("admins", []):
        await interaction.response.send_message(
            embed=create_error_embed("No Permission", "This command is admin-only."), ephemeral=True)
        return
    _d = load_analytics()
    _total   = _d["total_installs"]
    _success = _d["successful_installs"]
    _failed  = _d["failed_installs"]
    _times   = _d["install_times"]
    _tmpls   = _d["template_counts"]
    _avg_t   = (sum(_times) / len(_times)) if _times else 0
    _rate    = (_success / _total * 100) if _total else 0
    _top5    = sorted(_tmpls.items(), key=lambda _x: _x[1], reverse=True)[:5]
    _tmpl_str = "\\n".join(f"`{_t}` — **{_n}** install(s)" for _t, _n in _top5) or "No installs yet."
    _emb = create_embed(
        f"📈 {get_brand_name()} Analytics",
        "Live platform installation statistics.",
        get_embed_color_for("general"),
    )
    _emb.add_field(name="📦 Total Installs",  value=f"**{_total}**",            inline=True)
    _emb.add_field(name="✅ Successful",       value=f"**{_success}**",          inline=True)
    _emb.add_field(name="❌ Failed",           value=f"**{_failed}**",           inline=True)
    _emb.add_field(name="📊 Success Rate",    value=f"**{_rate:.1f}%**",        inline=True)
    _emb.add_field(name="⏱️ Avg Install Time", value=f"**{_avg_t:.0f}s**",       inline=True)
    _emb.add_field(name="🏆 Top Templates",    value=_tmpl_str,                  inline=False)
    _history = _d.get("installs_history", [])[-5:]
    if _history:
        _hlines = []
        for _h in reversed(_history):
            _icon = "✅" if _h["success"] else "❌"
            _ts = int(datetime.fromisoformat(_h["timestamp"]).timestamp())
            _hlines.append(f"{_icon} `{_h[\'template\']}` by <@{_h[\'user_id\']}> — <t:{_ts}:R>")
        _emb.add_field(name="🕐 Recent Installs", value="\\n".join(_hlines), inline=False)
    await interaction.response.send_message(embed=_emb, ephemeral=True)


# ─── /guided-setup — Pterodactyl Next Steps ───────────────────────────────────

_GUIDED_STEPS = [
    ("☁️ Install Cloudflare Tunnel", "cloudflare_tunnel"),
    ("🌐 Configure the Tunnel",      "configure_tunnel"),
    ("🦅 Install Wings",             "install_wings"),
    ("🔗 Link Wings to Panel",       "link_wings"),
]

_GUIDED_TIPS = {
    "cloudflare_tunnel": (
        "Run inside your VPS:\n"
        "```bash\ncurl -L https://pkg.cloudflare.com/cloudflare-main.gpg "
        "| sudo gpg --dearmor -o /usr/share/keyrings/cloudflare-main.gpg\n"
        "echo \'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] "
        "https://pkg.cloudflare.com/cloudflared any main\' "
        "| sudo tee /etc/apt/sources.list.d/cloudflared.list\n"
        "sudo apt update && sudo apt install cloudflared -y\n"
        "cloudflared tunnel login\n```"
    ),
    "configure_tunnel": (
        "After logging in:\n"
        "```bash\ncloudflared tunnel create ptero\n"
        "cloudflared tunnel route dns ptero panel.yourdomain.com\n"
        "cloudflared tunnel run ptero\n```"
    ),
    "install_wings": (
        "On your Wings node VPS:\n"
        "```bash\ncurl -sSL https://get.docker.com/ | CHANNEL=stable sh\n"
        "sudo mkdir -p /etc/pterodactyl\n"
        "curl -L -o /usr/local/bin/wings \\\\\n"
        "  \'https://github.com/pterodactyl/wings/releases/latest/download/wings_linux_amd64\'\n"
        "sudo chmod u+x /usr/local/bin/wings\n```"
    ),
    "link_wings": (
        "In your Pterodactyl panel:\n"
        "1. **Admin → Nodes → Create Node**\n"
        "2. Copy the auto-deploy command shown\n"
        "3. Paste and run it on your Wings VPS\n"
        "4. Start Wings: `sudo systemctl enable --now wings`"
    ),
}


class _GuidedSetupView(discord.ui.View):
    def __init__(self, container: str, user_id: int, current_step: int = 0):
        super().__init__(timeout=300)
        self.container    = container
        self.user_id      = user_id
        self.current_step = current_step
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        for _i, (_title, _key) in enumerate(_GUIDED_STEPS):
            _done     = _i < self.current_step
            _active   = _i == self.current_step
            _label    = f"{'✅ ' if _done else ''}{_title}"
            _style    = (discord.ButtonStyle.success if _active
                         else discord.ButtonStyle.secondary)
            _disabled = _i > self.current_step
            _btn = discord.ui.Button(
                label=_label, style=_style, disabled=_disabled, row=_i)
            async def _cb(_inter: discord.Interaction, __i=_i, __key=_key):
                if _inter.user.id != self.user_id:
                    await _inter.response.send_message("Not your panel.", ephemeral=True)
                    return
                _tip = _GUIDED_TIPS.get(__key, "Follow the in-panel instructions.")
                _emb = create_embed(
                    f"🎯 Step {__i + 1}: {_GUIDED_STEPS[__i][0]}",
                    _tip,
                    get_embed_color_for("general"),
                )
                _new_step = max(self.current_step, __i + 1)
                _nv = _GuidedSetupView(self.container, self.user_id, _new_step)
                await _inter.response.edit_message(embed=_emb, view=_nv)
            _btn.callback = _cb
            self.add_item(_btn)


@bot.tree.command(name="guided-setup",
                  description="Step-by-step guide for next steps after installing Pterodactyl")
@app_commands.describe(vps_number="Which VPS Pterodactyl is installed on (1, 2, 3…)")
async def guided_setup_cmd(interaction: discord.Interaction, vps_number: int = 1):
    _uid = str(interaction.user.id)
    _user_vps = vps_data.get(_uid, [])
    if not _user_vps:
        await interaction.response.send_message(
            embed=create_error_embed("No VPS", "You don\'t have any VPS."), ephemeral=True)
        return
    _idx = max(0, min(vps_number - 1, len(_user_vps) - 1))
    _container = _user_vps[_idx]["container_name"]
    _emb = create_embed(
        "🎯 Pterodactyl Guided Setup",
        (f"Pterodactyl is installed on `{_container}`. "
         f"Click each step in order to see instructions and mark it complete."),
        get_embed_color_for("general"),
    )
    _view = _GuidedSetupView(_container, interaction.user.id)
    await interaction.response.send_message(embed=_emb, view=_view, ephemeral=True)


# ─── Background Loops: Scheduled Backups & Smart Notifications ─────────────────

@tasks.loop(minutes=30)
async def scheduled_backup_loop():
    """Run any due scheduled backups and notify users."""
    _schedules = load_backup_schedules()
    _now = _utcnow()
    _changed = False
    for _key, _s in list(_schedules.items()):
        if not _s.get("enabled", True):
            continue
        try:
            _next_run = datetime.fromisoformat(_s["next_run"])
        except Exception:
            continue
        if _now < _next_run:
            continue
        _container  = _s["container_name"]
        _uid        = _s["user_id"]
        _channel_id = _s.get("channel_id")
        try:
            _ts   = datetime.now().strftime("%Y%m%d-%H%M%S")
            _snap = f"{_container}-sched-{_ts}"
            await execute_docker(f"docker commit {_container} {_snap}")
            _s["last_run"] = _now.isoformat()
            _s["next_run"] = _next_backup_dt(_s["frequency"], _now).isoformat()
            _changed = True
            try:
                _user = await bot.fetch_user(int(_uid))
                await _user.send(embed=create_embed(
                    "💾 Scheduled Backup Complete",
                    f"Auto-backup of `{_container}` succeeded.\n**Snapshot:** `{_snap}`",
                    0x57F287,
                ))
            except Exception:
                pass
            if _channel_id:
                try:
                    _ch = bot.get_channel(int(_channel_id))
                    if _ch:
                        await _ch.send(embed=create_embed(
                            "💾 Backup Complete",
                            f"<@{_uid}>\'s VPS `{_container}` → `{_snap}`",
                            0x57F287,
                        ))
                except Exception:
                    pass
        except Exception as _e:
            _s["last_run"] = _now.isoformat()
            _s["next_run"] = _next_backup_dt(_s["frequency"], _now).isoformat()
            _changed = True
            try:
                _user = await bot.fetch_user(int(_uid))
                await _user.send(embed=create_error_embed(
                    "💾 Scheduled Backup Failed",
                    f"Auto-backup of `{_container}` failed: {_e}",
                ))
            except Exception:
                pass
    if _changed:
        save_backup_schedules(_schedules)


@tasks.loop(minutes=5)
async def smart_notification_loop():
    """Proactively DM users about high disk, RAM, or crashed services."""
    _DISK_WARN = 90
    _RAM_WARN  = 90
    for _uid, _vps_list in list(vps_data.items()):
        for _vps in _vps_list:
            if _vps.get("status") != "running":
                continue
            _con = _vps["container_name"]
            # Disk
            try:
                _dpct = int(str(await docker_exec(
                    _con,
                    "df / --output=pcent 2>/dev/null | tail -1 | tr -d \\' \\'",
                    timeout=8,
                )).strip())
                if _dpct >= _DISK_WARN:
                    try:
                        _u = await bot.fetch_user(int(_uid))
                        await _u.send(embed=create_embed(
                            "⚠️ High Disk Usage",
                            f"Your VPS `{_con}` disk is at **{_dpct}%**. "
                            f"Use `/cleanup` to free space.",
                            0xFF6600,
                        ))
                    except Exception:
                        pass
            except Exception:
                pass
            # RAM
            try:
                _mout = str(await docker_exec(
                    _con,
                    "awk \\'/MemAvailable/{a=$2}/MemTotal/{t=$2}END{printf \"%d %d\",a,t}\\' /proc/meminfo",
                    timeout=8,
                )).split()
                if len(_mout) == 2:
                    _free_k, _tot_k = int(_mout[0]), int(_mout[1]) or 1
                    _rpct = 100 - (_free_k / _tot_k * 100)
                    if _rpct >= _RAM_WARN:
                        try:
                            _u = await bot.fetch_user(int(_uid))
                            await _u.send(embed=create_embed(
                                "⚠️ High RAM Usage",
                                f"Your VPS `{_con}` RAM is at **{_rpct:.0f}%**. "
                                f"Consider stopping unused services.",
                                0xFF6600,
                            ))
                        except Exception:
                            pass
            except Exception:
                pass
            # Failed services
            try:
                _nf = int(str(await docker_exec(
                    _con,
                    "systemctl list-units --state=failed --no-legend 2>/dev/null | wc -l",
                    timeout=8,
                )).strip())
                if _nf > 0:
                    try:
                        _u = await bot.fetch_user(int(_uid))
                        await _u.send(embed=create_embed(
                            "🔴 Service Crash Detected",
                            f"**{_nf}** service(s) on `{_con}` have crashed. "
                            f"Use `/fix` to restart them.",
                            0xED4245,
                        ))
                    except Exception:
                        pass
            except Exception:
                pass

'''

# ─────────────────────────────────────────────────────────────────────────────
# CLEANUP BUTTON (added to add_action_buttons in ManageView)
# ─────────────────────────────────────────────────────────────────────────────
CLEANUP_BUTTON_CODE = '''        cleanup_button = discord.ui.Button(label="🧹 Cleanup", style=discord.ButtonStyle.secondary, row=1)
        async def _cleanup_vps_cb(_inter: discord.Interaction, _view=self):
            if _inter.user.id != _view.user_id:
                await _inter.response.send_message("This panel is not for you.", ephemeral=True)
                return
            _idx2 = _view.selected_index if _view.selected_index is not None else 0
            _con2 = _view.vps_list[_idx2]["container_name"]
            await _inter.response.defer(ephemeral=True, thinking=True)
            _res = await run_vps_cleanup(_con2)
            _emb2 = create_embed(
                "🧹 Cleanup Complete",
                f"Freed **{_res[\'freed_mb\']} MB** of disk space from `{_con2}`.\\n\\n"
                "Cleaned: journal logs, APT cache, temp files, rotated logs.",
                0x57F287,
            )
            await _inter.followup.send(embed=_emb2, ephemeral=True)
        cleanup_button.callback = _cleanup_vps_cb
        self.add_item(cleanup_button)
'''

# LOOP STARTS for on_ready
LOOP_STARTS = '''    if not scheduled_backup_loop.is_running():
        scheduled_backup_loop.start()
    if not smart_notification_loop.is_running():
        smart_notification_loop.start()
'''

# ─────────────────────────────────────────────────────────────────────────────
# PERFORM ALL INSERTIONS — bottom to top
# ─────────────────────────────────────────────────────────────────────────────

# 1. Insert background loops before the second auto_expire_check
print(f"\n[1] Inserting background loops before line {LINE_second_auto_expire}")
# Insert BLOCK_B (all commands + loops) before the first __main__
# and loops before auto_expire_check
# We do: loops go before auto_expire_check; commands go before first __main__
# Both are in BLOCK_B already (loops at end of BLOCK_B)
# Actually we split: commands before first __main__, and the loops before second auto_expire

# Split BLOCK_B into commands part and loops part
_loops_marker = "# ─── Background Loops:"
_loops_start = BLOCK_B.index(_loops_marker)
BLOCK_B_CMDS  = BLOCK_B[:_loops_start].rstrip() + "\n"
BLOCK_B_LOOPS = "\n" + BLOCK_B[_loops_start:].lstrip()

# Insert loops before second auto_expire_check (bottom-most first)
lines.insert(LINE_second_auto_expire - 1, BLOCK_B_LOOPS)
# Update remaining line numbers (shift up by the number of lines inserted)
_shift_loops = BLOCK_B_LOOPS.count("\n")
print(f"   Inserted {_shift_loops} lines of loop code before auto_expire (line {LINE_second_auto_expire})")

# 2. Insert loop starts in second on_ready (shifted by _shift_loops since it's above insertion point? no — insertion was below)
# Wait: LINE_second_auto_expire > LINE_second_on_ready_abuse_end — inserting below doesn't affect lines above
# So second on_ready line is still the same
print(f"\n[2] Adding loop starts to second on_ready after line {LINE_second_on_ready_abuse_end}")
lines.insert(LINE_second_on_ready_abuse_end, LOOP_STARTS)
_shift_onready2 = LOOP_STARTS.count("\n")
print(f"   Inserted {_shift_onready2} lines")

# 3. Add cleanup button to second add_action_buttons (shifted by inserts above? Let's check)
# LINE_second_add_action_ssh is above LINE_second_on_ready_abuse_end? No — second on_ready is at 7354, ssh at 7504
# So 7504 > 7354 — insertions at 7362 DO shift line 7504 up
_adjusted_second_ssh = LINE_second_add_action_ssh + _shift_onready2
print(f"\n[3] Adding cleanup button to second add_action_buttons (original {LINE_second_add_action_ssh}, adjusted {_adjusted_second_ssh})")
lines.insert(_adjusted_second_ssh, CLEANUP_BUTTON_CODE)
_shift_btn2 = CLEANUP_BUTTON_CODE.count("\n")

# 4. Insert BLOCK_B_CMDS (all new slash commands) before first __main__
# LINE_slash_cmds_before_main1 is at 5490 — all the above inserts are above 5490 in line count? No — 7354 > 5490
# So: insertions 1 (at 10646), 2 (at 7354+), 3 (at 7504+) are ALL above line 5490 in number BUT below it? 
# Wait — line 5490 is LESS than 7354, so insertions at lines > 5490 shift lines > 5490 only.
# Line 5490 is not shifted by inserts at lines 7354, 7504, 10646.
print(f"\n[4] Inserting slash commands before first __main__ (line {LINE_slash_cmds_before_main1})")
lines.insert(LINE_slash_cmds_before_main1 - 1, BLOCK_B_CMDS)
_shift_cmds = BLOCK_B_CMDS.count("\n")
print(f"   Inserted {_shift_cmds} lines")

# 5. Add loop starts to first on_ready
# LINE_first_on_ready_abuse_end is at ~line 2152, well below 5490 — not affected by insert 4? 
# Insert 4 was at line 5490, which is ABOVE (numerically greater than) 2152 — so no shift to 2152
print(f"\n[5] Adding loop starts to first on_ready (line {LINE_first_on_ready_abuse_end})")
lines.insert(LINE_first_on_ready_abuse_end, LOOP_STARTS)
_shift_onready1 = LOOP_STARTS.count("\n")

# 6. Add cleanup button to first add_action_buttons
# LINE_first_add_action_ssh is at ~2288, ABOVE the on_ready insert at ~2152? 
# 2288 > 2152 so insert at 2152 shifts 2288
_adjusted_first_ssh = LINE_first_add_action_ssh + _shift_onready1
print(f"\n[6] Adding cleanup button to first add_action_buttons (original {LINE_first_add_action_ssh}, adjusted {_adjusted_first_ssh})")
lines.insert(_adjusted_first_ssh, CLEANUP_BUTTON_CODE)

# 7. Insert BLOCK_A (data helpers + scanner) after set_all_embed_colors
# LINE_set_all_embed_colors_end is at ~243, well below all previous inserts — not affected
print(f"\n[7] Inserting data helpers + scanner after line {LINE_set_all_embed_colors_end}")
lines.insert(LINE_set_all_embed_colors_end, BLOCK_A)
_shift_a = BLOCK_A.count("\n")
print(f"   Inserted {_shift_a} lines")

# ─────────────────────────────────────────────────────────────────────────────
# WRITE & VALIDATE
# ─────────────────────────────────────────────────────────────────────────────
new_src = "".join(lines)
print(f"\nTotal lines after patching: {new_src.count(chr(10))}")

import ast
try:
    ast.parse(new_src)
    print("✅ Syntax OK")
except SyntaxError as e:
    print(f"❌ SyntaxError at line {e.lineno}: {e.msg}")
    bad_lines = new_src.splitlines()
    for i in range(max(0, e.lineno-6), min(len(bad_lines), e.lineno+4)):
        print(f"{i+1}: {bad_lines[i]}")
    sys.exit(1)

with open("bot.py", "w") as f:
    f.write(new_src)
print("✅ bot.py written successfully")
