"""
template.py
━━━━━━━━━━━
Unified Template System for the DarkNodes VPS Bot.
Provides the /template slash command with two built-in templates:
  • 🦖 Pterodactyl  (Panel + Wings)
  • ☁️ Cloudflare Tunnel

Usage (in bot.py, before bot.run()):
    import template
    template.register(bot, lambda: vps_data, docker_exec)
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import string
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

import discord

logger = logging.getLogger("vps_bot.templates")

# ─── Module state (set by register()) ────────────────────────────────────────

_bot: Optional[discord.ext.commands.Bot] = None  # type: ignore[name-defined]
_get_vps_data: Optional[Callable] = None
_docker_exec:  Optional[Callable] = None


def register(bot_instance, get_vps_data_fn: Callable, docker_exec_fn: Callable) -> None:
    """
    Wire the template system into the running bot.  Call once from bot.py
    before bot.run().

        import template
        template.register(bot, lambda: vps_data, docker_exec)
    """
    global _bot, _get_vps_data, _docker_exec
    _bot           = bot_instance
    _get_vps_data  = get_vps_data_fn
    _docker_exec   = docker_exec_fn
    _bot.tree.add_command(_cmd_template)
    logger.info("Template system registered — /template is available.")


# ═══════════════════════════════════════════════════════════════════════════════
# SHARED HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

_SAFE_CHARS = string.ascii_letters + string.digits + "@#%^&*-_=+[]{}"


def _gen_password(length: int = 24) -> str:
    return "".join(secrets.choice(_SAFE_CHARS) for _ in range(length))

def _gen_username() -> str:
    return f"admin_{secrets.token_hex(4)}"

def _gen_email() -> str:
    return f"admin.{secrets.token_hex(5)}@darknodes.local"

def _gen_db_name() -> str:
    return f"ptero_{secrets.token_hex(4)}"

def _gen_db_user() -> str:
    return f"ptero_{secrets.token_hex(4)}"


def _progress_bar(done: int, total: int, width: int = 10) -> str:
    filled = int(width * done / max(total, 1))
    pct    = int(100 * done / max(total, 1))
    return f"{'█' * filled}{'░' * (width - filled)} {pct}%"


def _build_progress_embed(
    title:     str,
    steps:     List[Tuple[str, str]],
    states:    Dict[str, str],
    container: str,
    note:      str  = "",
    color:     int  = 0x5865F2,
    failed:    bool = False,
) -> discord.Embed:
    """states values: 'pending' | 'running' | 'done' | 'error'"""
    ICONS = {"done": "✅", "running": "⏳", "error": "❌", "pending": "⏸️"}
    done  = sum(1 for v in states.values() if v == "done")
    total = len(steps)

    embed = discord.Embed(
        title=title,
        description=(
            f"━━━━━━━━━━━━━━━━━━\n"
            f"`{_progress_bar(done, total)}`\n"
            f"▸ Container: `{container}`"
        ),
        color=0xff3366 if failed else color,
        timestamp=datetime.utcnow(),
    )
    lines = [f"{ICONS.get(states.get(k, 'pending'), '⏸️')} {lbl}" for k, lbl in steps]
    embed.add_field(name="📋 Installation Progress", value="\n".join(lines), inline=False)
    if note:
        embed.add_field(name="ℹ️ Status", value=note, inline=False)
    embed.set_footer(text="DarkNodes VPS Platform  •  Template System")
    return embed


async def _exec(container: str, script: str, timeout: int = 120) -> Tuple[bool, str]:
    """Run a bash script inside the container. Returns (ok, combined_output)."""
    try:
        stdout, stderr, rc = await _docker_exec(container, script, timeout=timeout)
        combined = (stdout or "") + ("\n" + (stderr or "") if stderr else "")
        return rc == 0, combined.strip()
    except asyncio.TimeoutError:
        return False, f"[TIMEOUT after {timeout}s]"
    except Exception as exc:
        return False, str(exc)


# ═══════════════════════════════════════════════════════════════════════════════
# PTERODACTYL — PANEL
# ═══════════════════════════════════════════════════════════════════════════════

PANEL_STEPS: List[Tuple[str, str]] = [
    ("repo",       "Preparing Package Repositories"),
    ("php",        "Installing PHP 8.3 & Extensions"),
    ("composer",   "Installing Composer"),
    ("db",         "Installing MariaDB"),
    ("redis",      "Installing Redis"),
    ("nginx",      "Installing Nginx"),
    ("files",      "Downloading Pterodactyl Panel"),
    ("vendor",     "Installing PHP Dependencies"),
    ("database",   "Configuring Database"),
    ("env",        "Configuring Environment"),
    ("migrations", "Running Database Migrations"),
    ("admin",      "Creating Admin Account"),
    ("perms",      "Setting File Permissions"),
    ("webserver",  "Configuring Nginx"),
    ("ssl",        "Setting Up SSL"),
    ("services",   "Starting Services"),
    ("verify",     "Verifying Installation"),
]


async def install_panel(
    interaction: discord.Interaction,
    container:   str,
    domain:      str,
    ssl_mode:    str,   # "letsencrypt" | "http" | "custom"
    cert_path:   str = "",
    key_path:    str = "",
) -> None:
    db_name  = _gen_db_name()
    db_user  = _gen_db_user()
    db_pass  = _gen_password(20)
    adm_user = _gen_username()
    adm_email = _gen_email()
    adm_pass = _gen_password(24)

    steps  = PANEL_STEPS
    states: Dict[str, str] = {k: "pending" for k, _ in steps}

    async def _update(current_key: str, note: str = "", failed: bool = False) -> None:
        found = False
        for k, _ in steps:
            if k == current_key:
                states[k] = "error" if failed else "running"
                found = True
            elif not found:
                if states[k] not in ("done", "error"):
                    states[k] = "done"
        embed = _build_progress_embed(
            "🚀 Installing Pterodactyl Panel",
            steps, states, container, note, failed=failed,
        )
        try:
            await interaction.edit_original_response(embed=embed)
        except Exception:
            pass

    async def _fail(key: str, reason: str, log: str) -> None:
        states[key] = "error"
        short_log = log[-800:] if log else "(no output)"
        embed = _build_progress_embed(
            "❌ Installation Failed",
            steps, states, container,
            f"**Step failed:** `{key}`\n**Reason:** {reason[:200]}\n```{short_log[-400:]}```",
            failed=True,
        )
        embed.add_field(
            name="🔄 What to do",
            value="Fix the issue then click **Retry** below to restart the installation.",
            inline=False,
        )
        view = _PanelRetryView(interaction, container, domain, ssl_mode, cert_path, key_path)
        try:
            await interaction.edit_original_response(embed=embed, view=view)
        except Exception:
            pass

    # ── STEP 1 — repo ─────────────────────────────────────────────────────
    await _update("repo", "Adding PHP & package repositories…")
    ok, out = await _exec(container, r"""
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq 2>&1 | tail -3
apt-get install -y -qq --no-install-recommends \
    software-properties-common apt-transport-https ca-certificates gnupg \
    curl wget git unzip zip tar lsb-release 2>&1 | tail -5
LC_ALL=C.UTF-8 add-apt-repository -y ppa:ondrej/php 2>&1 | tail -3
apt-get update -qq 2>&1 | tail -3
echo "REPO_OK"
""", timeout=180)
    if not ok or "REPO_OK" not in out:
        return await _fail("repo", "Failed to add package repositories.", out)
    states["repo"] = "done"

    # ── STEP 2 — php ──────────────────────────────────────────────────────
    await _update("php", "Installing PHP 8.3 + required extensions…")
    ok, out = await _exec(container, r"""
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq \
    php8.3 php8.3-cli php8.3-fpm php8.3-mysql php8.3-mbstring \
    php8.3-xml php8.3-bcmath php8.3-gd php8.3-curl php8.3-zip \
    php8.3-tokenizer php8.3-fileinfo php8.3-openssl php8.3-pdo \
    php8.3-redis php8.3-intl 2>&1 | tail -5
php8.3 --version | head -1
echo "PHP_OK"
""", timeout=300)
    if not ok or "PHP_OK" not in out:
        return await _fail("php", "PHP 8.3 installation failed.", out)
    states["php"] = "done"

    # ── STEP 3 — composer ─────────────────────────────────────────────────
    await _update("composer", "Installing Composer…")
    ok, out = await _exec(container, r"""
curl -sS https://getcomposer.org/installer \
    | php8.3 -- --install-dir=/usr/local/bin --filename=composer 2>&1
composer --version 2>&1 | head -1
echo "COMPOSER_OK"
""", timeout=90)
    if not ok or "COMPOSER_OK" not in out:
        return await _fail("composer", "Composer installation failed.", out)
    states["composer"] = "done"

    # ── STEP 4 — db ───────────────────────────────────────────────────────
    await _update("db", "Installing and starting MariaDB…")
    ok, out = await _exec(container, r"""
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq mariadb-server 2>&1 | tail -5
systemctl enable mariadb --quiet 2>&1 || true
systemctl start  mariadb        2>&1 || true
sleep 2
mysqladmin -u root ping 2>&1 | head -2
echo "MARIADB_OK"
""", timeout=120)
    if not ok or "MARIADB_OK" not in out:
        return await _fail("db", "MariaDB installation failed.", out)
    states["db"] = "done"

    # ── STEP 5 — redis ────────────────────────────────────────────────────
    await _update("redis", "Installing and starting Redis…")
    ok, out = await _exec(container, r"""
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq redis-server 2>&1 | tail -3
sed -i 's/^supervised no/supervised systemd/' /etc/redis/redis.conf 2>/dev/null || true
systemctl enable redis-server --quiet 2>&1 || true
systemctl start  redis-server        2>&1 || true
sleep 1
redis-cli ping 2>&1 | head -1
echo "REDIS_OK"
""", timeout=90)
    if not ok or "REDIS_OK" not in out:
        return await _fail("redis", "Redis installation failed.", out)
    states["redis"] = "done"

    # ── STEP 6 — nginx ────────────────────────────────────────────────────
    await _update("nginx", "Installing Nginx…")
    ok, out = await _exec(container, r"""
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq nginx 2>&1 | tail -3
systemctl enable nginx --quiet 2>&1 || true
nginx -v 2>&1 | head -1
echo "NGINX_OK"
""", timeout=90)
    if not ok or "NGINX_OK" not in out:
        return await _fail("nginx", "Nginx installation failed.", out)
    states["nginx"] = "done"

    # ── STEP 7 — files ────────────────────────────────────────────────────
    await _update("files", "Downloading Pterodactyl Panel files…")
    ok, out = await _exec(container, r"""
mkdir -p /var/www/pterodactyl
cd /var/www/pterodactyl
curl -Lo panel.tar.gz \
  https://github.com/pterodactyl/panel/releases/latest/download/panel.tar.gz \
  2>&1 | tail -3
tar -xzf panel.tar.gz
rm -f panel.tar.gz
ls composer.json >/dev/null 2>&1
echo "FILES_OK"
""", timeout=120)
    if not ok or "FILES_OK" not in out:
        return await _fail("files", "Failed to download Pterodactyl panel files.", out)
    states["files"] = "done"

    # ── STEP 8 — vendor ───────────────────────────────────────────────────
    await _update("vendor", "Running composer install (this may take a few minutes)…")
    ok, out = await _exec(container, r"""
cd /var/www/pterodactyl
composer install \
    --no-dev \
    --optimize-autoloader \
    --no-interaction \
    --no-progress \
    2>&1 | tail -5
echo "VENDOR_OK"
""", timeout=420)
    if not ok or "VENDOR_OK" not in out:
        return await _fail("vendor", "composer install failed.", out)
    states["vendor"] = "done"

    # ── STEP 9 — database ─────────────────────────────────────────────────
    await _update("database", "Creating database and user…")
    ok, out = await _exec(container, f"""
mysql -u root 2>&1 << 'MYSQL_EOF'
CREATE DATABASE IF NOT EXISTS `{db_name}`;
CREATE USER IF NOT EXISTS '{db_user}'@'127.0.0.1' IDENTIFIED BY '{db_pass}';
GRANT ALL PRIVILEGES ON `{db_name}`.* TO '{db_user}'@'127.0.0.1';
FLUSH PRIVILEGES;
MYSQL_EOF
echo "DB_SETUP_OK"
""", timeout=30)
    if not ok or "DB_SETUP_OK" not in out:
        return await _fail("database", "Database/user creation failed.", out)
    states["database"] = "done"

    # ── STEP 10 — env ─────────────────────────────────────────────────────
    await _update("env", "Configuring .env and generating APP_KEY…")
    scheme = "http" if ssl_mode == "http" else "https"
    ok, out = await _exec(container, f"""
cd /var/www/pterodactyl
cp .env.example .env
php8.3 artisan key:generate --force 2>&1

sed -i "s|^APP_URL=.*|APP_URL={scheme}://{domain}|"          .env
sed -i "s|^APP_ENV=.*|APP_ENV=production|"                     .env
sed -i "s|^APP_DEBUG=.*|APP_DEBUG=false|"                      .env
sed -i "s|^DB_HOST=.*|DB_HOST=127.0.0.1|"                     .env
sed -i "s|^DB_PORT=.*|DB_PORT=3306|"                           .env
sed -i "s|^DB_DATABASE=.*|DB_DATABASE={db_name}|"             .env
sed -i "s|^DB_USERNAME=.*|DB_USERNAME={db_user}|"             .env
sed -i "s|^DB_PASSWORD=.*|DB_PASSWORD={db_pass}|"             .env
sed -i "s|^CACHE_DRIVER=.*|CACHE_DRIVER=redis|"               .env
sed -i "s|^SESSION_DRIVER=.*|SESSION_DRIVER=redis|"           .env
sed -i "s|^QUEUE_CONNECTION=.*|QUEUE_CONNECTION=redis|"       .env
sed -i "s|^REDIS_HOST=.*|REDIS_HOST=127.0.0.1|"               .env
sed -i "s|^REDIS_PORT=.*|REDIS_PORT=6379|"                     .env

grep "^APP_URL=" .env
echo "ENV_OK"
""", timeout=60)
    if not ok or "ENV_OK" not in out:
        return await _fail("env", "Environment configuration failed.", out)
    states["env"] = "done"

    # ── STEP 11 — migrations ──────────────────────────────────────────────
    await _update("migrations", "Running database migrations and seeding…")
    ok, out = await _exec(container, r"""
cd /var/www/pterodactyl
php8.3 artisan migrate --seed --force 2>&1 | tail -10
echo "MIGRATE_OK"
""", timeout=300)
    if not ok or "MIGRATE_OK" not in out:
        return await _fail("migrations", "Database migration failed.", out)
    states["migrations"] = "done"

    # ── STEP 12 — admin ───────────────────────────────────────────────────
    await _update("admin", "Creating administrator account…")
    ok, out = await _exec(container, f"""
cd /var/www/pterodactyl
php8.3 artisan p:user:make \\
    --email="{adm_email}" \\
    --username="{adm_user}" \\
    --name-first="Admin" \\
    --name-last="User" \\
    --password="{adm_pass}" \\
    --admin=1 \\
    --no-interaction 2>&1 | tail -5
echo "ADMIN_OK"
""", timeout=60)
    if not ok or "ADMIN_OK" not in out:
        return await _fail("admin", "Admin user creation failed.", out)
    states["admin"] = "done"

    # ── STEP 13 — perms ───────────────────────────────────────────────────
    await _update("perms", "Setting file permissions…")
    ok, out = await _exec(container, r"""
chown -R www-data:www-data /var/www/pterodactyl
chmod -R 755 /var/www/pterodactyl/storage/*
chmod -R 755 /var/www/pterodactyl/bootstrap/cache/
echo "PERMS_OK"
""", timeout=30)
    if not ok or "PERMS_OK" not in out:
        return await _fail("perms", "File permission setup failed.", out)
    states["perms"] = "done"

    # ── STEP 14 — webserver ───────────────────────────────────────────────
    await _update("webserver", "Writing Nginx virtual host config…")
    nginx_http_conf = f"""server {{
    listen 80;
    listen [::]:80;
    server_name {domain};
    root /var/www/pterodactyl/public;
    index index.php;
    charset utf-8;

    location / {{
        try_files $uri $uri/ /index.php?$query_string;
    }}

    location ~ \\.php$ {{
        fastcgi_pass   unix:/run/php/php8.3-fpm.sock;
        fastcgi_index  index.php;
        fastcgi_param  SCRIPT_FILENAME $realpath_root$fastcgi_script_name;
        include        fastcgi_params;
    }}

    location ~ /\\.(?!well-known).* {{
        deny all;
    }}

    client_max_body_size 100m;
    client_body_timeout  120;
    sendfile off;
    access_log /var/log/nginx/pterodactyl_access.log;
    error_log  /var/log/nginx/pterodactyl_error.log error;
}}
"""
    ok, out = await _exec(container, f"""
cat > /etc/nginx/sites-available/pterodactyl.conf << 'NGINX_EOF'
{nginx_http_conf}
NGINX_EOF
ln -sf /etc/nginx/sites-available/pterodactyl.conf /etc/nginx/sites-enabled/pterodactyl.conf
rm -f /etc/nginx/sites-enabled/default
nginx -t 2>&1 | head -5
systemctl start php8.3-fpm 2>&1 || true
systemctl enable php8.3-fpm --quiet 2>&1 || true
systemctl reload nginx 2>&1 || true
echo "NGINX_CONF_OK"
""", timeout=30)
    if not ok or "NGINX_CONF_OK" not in out:
        return await _fail("webserver", "Nginx configuration failed.", out)
    states["webserver"] = "done"

    # ── STEP 15 — ssl ─────────────────────────────────────────────────────
    await _update("ssl", f"Setting up SSL ({ssl_mode})…")
    if ssl_mode == "letsencrypt":
        ok, out = await _exec(container, f"""
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq certbot python3-certbot-nginx 2>&1 | tail -3
certbot --nginx \\
    --non-interactive \\
    --agree-tos \\
    --register-unsafely-without-email \\
    -d {domain} 2>&1 | tail -10
echo "SSL_OK"
""", timeout=180)
        if not ok or "SSL_OK" not in out:
            logger.warning(f"Let's Encrypt failed for {container}/{domain}: {out[-300:]}")
        states["ssl"] = "done"

    elif ssl_mode == "custom":
        ok, out = await _exec(container, f"""
ls "{cert_path}" >/dev/null 2>&1 || exit 1
ls "{key_path}"  >/dev/null 2>&1 || exit 1
cat > /etc/nginx/sites-available/pterodactyl.conf << 'NGINX_SSL_EOF'
server {{
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name {domain};
    root /var/www/pterodactyl/public;
    index index.php;
    charset utf-8;
    ssl_certificate     {cert_path};
    ssl_certificate_key {key_path};
    ssl_session_cache   shared:SSL:10m;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;
    location / {{ try_files $uri $uri/ /index.php?$query_string; }}
    location ~ \\.php$ {{
        fastcgi_pass  unix:/run/php/php8.3-fpm.sock;
        fastcgi_index index.php;
        fastcgi_param SCRIPT_FILENAME $realpath_root$fastcgi_script_name;
        include       fastcgi_params;
    }}
    location ~ /\\.(?!well-known).* {{ deny all; }}
    client_max_body_size 100m;
    client_body_timeout 120;
    sendfile off;
}}
server {{
    listen 80;
    listen [::]:80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}
NGINX_SSL_EOF
nginx -t 2>&1
systemctl reload nginx 2>&1 || true
echo "SSL_OK"
""", timeout=30)
        if not ok or "SSL_OK" not in out:
            return await _fail("ssl", "Custom SSL certificate configuration failed.", out)
        states["ssl"] = "done"

    else:
        states["ssl"] = "done"

    # ── STEP 16 — services ────────────────────────────────────────────────
    await _update("services", "Configuring queue worker and scheduler services…")
    ok, out = await _exec(container, r"""
cat > /etc/systemd/system/pteroq.service << 'SVC_EOF'
[Unit]
Description=Pterodactyl Queue Worker
After=redis-server.service mariadb.service

[Service]
User=www-data
Group=www-data
Restart=always
ExecStart=/usr/bin/php8.3 /var/www/pterodactyl/artisan queue:work --queue=high,standard,low --sleep=3 --tries=3 --max-time=3600
StartLimitInterval=180
StartLimitBurst=30
RestartSec=5s

[Install]
WantedBy=multi-user.target
SVC_EOF

cat > /etc/systemd/system/ptero-schedule.service << 'SCHED_SVC_EOF'
[Unit]
Description=Pterodactyl Scheduler

[Service]
Type=oneshot
User=www-data
ExecStart=/usr/bin/php8.3 /var/www/pterodactyl/artisan schedule:run
SCHED_SVC_EOF

cat > /etc/systemd/system/ptero-schedule.timer << 'SCHED_TIMER_EOF'
[Unit]
Description=Pterodactyl Scheduler Timer

[Timer]
OnCalendar=*:0/1
Persistent=true

[Install]
WantedBy=timers.target
SCHED_TIMER_EOF

systemctl daemon-reload 2>&1 || true
systemctl enable pteroq --quiet 2>&1 || true
systemctl enable ptero-schedule.timer --quiet 2>&1 || true
systemctl start  pteroq               2>&1 || true
systemctl start  ptero-schedule.timer 2>&1 || true
echo "SERVICES_OK"
""", timeout=60)
    if not ok or "SERVICES_OK" not in out:
        return await _fail("services", "Service configuration failed.", out)
    states["services"] = "done"

    # ── STEP 17 — verify ──────────────────────────────────────────────────
    await _update("verify", "Verifying installation…")
    await asyncio.sleep(3)
    ok, out = await _exec(container, r"""
ERRORS=0
systemctl is-active mariadb    >/dev/null 2>&1 || { echo "FAIL: mariadb not active";   ERRORS=$((ERRORS+1)); }
redis-cli ping 2>/dev/null | grep -q PONG     || { echo "FAIL: redis not responding";  ERRORS=$((ERRORS+1)); }
systemctl is-active php8.3-fpm >/dev/null 2>&1 || { echo "FAIL: php-fpm not active";   ERRORS=$((ERRORS+1)); }
systemctl is-active nginx      >/dev/null 2>&1 || { echo "FAIL: nginx not active";     ERRORS=$((ERRORS+1)); }
systemctl is-active pteroq     >/dev/null 2>&1 || { echo "WARN: pteroq not yet active"; }
[ -f /var/www/pterodactyl/public/index.php ]   || { echo "FAIL: panel index missing";  ERRORS=$((ERRORS+1)); }
cd /var/www/pterodactyl && php8.3 artisan db:show --no-interaction 2>&1 | grep -qi "mysql\|mariadb" || \
    { echo "WARN: db:show could not confirm connection"; }
echo "VERIFY_ERRORS=${ERRORS}"
echo "VERIFY_DONE"
""", timeout=60)

    errors_match = re.search(r"VERIFY_ERRORS=(\d+)", out)
    error_count  = int(errors_match.group(1)) if errors_match else 99
    if "VERIFY_DONE" not in out or error_count > 2:
        return await _fail("verify", f"Verification found {error_count} critical issue(s).", out)
    states["verify"] = "done"

    # ── SUCCESS ───────────────────────────────────────────────────────────
    panel_url = f"{'http' if ssl_mode == 'http' else 'https'}://{domain}"
    for k, _ in steps:
        states[k] = "done"

    success_embed = discord.Embed(
        title="✅ Pterodactyl Panel Installed Successfully",
        description=(
            f"━━━━━━━━━━━━━━━━━━\n"
            f"`{_progress_bar(len(steps), len(steps))}`\n"
            f"▸ Container: `{container}`\n\n"
            f"📬 Credentials have been sent to **your DMs**.\n"
            f"🌐 Panel URL: **{panel_url}**"
        ),
        color=0x00ff88,
        timestamp=datetime.utcnow(),
    )
    success_embed.add_field(
        name="📋 Completed Steps",
        value="\n".join(f"✅ {lbl}" for _, lbl in steps),
        inline=False,
    )
    success_embed.add_field(
        name="🚀 Next Steps",
        value=(
            "1. Visit your panel URL and log in\n"
            "2. Add a node in the admin area\n"
            "3. Use `/template` → **Pterodactyl** → **Wings** to set up a Wings node"
        ),
        inline=False,
    )
    success_embed.set_footer(text="DarkNodes VPS Platform  •  Template System")
    await interaction.edit_original_response(embed=success_embed, view=None)

    # DM credentials
    dm_embed = discord.Embed(
        title="🦖 Pterodactyl Panel — Access Credentials",
        description=(
            "━━━━━━━━━━━━━━━━━━\n"
            "Your Pterodactyl Panel has been installed successfully.\n"
            "**Keep this message safe — credentials will not be shown again.**"
        ),
        color=0x5865F2,
        timestamp=datetime.utcnow(),
    )
    dm_embed.add_field(name="🌐 Panel URL",  value=panel_url,           inline=False)
    dm_embed.add_field(name="👤 Username",    value=f"`{adm_user}`",     inline=True)
    dm_embed.add_field(name="📧 Email",       value=f"`{adm_email}`",    inline=True)
    dm_embed.add_field(name="🔑 Password",    value=f"||`{adm_pass}`||", inline=False)
    dm_embed.add_field(name="🗃️ Database",    value=f"`{db_name}`",      inline=True)
    dm_embed.add_field(name="👤 DB User",     value=f"`{db_user}`",      inline=True)
    dm_embed.add_field(name="🔑 DB Password", value=f"||`{db_pass}`||",  inline=False)
    dm_embed.add_field(name="📦 Container",   value=f"`{container}`",    inline=True)
    dm_embed.add_field(name="🔒 SSL",         value=ssl_mode.title(),    inline=True)
    dm_embed.set_footer(text="DarkNodes VPS Platform  •  Never share these credentials")
    try:
        await interaction.user.send(embed=dm_embed)
    except discord.Forbidden:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# PTERODACTYL — WINGS
# ═══════════════════════════════════════════════════════════════════════════════

WINGS_STEPS: List[Tuple[str, str]] = [
    ("check",   "Checking Container Environment"),
    ("binary",  "Installing Wings Binary"),
    ("config",  "Writing Wings Configuration"),
    ("service", "Configuring Systemd Service"),
    ("start",   "Starting Wings"),
    ("verify",  "Verifying Wings"),
]


async def install_wings(
    interaction:  discord.Interaction,
    container:    str,
    panel_url:    str,
    wings_token:  str,
    node_id:      str,
) -> None:
    steps  = WINGS_STEPS
    states: Dict[str, str] = {k: "pending" for k, _ in steps}

    async def _update(current_key: str, note: str = "", failed: bool = False) -> None:
        found = False
        for k, _ in steps:
            if k == current_key:
                states[k] = "error" if failed else "running"
                found = True
            elif not found:
                if states[k] not in ("done", "error"):
                    states[k] = "done"
        embed = _build_progress_embed(
            "🦅 Installing Pterodactyl Wings",
            steps, states, container, note, failed=failed,
        )
        try:
            await interaction.edit_original_response(embed=embed)
        except Exception:
            pass

    async def _fail(key: str, reason: str, log: str) -> None:
        states[key] = "error"
        short_log = log[-600:] if log else "(no output)"
        embed = _build_progress_embed(
            "❌ Wings Installation Failed",
            steps, states, container,
            f"**Step failed:** `{key}`\n**Reason:** {reason[:200]}\n```{short_log[-400:]}```",
            failed=True,
        )
        embed.add_field(
            name="🔄 What to do",
            value="Click **Retry** below after fixing the issue.",
            inline=False,
        )
        view = _WingsRetryView(interaction, container, panel_url, wings_token, node_id)
        try:
            await interaction.edit_original_response(embed=embed, view=view)
        except Exception:
            pass

    # STEP 1 — check
    await _update("check", "Checking Docker availability inside container…")
    ok, out = await _exec(container, r"""
docker info >/dev/null 2>&1 && echo "DOCKER_OK" || echo "DOCKER_FAIL"
uname -m
echo "CHECK_DONE"
""", timeout=20)
    if "DOCKER_OK" not in out:
        return await _fail("check", "Docker is not accessible inside the container.", out)
    arch = "arm64" if "aarch64" in out.lower() else "amd64"
    states["check"] = "done"

    # STEP 2 — wings binary
    await _update("binary", f"Downloading Wings binary ({arch})…")
    ok, out = await _exec(container, f"""
mkdir -p /etc/pterodactyl
curl -Lo /usr/local/bin/wings \\
  https://github.com/pterodactyl/wings/releases/latest/download/wings_linux_{arch} \\
  2>&1 | tail -5
chmod +x /usr/local/bin/wings
wings --version 2>&1 | head -1
echo "WINGS_BIN_OK"
""", timeout=120)
    if not ok or "WINGS_BIN_OK" not in out:
        return await _fail("binary", "Wings binary download failed.", out)
    states["binary"] = "done"

    # STEP 3 — config
    await _update("config", "Fetching Wings configuration from Panel…")
    panel_clean = panel_url.rstrip("/")
    ok, out = await _exec(container, f"""
mkdir -p /etc/pterodactyl
wings configure \\
    --panel-url "{panel_clean}" \\
    --token    "{wings_token}"  \\
    --node     "{node_id}"     \\
    --override 2>&1 | tail -10
ls /etc/pterodactyl/config.yml >/dev/null 2>&1
echo "CONFIG_OK"
""", timeout=60)
    if not ok or "CONFIG_OK" not in out:
        return await _fail("config", "Wings configuration failed. Verify the Panel URL, token, and node ID.", out)
    states["config"] = "done"

    # STEP 4 — service
    await _update("service", "Installing Wings systemd service…")
    ok, out = await _exec(container, r"""
cat > /etc/systemd/system/wings.service << 'SVC_EOF'
[Unit]
Description=Pterodactyl Wings Daemon
After=docker.service
Requires=docker.service

[Service]
User=root
WorkingDirectory=/etc/pterodactyl
LimitNOFILE=4096
PIDFile=/var/run/wings/daemon.pid
ExecStart=/usr/local/bin/wings
Restart=on-failure
StartLimitInterval=180
StartLimitBurst=30
RestartSec=5s

[Install]
WantedBy=multi-user.target
SVC_EOF
systemctl daemon-reload 2>&1 || true
systemctl enable wings --quiet 2>&1 || true
echo "SERVICE_OK"
""", timeout=30)
    if not ok or "SERVICE_OK" not in out:
        return await _fail("service", "Wings service configuration failed.", out)
    states["service"] = "done"

    # STEP 5 — start
    await _update("start", "Starting Wings daemon…")
    ok, out = await _exec(container, r"""
systemctl start wings 2>&1 | tail -5
sleep 3
systemctl is-active wings 2>&1 | head -1
echo "START_DONE"
""", timeout=30)
    states["start"] = "done"

    # STEP 6 — verify
    await _update("verify", "Verifying Wings connectivity…")
    await asyncio.sleep(3)
    ok, out = await _exec(container, r"""
systemctl is-active wings >/dev/null 2>&1 && echo "WINGS_ACTIVE" || echo "WINGS_INACTIVE"
echo "VERIFY_DONE"
""", timeout=20)
    if "WINGS_ACTIVE" not in out:
        return await _fail("verify", "Wings service is not running after start.", out)
    states["verify"] = "done"

    # SUCCESS
    for k, _ in steps:
        states[k] = "done"

    success_embed = discord.Embed(
        title="✅ Pterodactyl Wings Installed Successfully",
        description=(
            f"━━━━━━━━━━━━━━━━━━\n"
            f"`{_progress_bar(len(steps), len(steps))}`\n"
            f"▸ Container: `{container}`\n\n"
            "Wings is now running and connected to your Panel."
        ),
        color=0x00ff88,
        timestamp=datetime.utcnow(),
    )
    success_embed.add_field(
        name="📋 Completed",
        value="\n".join(f"✅ {lbl}" for _, lbl in steps),
        inline=False,
    )
    success_embed.add_field(
        name="🚀 Next Steps",
        value=(
            "1. Go to your Panel → Admin → Nodes\n"
            "2. Verify the node shows **Green / Connected**\n"
            "3. Create allocations and start deploying game servers!"
        ),
        inline=False,
    )
    success_embed.set_footer(text="DarkNodes VPS Platform  •  Template System")
    await interaction.edit_original_response(embed=success_embed, view=None)


# ═══════════════════════════════════════════════════════════════════════════════
# CLOUDFLARE TUNNEL
# ═══════════════════════════════════════════════════════════════════════════════

CF_STEPS: List[Tuple[str, str]] = [
    ("download", "Downloading cloudflared Binary"),
    ("detect",   "Detecting Installed Services"),
    ("service",  "Installing Tunnel as System Service"),
    ("start",    "Starting Cloudflare Tunnel"),
    ("verify",   "Verifying Tunnel Service"),
]


async def _detect_services(container: str) -> Dict:
    """Detect Panel and Wings inside the container and return their port/scheme info."""
    _, out = await _exec(container, r"""
PANEL_INSTALLED=no
PANEL_PORT=80
PANEL_SCHEME=http
if [ -f /var/www/pterodactyl/public/index.php ]; then
    PANEL_INSTALLED=yes
    if [ -f /etc/nginx/sites-available/pterodactyl.conf ]; then
        if grep -q "listen 443" /etc/nginx/sites-available/pterodactyl.conf 2>/dev/null; then
            PANEL_PORT=443
            PANEL_SCHEME=https
        else
            PANEL_PORT=80
            PANEL_SCHEME=http
        fi
    fi
fi
echo "PANEL_INSTALLED=${PANEL_INSTALLED}"
echo "PANEL_PORT=${PANEL_PORT}"
echo "PANEL_SCHEME=${PANEL_SCHEME}"

WINGS_INSTALLED=no
WINGS_PORT=443
WINGS_SCHEME=https
if [ -f /usr/local/bin/wings ] && [ -f /etc/pterodactyl/config.yml ]; then
    WINGS_INSTALLED=yes
    _port=$(grep -A3 "^api:" /etc/pterodactyl/config.yml 2>/dev/null \
            | grep "port:" | head -1 | awk '{print $2}' | tr -d '"'"'"' ')
    [ -n "$_port" ] && [ "$_port" -eq "$_port" ] 2>/dev/null && WINGS_PORT="$_port"
    _ssl=$(grep -A10 "^api:" /etc/pterodactyl/config.yml 2>/dev/null \
           | grep -A5 "ssl:" | grep "enabled:" | head -1 | awk '{print $2}')
    if [ "$_ssl" = "true" ]; then
        WINGS_SCHEME=https
    else
        WINGS_SCHEME=http
    fi
fi
echo "WINGS_INSTALLED=${WINGS_INSTALLED}"
echo "WINGS_PORT=${WINGS_PORT}"
echo "WINGS_SCHEME=${WINGS_SCHEME}"
echo "DETECT_DONE"
""", timeout=20)

    def _get(key: str, default: str) -> str:
        m = re.search(rf"^{re.escape(key)}=(.+)$", out, re.MULTILINE)
        return m.group(1).strip() if m else default

    return {
        "panel_installed": _get("PANEL_INSTALLED", "no") == "yes",
        "panel_scheme":    _get("PANEL_SCHEME",    "http"),
        "panel_port":      _get("PANEL_PORT",      "80"),
        "wings_installed": _get("WINGS_INSTALLED", "no") == "yes",
        "wings_scheme":    _get("WINGS_SCHEME",    "https"),
        "wings_port":      _get("WINGS_PORT",      "443"),
    }


async def install_cloudflare_tunnel(
    interaction:  discord.Interaction,
    container:    str,
    tunnel_name:  str,
    tunnel_token: str,
) -> None:
    steps  = CF_STEPS
    states: Dict[str, str] = {k: "pending" for k, _ in steps}

    async def _update(current_key: str, note: str = "", failed: bool = False) -> None:
        found = False
        for k, _ in steps:
            if k == current_key:
                states[k] = "error" if failed else "running"
                found = True
            elif not found:
                if states[k] not in ("done", "error"):
                    states[k] = "done"
        embed = _build_progress_embed(
            "☁️ Installing Cloudflare Tunnel",
            steps, states, container, note,
            color=0xF6821F,
            failed=failed,
        )
        try:
            await interaction.edit_original_response(embed=embed)
        except Exception:
            pass

    async def _fail(key: str, reason: str, log: str) -> None:
        states[key] = "error"
        short_log = log[-600:] if log else "(no output)"
        embed = _build_progress_embed(
            "❌ Cloudflare Tunnel Installation Failed",
            steps, states, container,
            f"**Step failed:** `{key}`\n**Reason:** {reason[:200]}\n```{short_log[-400:]}```",
            color=0xF6821F,
            failed=True,
        )
        embed.add_field(
            name="🔄 What to do",
            value="Click **Retry** below after resolving the issue.",
            inline=False,
        )
        view = _CFRetryView(interaction, container, tunnel_name, tunnel_token)
        try:
            await interaction.edit_original_response(embed=embed, view=view)
        except Exception:
            pass

    # STEP 1 — download
    await _update("download", "Detecting architecture and downloading cloudflared…")
    ok, out = await _exec(container, r"""
ARCH=$(uname -m)
if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
    CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
else
    CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
fi
curl -fsSL -o /usr/local/bin/cloudflared "$CF_URL" 2>&1 | tail -3
chmod +x /usr/local/bin/cloudflared
cloudflared --version 2>&1 | head -1
echo "DOWNLOAD_OK"
""", timeout=120)
    if not ok or "DOWNLOAD_OK" not in out:
        return await _fail("download", "Failed to download cloudflared binary.", out)
    states["download"] = "done"

    # STEP 2 — detect
    await _update("detect", "Scanning container for installed services…")
    services = await _detect_services(container)
    states["detect"] = "done"

    # STEP 3 — service
    await _update("service", "Creating Cloudflare Tunnel systemd service…")
    ok, out = await _exec(container, f"""
mkdir -p /etc/cloudflared
cat > /etc/cloudflared/cloudflared.env << 'ENV_EOF'
TUNNEL_TOKEN={tunnel_token}
TUNNEL_NAME={tunnel_name}
ENV_EOF
chmod 600 /etc/cloudflared/cloudflared.env

cat > /etc/systemd/system/cloudflared.service << 'SVC_EOF'
[Unit]
Description=Cloudflare Tunnel ({tunnel_name})
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
EnvironmentFile=/etc/cloudflared/cloudflared.env
ExecStart=/usr/local/bin/cloudflared --no-autoupdate tunnel run --token ${{TUNNEL_TOKEN}}
Restart=on-failure
RestartSec=5s
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
SVC_EOF

systemctl daemon-reload 2>&1 | head -3
systemctl enable cloudflared --quiet 2>&1 || true
echo "SERVICE_OK"
""", timeout=30)
    if not ok or "SERVICE_OK" not in out:
        return await _fail("service", "Failed to create cloudflared systemd service.", out)
    states["service"] = "done"

    # STEP 4 — start
    await _update("start", "Starting Cloudflare Tunnel…")
    ok, out = await _exec(container, r"""
systemctl start cloudflared 2>&1 | head -5
sleep 4
systemctl is-active cloudflared 2>&1 | head -1
echo "START_DONE"
""", timeout=30)
    states["start"] = "done"

    # STEP 5 — verify
    await _update("verify", "Verifying Cloudflare Tunnel is running…")
    await asyncio.sleep(3)
    ok, out = await _exec(container, r"""
systemctl is-active cloudflared >/dev/null 2>&1 \
    && echo "TUNNEL_ACTIVE" \
    || echo "TUNNEL_INACTIVE"
journalctl -u cloudflared --no-pager -n 8 2>/dev/null | tail -8 || true
echo "VERIFY_DONE"
""", timeout=20)
    if "TUNNEL_ACTIVE" not in out:
        log_snippet = "\n".join(
            l for l in out.splitlines() if l and "VERIFY_DONE" not in l
        )
        return await _fail(
            "verify",
            "Cloudflare Tunnel service is not running. Check the token and try again.",
            log_snippet,
        )
    states["verify"] = "done"

    # SUCCESS
    for k, _ in steps:
        states[k] = "done"

    success_embed = discord.Embed(
        title="✅ Cloudflare Tunnel Installed Successfully",
        description=(
            f"━━━━━━━━━━━━━━━━━━\n"
            f"`{_progress_bar(len(steps), len(steps))}`\n"
            f"▸ Container: `{container}`\n\n"
            f"**Tunnel name:** `{tunnel_name}`\n"
            "Your tunnel is running and connected to Cloudflare.\n\n"
            "📬 Configuration instructions have been sent to your **DMs**."
        ),
        color=0x00ff88,
        timestamp=datetime.utcnow(),
    )
    success_embed.add_field(
        name="📋 Completed",
        value="\n".join(f"✅ {lbl}" for _, lbl in steps),
        inline=False,
    )
    success_embed.add_field(
        name="🚀 Next Step",
        value=(
            "Add **Public Hostnames** in your Cloudflare Zero Trust Dashboard.\n"
            "Full instructions are in your DMs."
        ),
        inline=False,
    )
    success_embed.set_footer(text="DarkNodes VPS Platform  •  Template System")
    await interaction.edit_original_response(embed=success_embed, view=None)

    # DM
    try:
        await interaction.user.send(embed=_build_cf_dm_embed(tunnel_name, container, services))
    except discord.Forbidden:
        pass


def _build_cf_dm_embed(tunnel_name: str, container: str, services: Dict) -> discord.Embed:
    embed = discord.Embed(
        title="☁️ Cloudflare Tunnel Installed Successfully",
        description="Your tunnel is running.",
        color=0xF6821F,
        timestamp=datetime.utcnow(),
    )

    embed.add_field(
        name="📍 Cloudflare Dashboard",
        value=(
            "**[one.dash.cloudflare.com](https://one.dash.cloudflare.com/)**\n"
            "→ **Networks**\n"
            f"→ **Tunnels**\n"
            f"→ **`{tunnel_name}`**\n"
            "→ **Public Hostnames**\n"
            "→ **Add Public Hostname**"
        ),
        inline=False,
    )

    if services.get("panel_installed"):
        p_scheme = services["panel_scheme"].upper()
        p_port   = services["panel_port"]
        embed.add_field(
            name="━━━━━━━━━━━━━━━━━━━━━━━━━━\n🦖 Pterodactyl Panel",
            value=(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "**Hostname**\n`panel.yourdomain.com`\n\n"
                f"**Service**\n`{p_scheme}`\n\n"
                "**URL / Host**\n`127.0.0.1`\n\n"
                f"**Port**\n`{p_port}`\n\n"
                "**Advanced Settings**\n"
                "TLS Verify → `OFF`\n\n"
                "_Leave every other setting at its default value._\n"
                "_Click **Save**._"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="━━━━━━━━━━━━━━━━━━━━━━━━━━\n🦖 Pterodactyl Panel",
            value=(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Panel was **not detected** on this VPS.\n"
                "If you install it later, add a Public Hostname:\n\n"
                "**Hostname:** `panel.yourdomain.com`\n"
                "**Service:** `HTTP`\n"
                "**URL / Host:** `127.0.0.1`\n"
                "**Port:** `80`\n"
                "**Advanced Settings → TLS Verify:** `OFF`"
            ),
            inline=False,
        )

    if services.get("wings_installed"):
        w_scheme = services["wings_scheme"].upper()
        w_port   = services["wings_port"]
        embed.add_field(
            name="━━━━━━━━━━━━━━━━━━━━━━━━━━\n🪽 Pterodactyl Wings",
            value=(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Create another **Public Hostname**:\n\n"
                "**Hostname**\n`wings.yourdomain.com`\n\n"
                f"**Service**\n`{w_scheme}`\n\n"
                "**URL / Host**\n`127.0.0.1`\n\n"
                f"**Port**\n`{w_port}`\n\n"
                "**Advanced Settings**\n"
                "TLS Verify → `OFF`\n\n"
                "_Leave every other setting at its default value._\n"
                "_Click **Save**._"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="━━━━━━━━━━━━━━━━━━━━━━━━━━\n🪽 Pterodactyl Wings",
            value=(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Wings was **not detected** on this VPS.\n"
                "If you install it later, add a Public Hostname:\n\n"
                "**Hostname:** `wings.yourdomain.com`\n"
                "**Service:** `HTTPS`\n"
                "**URL / Host:** `127.0.0.1`\n"
                "**Port:** `443`\n"
                "**Advanced Settings → TLS Verify:** `OFF`"
            ),
            inline=False,
        )

    after_lines: List[str] = []
    if services.get("panel_installed"):
        after_lines.append("**Panel:**\nhttps://panel.yourdomain.com")
    if services.get("wings_installed"):
        after_lines.append("**Wings:**\nhttps://wings.yourdomain.com")

    after_body = (
        "\n\n".join(after_lines)
        if after_lines
        else "_No Pterodactyl services detected — add hostnames for any services you expose._"
    )
    embed.add_field(
        name="━━━━━━━━━━━━━━━━━━━━━━━━━━\n✅ After Both Hostnames Are Created",
        value=(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + after_body
            + "\n\n_Replace `yourdomain.com` with your actual domain._"
        ),
        inline=False,
    )
    embed.add_field(
        name="💡 Tips",
        value=(
            "• Cloudflare Tunnel handles HTTPS automatically — no open ports needed\n"
            "• DNS may take up to 60 s to propagate after adding a hostname\n"
            "• Check tunnel status: `systemctl status cloudflared` inside your VPS\n"
            f"• Tunnel name: `{tunnel_name}`  •  Container: `{container}`"
        ),
        inline=False,
    )
    embed.set_footer(text="DarkNodes VPS Platform  •  Never share your tunnel token")
    return embed


# ═══════════════════════════════════════════════════════════════════════════════
# MODALS
# ═══════════════════════════════════════════════════════════════════════════════

class _DomainModal(discord.ui.Modal, title="🦖 Pterodactyl Panel — Domain"):
    domain = discord.ui.TextInput(
        label="Domain Name",
        placeholder="panel.example.com",
        required=True,
        min_length=4,
        max_length=253,
    )

    def __init__(self, container: str) -> None:
        super().__init__()
        self.container = container

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.domain.value.strip().lower()
        raw = re.sub(r"^https?://", "", raw).rstrip("/")
        if not re.match(r"^[a-z0-9]([a-z0-9\-\.]*[a-z0-9])?$", raw):
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ Invalid Domain",
                    description=f"`{raw}` doesn't look like a valid domain name.",
                    color=0xff3366,
                ).set_footer(text="DarkNodes VPS Platform  •  Template System"),
                ephemeral=True,
            )
            return
        view  = _HTTPSSelectView(self.container, raw)
        embed = discord.Embed(
            title="🔒 SSL / HTTPS Configuration",
            description=(
                f"━━━━━━━━━━━━━━━━━━\n"
                f"**Domain:** `{raw}`\n\n"
                "How should Pterodactyl serve HTTPS?"
            ),
            color=0x5865F2,
        )
        embed.set_footer(text="DarkNodes VPS Platform  •  Template System")
        await interaction.response.edit_message(embed=embed, view=view)


class _SSLCertModal(discord.ui.Modal, title="🔒 Custom SSL Certificate Paths"):
    cert = discord.ui.TextInput(
        label="Certificate Path (.crt / .pem)",
        placeholder="/etc/ssl/certs/panel.crt",
        required=True,
    )
    key = discord.ui.TextInput(
        label="Private Key Path (.key / .pem)",
        placeholder="/etc/ssl/private/panel.key",
        required=True,
    )

    def __init__(self, container: str, domain: str) -> None:
        super().__init__()
        self.container = container
        self.domain    = domain

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cert_path = self.cert.value.strip()
        key_path  = self.key.value.strip()
        embed = _build_progress_embed(
            "🚀 Installing Pterodactyl Panel",
            PANEL_STEPS,
            {k: "pending" for k, _ in PANEL_STEPS},
            self.container,
            "Queued — starting shortly…",
        )
        await interaction.response.edit_message(embed=embed, view=None)
        asyncio.create_task(
            install_panel(interaction, self.container, self.domain, "custom", cert_path, key_path)
        )


class _WingsModal(discord.ui.Modal, title="🦅 Wings — Panel Connection"):
    panel_url = discord.ui.TextInput(
        label="Panel URL",
        placeholder="https://panel.example.com",
        required=True,
    )
    token = discord.ui.TextInput(
        label="Wings Token (from Panel → Nodes → Auto-Deploy)",
        placeholder="Paste your Wings token here",
        required=True,
        min_length=20,
    )
    node_id = discord.ui.TextInput(
        label="Node ID (number shown in Panel → Nodes)",
        placeholder="1",
        required=True,
        max_length=6,
    )

    def __init__(self, container: str) -> None:
        super().__init__()
        self.container = container

    async def on_submit(self, interaction: discord.Interaction) -> None:
        panel = self.panel_url.value.strip().rstrip("/")
        token = self.token.value.strip()
        nid   = self.node_id.value.strip()
        if not nid.isdigit():
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ Invalid Node ID",
                    description="Node ID must be a number (e.g. `1`).",
                    color=0xff3366,
                ).set_footer(text="DarkNodes VPS Platform  •  Template System"),
                ephemeral=True,
            )
            return
        embed = _build_progress_embed(
            "🦅 Installing Pterodactyl Wings",
            WINGS_STEPS,
            {k: "pending" for k, _ in WINGS_STEPS},
            self.container,
            "Queued — starting shortly…",
        )
        await interaction.response.edit_message(embed=embed, view=None)
        asyncio.create_task(install_wings(interaction, self.container, panel, token, nid))


class _CFModal(discord.ui.Modal, title="☁️ Cloudflare Tunnel Setup"):
    tunnel_name = discord.ui.TextInput(
        label="Tunnel Name",
        placeholder="my-vps  (any label you like)",
        required=True,
        min_length=1,
        max_length=64,
    )
    tunnel_token = discord.ui.TextInput(
        label="Tunnel Token",
        placeholder="Paste your Cloudflare Tunnel token here",
        required=True,
        min_length=20,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, container: str) -> None:
        super().__init__()
        self.container = container

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name  = re.sub(r"[^a-zA-Z0-9\-_]", "-", self.tunnel_name.value.strip())[:64]
        token = self.tunnel_token.value.strip()
        embed = _build_progress_embed(
            "☁️ Installing Cloudflare Tunnel",
            CF_STEPS,
            {k: "pending" for k, _ in CF_STEPS},
            self.container,
            "Queued — starting installation…",
            color=0xF6821F,
        )
        await interaction.response.edit_message(embed=embed, view=None)
        asyncio.create_task(install_cloudflare_tunnel(interaction, self.container, name, token))


# ═══════════════════════════════════════════════════════════════════════════════
# VIEWS
# ═══════════════════════════════════════════════════════════════════════════════

class _TemplateSelectView(discord.ui.View):
    """Top-level template choice: Pterodactyl or Cloudflare Tunnel."""

    def __init__(self, container: str) -> None:
        super().__init__(timeout=300)
        self.container = container

    @discord.ui.button(label="🦖  Pterodactyl", style=discord.ButtonStyle.success, row=0)
    async def pterodactyl(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="🦖 Pterodactyl — What to Install?",
            description=(
                f"━━━━━━━━━━━━━━━━━━\n"
                f"▸ Container: `{self.container}`\n\n"
                "**Panel** — The web interface for managing game servers\n"
                "**Wings** — The node daemon that runs game servers (requires a Panel)"
            ),
            color=0x5865F2,
        )
        embed.set_footer(text="DarkNodes VPS Platform  •  Template System")
        await interaction.response.edit_message(embed=embed, view=_PteroComponentView(self.container))

    @discord.ui.button(label="☁️  Cloudflare Tunnel", style=discord.ButtonStyle.primary, row=0)
    async def cloudflare(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="☁️ Cloudflare Tunnel",
            description=(
                "━━━━━━━━━━━━━━━━━━\n"
                f"▸ Container: `{self.container}`\n\n"
                "**Cloudflare Tunnel** lets you expose services running on your VPS "
                "to the internet — no open ports, no firewall changes.\n\n"
                "You'll need:\n"
                "• A **Tunnel Name** (any label you like, e.g. `my-vps`)\n"
                "• A **Tunnel Token** — from [Cloudflare Zero Trust Dashboard]"
                "(https://one.dash.cloudflare.com/) → Networks → Tunnels → "
                "Create a Tunnel → Docker → copy the token from the command shown"
            ),
            color=0xF6821F,
        )
        embed.set_footer(text="DarkNodes VPS Platform  •  Template System")
        await interaction.response.edit_message(embed=embed, view=_CFInfoView(self.container))

    @discord.ui.button(label="◀  Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _show_vps_selector(interaction)


class _PteroComponentView(discord.ui.View):
    """Panel vs Wings choice."""

    def __init__(self, container: str) -> None:
        super().__init__(timeout=300)
        self.container = container

    @discord.ui.button(label="🖥️  Panel", style=discord.ButtonStyle.success)
    async def panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_DomainModal(self.container))

    @discord.ui.button(label="🦅  Wings (Daemon)", style=discord.ButtonStyle.primary)
    async def wings(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="🦅 Install Pterodactyl Wings",
            description=(
                "━━━━━━━━━━━━━━━━━━\n"
                "Wings is the **node daemon** that runs game servers.\n\n"
                "You'll need:\n"
                "• A running Pterodactyl **Panel** (can be on another VPS)\n"
                "• The **Wings Token** — from Panel → Admin → Nodes → click a node → Auto-Deploy\n"
                "• The **Node ID** shown in that same screen"
            ),
            color=0x5865F2,
        )
        embed.set_footer(text="DarkNodes VPS Platform  •  Template System")
        await interaction.response.edit_message(embed=embed, view=_WingsInfoView(self.container))

    @discord.ui.button(label="◀  Back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed, view = _template_select_embed_view(self.container)
        await interaction.response.edit_message(embed=embed, view=view)


class _WingsInfoView(discord.ui.View):
    def __init__(self, container: str) -> None:
        super().__init__(timeout=300)
        self.container = container

    @discord.ui.button(label="▶  Continue", style=discord.ButtonStyle.success)
    async def proceed(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_WingsModal(self.container))

    @discord.ui.button(label="◀  Back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="🦖 Pterodactyl — What to Install?",
            description=(
                f"━━━━━━━━━━━━━━━━━━\n"
                f"▸ Container: `{self.container}`\n\n"
                "**Panel** — The web interface for managing game servers\n"
                "**Wings** — The node daemon that runs game servers (requires a Panel)"
            ),
            color=0x5865F2,
        )
        embed.set_footer(text="DarkNodes VPS Platform  •  Template System")
        await interaction.response.edit_message(embed=embed, view=_PteroComponentView(self.container))


class _HTTPSSelectView(discord.ui.View):
    def __init__(self, container: str, domain: str) -> None:
        super().__init__(timeout=300)
        self.container = container
        self.domain    = domain

    @discord.ui.button(label="🔒 Let's Encrypt", style=discord.ButtonStyle.success, row=0)
    async def letsencrypt(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = _build_progress_embed(
            "🚀 Installing Pterodactyl Panel",
            PANEL_STEPS,
            {k: "pending" for k, _ in PANEL_STEPS},
            self.container,
            "Queued — starting shortly…",
        )
        await interaction.response.edit_message(embed=embed, view=None)
        asyncio.create_task(install_panel(interaction, self.container, self.domain, "letsencrypt"))

    @discord.ui.button(label="🌐 HTTP Only (No SSL)", style=discord.ButtonStyle.secondary, row=0)
    async def http_only(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = _build_progress_embed(
            "🚀 Installing Pterodactyl Panel",
            PANEL_STEPS,
            {k: "pending" for k, _ in PANEL_STEPS},
            self.container,
            "Queued — starting shortly…",
        )
        await interaction.response.edit_message(embed=embed, view=None)
        asyncio.create_task(install_panel(interaction, self.container, self.domain, "http"))

    @discord.ui.button(label="📜 I Already Have an SSL Certificate", style=discord.ButtonStyle.primary, row=1)
    async def custom_ssl(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_SSLCertModal(self.container, self.domain))


class _CFInfoView(discord.ui.View):
    def __init__(self, container: str) -> None:
        super().__init__(timeout=300)
        self.container = container

    @discord.ui.button(label="▶  Continue", style=discord.ButtonStyle.success)
    async def proceed(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_CFModal(self.container))

    @discord.ui.button(label="◀  Back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed, view = _template_select_embed_view(self.container)
        await interaction.response.edit_message(embed=embed, view=view)


# ─── Retry views ──────────────────────────────────────────────────────────────

class _PanelRetryView(discord.ui.View):
    def __init__(self, interaction, container, domain, ssl_mode, cert_path, key_path):
        super().__init__(timeout=300)
        self._args = (interaction, container, domain, ssl_mode, cert_path, key_path)

    @discord.ui.button(label="🔄 Retry Installation", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()
        asyncio.create_task(install_panel(*self._args))


class _WingsRetryView(discord.ui.View):
    def __init__(self, interaction, container, panel_url, token, node_id):
        super().__init__(timeout=300)
        self._args = (interaction, container, panel_url, token, node_id)

    @discord.ui.button(label="🔄 Retry Installation", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()
        asyncio.create_task(install_wings(*self._args))


class _CFRetryView(discord.ui.View):
    def __init__(self, interaction, container, tunnel_name, tunnel_token):
        super().__init__(timeout=300)
        self._args = (interaction, container, tunnel_name, tunnel_token)

    @discord.ui.button(label="🔄 Retry Installation", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()
        asyncio.create_task(install_cloudflare_tunnel(*self._args))


# ─── VPS selector ─────────────────────────────────────────────────────────────

class _VPSSelectView(discord.ui.View):
    def __init__(self, user_id: str, vps_list: list) -> None:
        super().__init__(timeout=300)
        self.user_id  = user_id
        self.vps_list = vps_list

        options = [
            discord.SelectOption(
                label=f"VPS {i + 1} — {v['container_name']}"[:100],
                description=(
                    f"Status: {v.get('status', 'unknown').upper()} | "
                    f"RAM: {v.get('ram', '?')} CPU: {v.get('cpu', '?')} Core(s)"
                )[:100],
                value=str(i),
                emoji="🖥️",
            )
            for i, v in enumerate(vps_list)
        ]
        sel = discord.ui.Select(
            placeholder="Choose a VPS to install a template on…",
            options=options,
            min_values=1,
            max_values=1,
        )
        sel.callback = self._on_select
        self.add_item(sel)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        idx = int(interaction.data["values"][0])
        vps = self.vps_list[idx]
        container = vps["container_name"]

        if vps.get("status", "running") not in ("running", "active"):
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⚠️ VPS Not Running",
                    description=(
                        f"`{container}` is not currently running.\n"
                        "Start it with `!manage` before installing a template."
                    ),
                    color=0xffaa00,
                ).set_footer(text="DarkNodes VPS Platform  •  Template System"),
                ephemeral=True,
            )
            return

        embed, view = _template_select_embed_view(container)
        await interaction.response.edit_message(embed=embed, view=view)


# ─── Shared embed/view builders ───────────────────────────────────────────────

def _template_select_embed_view(container: str):
    embed = discord.Embed(
        title="📦 Template Gallery",
        description=(
            "━━━━━━━━━━━━━━━━━━\n"
            f"▸ Container: `{container}`\n\n"
            "Choose a template to install on this VPS."
        ),
        color=0x5865F2,
    )
    embed.add_field(
        name="Available Templates",
        value=(
            "🦖 **Pterodactyl** — Game server panel & daemon\n"
            "☁️ **Cloudflare Tunnel** — Zero-trust tunnel, no open ports needed"
        ),
        inline=False,
    )
    embed.set_footer(text="DarkNodes VPS Platform  •  Template System")
    return embed, _TemplateSelectView(container)


async def _show_vps_selector(interaction: discord.Interaction) -> None:
    user_id  = str(interaction.user.id)
    vps_data = _get_vps_data()
    vps_list = vps_data.get(user_id, [])

    if not vps_list:
        embed = discord.Embed(
            title="❌ No VPS Found",
            description=(
                "You don't have any VPS instances yet.\n"
                "Use `!create` to deploy your first VPS, then come back."
            ),
            color=0xff3366,
        )
        embed.set_footer(text="DarkNodes VPS Platform  •  Template System")
        try:
            await interaction.response.edit_message(embed=embed, view=None)
        except Exception:
            await interaction.followup.send(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(
        title="🖥️ Select Your VPS",
        description=(
            "━━━━━━━━━━━━━━━━━━\n"
            "Choose which VPS you want to install a template on.\n\n"
            "⚠️ The VPS must be **running** during installation."
        ),
        color=0x5865F2,
    )
    embed.add_field(
        name=f"Your VPS ({len(vps_list)})",
        value="\n".join(
            f"**{i + 1}.** `{v['container_name']}` — {v.get('status', 'unknown').upper()}"
            for i, v in enumerate(vps_list)
        ),
        inline=False,
    )
    embed.set_footer(text="DarkNodes VPS Platform  •  Template System")
    view = _VPSSelectView(user_id, vps_list)
    try:
        await interaction.response.edit_message(embed=embed, view=view)
    except Exception:
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
# /template COMMAND
# ═══════════════════════════════════════════════════════════════════════════════

@discord.app_commands.command(
    name="template",
    description="Install a production-ready software template on one of your VPS instances",
)
async def _cmd_template(interaction: discord.Interaction) -> None:
    user_id  = str(interaction.user.id)
    vps_data = _get_vps_data()
    vps_list = vps_data.get(user_id, [])

    if not vps_list:
        embed = discord.Embed(
            title="❌ No VPS Found",
            description=(
                "You don't have any VPS instances yet.\n"
                "Use `!create` to deploy your first VPS, then come back to `/template`."
            ),
            color=0xff3366,
        )
        embed.set_footer(text="DarkNodes VPS Platform  •  Template System")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(
        title="🖥️ Select Your VPS",
        description=(
            "━━━━━━━━━━━━━━━━━━\n"
            "Choose which VPS you want to install a template on.\n\n"
            "⚠️ The VPS must be **running** during installation."
        ),
        color=0x5865F2,
    )
    embed.add_field(
        name=f"Your VPS Instances ({len(vps_list)})",
        value="\n".join(
            f"**{i + 1}.** `{v['container_name']}` — {v.get('status', 'unknown').upper()}"
            for i, v in enumerate(vps_list)
        ),
        inline=False,
    )
    embed.set_footer(text="DarkNodes VPS Platform  •  Template System")
    await interaction.response.send_message(
        embed=embed,
        view=_VPSSelectView(user_id, vps_list),
        ephemeral=True,
    )
