# This Dockerfile is based upon sysbox example images: https://github.com/nestybox/dockerfiles/
# but with some modifications to have a more generic image.
ARG UBUNTU_VERSION="26.04"
FROM ubuntu:${UBUNTU_VERSION}

ARG UBUNTU_VERSION
ENV DOCKER_VERSION=29.6.1 \
    DOCKER_COMPOSE_VERSION=v5.3.0 \
    BUILDX_VERSION=v0.35.0 \
    DEBIAN_FRONTEND=noninteractive \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    LANGUAGE=en_US:en \
    TZ=UTC \
    # Suppress "which services need restart?" prompts from apt install
    NEEDRESTART_MODE=a \
    NEEDRESTART_SUSPEND=1 \
    # Silence debconf interactive questions globally
    DEBIAN_PRIORITY=critical

#
# Systemd installation
#
RUN apt-get update &&                            \
    apt-get install -y --no-install-recommends   \
            systemd                              \
            systemd-sysv                         \
            libsystemd0                          \
            ca-certificates                      \
            dbus                                 \
            fuse3                                \
            fuse-overlayfs                       \
            iptables                             \
            iproute2                             \
            kmod                                 \
            locales                              \
            sudo                                 \
            udev &&                              \
                                                 \
    # Prevents journald from reading kernel messages from /dev/kmsg
    echo "ReadKMsg=no" >> /etc/systemd/journald.conf &&               \
                                                                      \
    # Housekeeping
    apt-get clean -y &&                                               \
    rm -rf                                                            \
       /var/cache/debconf/*                                           \
       /var/lib/apt/lists/*                                           \
       /var/log/*                                                     \
       /tmp/*                                                         \
       /var/tmp/*                                                     \
       /usr/share/doc/*                                               \
       /usr/share/man/*                                               \
       /usr/share/local/* &&                                          \
                                                                      \
    # Locale
    locale-gen en_US.UTF-8 && update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 && \
    echo "LANG=en_US.UTF-8\nLC_ALL=en_US.UTF-8\nLANGUAGE=en_US:en\nDEBIAN_FRONTEND=noninteractive\nTZ=UTC" > /etc/environment && \
    ln -snf /usr/share/zoneinfo/UTC /etc/localtime && echo UTC > /etc/timezone && \
    \
    # Create default 'admin/admin' user
    useradd --create-home --shell /bin/bash admin && echo "admin:admin" | chpasswd && usermod -aG sudo admin && \
    # Passwordless sudo — same as most cloud VPS providers (DigitalOcean, Vultr, etc.)
    echo "admin ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/admin && chmod 440 /etc/sudoers.d/admin && \
    echo "root  ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/root  && chmod 440 /etc/sudoers.d/root

# ── apt reliability: retries, timeouts, no interactive prompts ────────────────
RUN echo 'APT::Acquire::Retries "5";'                        > /etc/apt/apt.conf.d/80retries && \
    echo 'APT::Acquire::http::Timeout "30";'                >> /etc/apt/apt.conf.d/80retries && \
    echo 'APT::Acquire::https::Timeout "30";'               >> /etc/apt/apt.conf.d/80retries && \
    echo 'DPkg::Options:: "--force-confdef";'                > /etc/apt/apt.conf.d/90dpkg    && \
    echo 'DPkg::Options:: "--force-confold";'               >> /etc/apt/apt.conf.d/90dpkg    && \
    echo 'APT::Install-Recommends "false";'                  > /etc/apt/apt.conf.d/91norecommends

# ── Enable universe + multiverse + restricted repos ──────────────────────────
# Ubuntu 26.04 uses DEB822 format (/etc/apt/sources.list.d/ubuntu.sources).
# add-apt-repository handles both formats correctly.
RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common \
        apt-transport-https \
        gnupg \
        lsb-release && \
    add-apt-repository -y universe && \
    add-apt-repository -y multiverse && \
    add-apt-repository -y restricted && \
    # Also enable in DEB822 format (Ubuntu 26.04+) as a belt-and-suspenders fix
    if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then \
        sed -i 's/^Components: main$/Components: main restricted universe multiverse/' \
            /etc/apt/sources.list.d/ubuntu.sources; \
    fi && \
    apt-get update

# ── Comprehensive package install — split into groups so one bad name never
#    breaks the entire build. Each group uses || true as a safety net.
#    Packages are chosen to be valid on Ubuntu 26.04 (noble/oracular).
# ─────────────────────────────────────────────────────────────────────────────

# Core shell & CLI utilities (all stable, always present)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    bash-completion less man-db manpages manpages-dev \
    moreutils tree watch bc file dos2unix jq pv \
    dialog whiptail expect at cron \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Editors — nano and vim are stable; neovim may be unavailable in some Ubuntu releases, so soft-fail it
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    nano vim \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y neovim 2>/dev/null || true \
    ; apt-get clean && rm -rf /var/lib/apt/lists/*

# Network tools
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    curl wget net-tools iputils-ping iputils-tracepath \
    dnsutils nmap netcat-openbsd traceroute telnet \
    tcpdump iperf3 whois openssh-client openssl socat \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# File & archive tools
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    unzip zip tar gzip bzip2 xz-utils p7zip-full rsync \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# System monitoring & process tools
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    htop iotop ncdu procps lsof strace ltrace sysstat tmux screen \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Optional monitoring (may not exist on all Ubuntu versions — soft fail)
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
    iftop dstat glances 2>/dev/null || \
    DEBIAN_FRONTEND=noninteractive apt-get install -y iftop dstat 2>/dev/null || true \
    ; apt-get clean && rm -rf /var/lib/apt/lists/*

# System info tools (neofetch always, fastfetch/inxi/hwinfo soft-fail)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    neofetch lshw dmidecode \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
    fastfetch inxi hwinfo 2>/dev/null || \
    DEBIAN_FRONTEND=noninteractive apt-get install -y inxi 2>/dev/null || true \
    ; apt-get clean && rm -rf /var/lib/apt/lists/*

# Build tools & compilers
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    build-essential gcc g++ make cmake pkg-config \
    autoconf automake libtool patch binutils \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Dev libraries
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    libssl-dev libffi-dev zlib1g-dev libreadline-dev \
    libbz2-dev libsqlite3-dev libncurses5-dev libncursesw5-dev \
    liblzma-dev libgdbm-dev libexpat1-dev \
    libxml2-dev libxslt1-dev libcurl4-openssl-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Python — full install + python-is-python3 makes `python` work (not just python3)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-pip python3-venv python3-dev python3-full \
    python3-setuptools python3-wheel \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
# python-is-python3 creates the `python` symlink; soft-fail if not available on this distro
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y python-is-python3 2>/dev/null || \
    ln -sf /usr/bin/python3 /usr/local/bin/python \
    ; apt-get clean && rm -rf /var/lib/apt/lists/*
# pip → pip3 symlink so `pip install` works without specifying pip3
RUN ln -sf /usr/bin/pip3 /usr/local/bin/pip 2>/dev/null || true
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y pipx 2>/dev/null || true \
    ; apt-get clean && rm -rf /var/lib/apt/lists/*

# Ruby
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ruby ruby-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Perl, Go
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    perl golang \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Rust
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    rustc cargo \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Java
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    default-jdk default-jre \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Database clients — use default-mysql-client (mysql-client meta removed in 26.04)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    default-mysql-client sqlite3 redis-tools \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
    postgresql-client 2>/dev/null || \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
    postgresql-client-16 2>/dev/null || \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
    postgresql-client-17 2>/dev/null || true \
    ; apt-get clean && rm -rf /var/lib/apt/lists/*

# Web & security
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    apache2-utils nginx fail2ban ufw \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# VCS & misc
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    git git-lfs subversion mercurial \
    coreutils util-linux psmisc hostname \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── pip: allow system-wide installs without --break-system-packages ───────────
# PEP 668 blocks pip on Ubuntu 24.04+. Setting this globally makes pip behave
# like it does on a real VPS where users just run `pip install <anything>`.
RUN mkdir -p /etc/pip && printf '[global]\nbreak-system-packages = true\nno-cache-dir = false\n' \
        > /etc/pip.conf && \
    # Also set for root and future users via skel
    mkdir -p /root/.config/pip /home/admin/.config/pip /etc/skel/.config/pip && \
    cp /etc/pip.conf /root/.config/pip/pip.conf && \
    cp /etc/pip.conf /home/admin/.config/pip/pip.conf && \
    cp /etc/pip.conf /etc/skel/.config/pip/pip.conf

# ── pip: globally useful Python packages ─────────────────────────────────────
RUN pip3 install --no-cache-dir \
    requests httpx flask fastapi uvicorn gunicorn \
    django sqlalchemy alembic psycopg2-binary pymysql \
    redis celery pydantic rich typer click \
    paramiko cryptography pillow \
    pytest black ruff mypy ipython 2>/dev/null || \
    pip3 install --no-cache-dir \
    requests httpx flask fastapi uvicorn gunicorn \
    rich typer click paramiko cryptography 2>/dev/null || true

# Heavier packages in a separate layer (soft-fail — large build deps)
RUN pip3 install --no-cache-dir numpy pandas scipy matplotlib jupyter \
    2>/dev/null || pip3 install --no-cache-dir numpy pandas 2>/dev/null || true

# ── Node.js LTS via NodeSource ────────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ── npm: global prefix = /usr/local so `npm install -g` binaries land in PATH ─
# Root runs as root so /usr/local is always writable. Non-root users get their
# own prefix via the profile.d script below.
RUN npm install -g npm@latest yarn pnpm pm2 tsx ts-node typescript && \
    npm config set prefix '/usr/local' && \
    # For non-root / admin users: use ~/.npm-global and add to PATH via bashrc
    mkdir -p /etc/skel/.npm-global /home/admin/.npm-global && \
    printf 'export NPM_CONFIG_PREFIX="$HOME/.npm-global"\nexport PATH="$HOME/.npm-global/bin:$PATH"\n' \
        >> /etc/skel/.bashrc && \
    printf 'export NPM_CONFIG_PREFIX="/home/admin/.npm-global"\nexport PATH="/home/admin/.npm-global/bin:$PATH"\n' \
        >> /home/admin/.bashrc && \
    chown -R admin:admin /home/admin/.npm-global

# ── profile.d: system-wide PATH for all toolchains ───────────────────────────
# This runs for every login shell (root, admin, any new user).
# Makes `go`, `cargo`, npm globals, pip, python all available without extra setup.
RUN printf '%s\n' \
    '#!/bin/sh' \
    '# Go' \
    'if command -v go >/dev/null 2>&1; then' \
    '    export GOPATH="${GOPATH:-$HOME/go}"' \
    '    export PATH="$GOPATH/bin:/usr/local/go/bin:$PATH"' \
    'fi' \
    '# Cargo / Rust' \
    '[ -d "$HOME/.cargo/bin" ] && export PATH="$HOME/.cargo/bin:$PATH"' \
    '[ -d "/root/.cargo/bin" ] && export PATH="/root/.cargo/bin:$PATH"' \
    '# pip / python' \
    'export PIP_BREAK_SYSTEM_PACKAGES=1' \
    'export DEBIAN_FRONTEND=noninteractive' \
    'export NEEDRESTART_MODE=a' \
    'export NEEDRESTART_SUSPEND=1' \
    > /etc/profile.d/00-darknodes-path.sh && \
    chmod +x /etc/profile.d/00-darknodes-path.sh

# ── Rust/Cargo: install via rustup for latest stable (replaces apt rust) ─────
# The apt rust is often very old. rustup gives users the real experience.
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
    sh -s -- -y --default-toolchain stable --no-modify-path 2>/dev/null || true
# Symlink cargo/rustc into /usr/local/bin so they're in PATH without sourcing ~/.cargo/env
RUN ln -sf /root/.cargo/bin/cargo   /usr/local/bin/cargo   2>/dev/null || true && \
    ln -sf /root/.cargo/bin/rustc   /usr/local/bin/rustc   2>/dev/null || true && \
    ln -sf /root/.cargo/bin/rustup  /usr/local/bin/rustup  2>/dev/null || true && \
    ln -sf /root/.cargo/bin/rustfmt /usr/local/bin/rustfmt 2>/dev/null || true && \
    ln -sf /root/.cargo/bin/clippy-driver /usr/local/bin/clippy-driver 2>/dev/null || true

# ── Go: ensure `go install` binaries land in a PATH-accessible dir ────────────
RUN mkdir -p /root/go/bin /root/go/pkg /root/go/src && \
    ln -sf /root/go/bin /usr/local/go-user-bin 2>/dev/null || true

# ── bash: nice prompt + useful aliases for root and admin ────────────────────
RUN BASHRC_EXTRA=' \
\n# DarkNodes VPS defaults\
\nexport DEBIAN_FRONTEND=noninteractive\
\nexport LANG=en_US.UTF-8\
\nexport LC_ALL=en_US.UTF-8\
\nalias ll="ls -alF"\
\nalias la="ls -A"\
\nalias l="ls -CF"\
\nalias cls="clear"\
\nalias ports="ss -tulpn"\
\nalias myip="curl -s ifconfig.me"\
\nalias update="apt-get update && apt-get upgrade -y"\
\nalias install="apt-get install -y"\
' && \
    printf "$BASHRC_EXTRA" >> /root/.bashrc && \
    printf "$BASHRC_EXTRA" >> /home/admin/.bashrc && \
    printf "$BASHRC_EXTRA" >> /etc/skel/.bashrc

# ── command-not-found: populate handler DB ───────────────────────────────────
RUN apt-get update && \
    update-command-not-found 2>/dev/null || true && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ── needrestart: auto-restart mode so `apt install` never prompts ─────────────
RUN mkdir -p /etc/needrestart/conf.d && \
    printf '$nrconf{restart}     = '"'"'a'"'"';\n$nrconf{kernelhints} = 0;\n$nrconf{ucodehints}  = 0;\n' \
        > /etc/needrestart/conf.d/50-darknodes.conf 2>/dev/null || true

# ── apt: dpkg lock-break and non-interactive defaults ────────────────────────
RUN echo 'APT::Get::Assume-Yes "true";'              > /etc/apt/apt.conf.d/92assumeyes && \
    echo 'APT::Get::Show-Upgraded "false";'         >> /etc/apt/apt.conf.d/92assumeyes

# Disable systemd services/units that are unnecessary within a container.
# Also mask systemd-resolved: it creates a 127.0.0.53 stub that breaks DNS
# inside containers — configure_vps writes /etc/resolv.conf directly instead.
RUN systemctl mask systemd-udevd.service \
                   systemd-udevd-kernel.socket \
                   systemd-udevd-control.socket \
                   systemd-modules-load.service \
                   sys-kernel-debug.mount \
                   sys-kernel-tracing.mount \
                   systemd-resolved.service \
                   systemd-networkd-wait-online.service

# Set iptables backend per Ubuntu version:
#  - 22.04, 24.04: legacy backend (kept for compatibility)
#  - 26.04+: nft backend (iptables-legacy can't initialise the nat table on
#    iptables 1.8.11 shipped in 26.04)
RUN set -eux; \
    case "${UBUNTU_VERSION}" in \
        20.04) ;; \
        22.04|24.04) update-alternatives --set iptables /usr/sbin/iptables-legacy ;; \
        *) update-alternatives --set iptables /usr/sbin/iptables-nft ;; \
    esac

# Install Docker
RUN apt-get update && apt-get install -y wget curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://get.docker.com -o get-docker.sh && sh get-docker.sh --version ${DOCKER_VERSION} \
    # Add user "admin" to the Docker group
    && usermod -a -G docker admin \
    && rm get-docker.sh \
    && docker --version

# Install buildx
RUN set -eux; \
    arch="$(uname -m)"; \
    case "$arch" in \
        x86_64) dockerArch='x86_64' ; buildx_arch='linux-amd64' ;; \
        armhf) dockerArch='armel' ; buildx_arch='linux-arm-v6' ;; \
        armv7) dockerArch='armhf' ; buildx_arch='linux-arm-v7' ;; \
        aarch64) dockerArch='aarch64' ; buildx_arch='linux-arm64' ;; \
        *) echo >&2 "error: unsupported architecture ($arch)"; exit 1 ;; \
    esac && \
    wget -O docker-buildx "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.${buildx_arch}" && \
    mkdir -p /usr/local/lib/docker/cli-plugins && \
    chmod +x docker-buildx && \
    mv docker-buildx /usr/local/lib/docker/cli-plugins/docker-buildx && \
    docker buildx version

# Install Docker Compose
RUN curl --retry 5 --retry-max-time 40 \
    --write-out "%{http_code}\n" \
    -L "https://github.com/docker/compose/releases/download/$DOCKER_COMPOSE_VERSION/docker-compose-$(uname -s)-$(uname -m)" \
    -o /usr/local/bin/docker-compose && \
    chmod 755 /usr/local/bin/docker-compose && \
    mkdir -p /usr/local/lib/docker/cli-plugins && \
    ln -s /usr/local/bin/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose

# Make use of stopsignal (instead of sigterm) to stop systemd containers.
STOPSIGNAL SIGRTMIN+3

# Mask services of no use inside the container
# to avoid problems with --privileged flag
# Source: https://forums.docker.com/t/docker-run-privileged-systemd-kills-all-tty-sessions/8610/3
RUN systemctl mask \
    systemd-logind.service getty.service getty.target

# Volume /var/lib/docker to avoid storage driver issues
VOLUME /var/lib/docker

# ── Docker daemon: fuse-overlayfs + real DNS forwarded into DinD ──────────────
# fuse-overlayfs: overlay2 cannot nest on an overlay2 host without native-overlay-diff.
# dns: ensure containers spawned BY the VPS (DinD) also get working DNS.
# Note: printf is used instead of a heredoc to avoid bash syntax errors in
# environments where BuildKit heredoc support is unavailable.
RUN mkdir -p /etc/docker && printf '{\n  "storage-driver": "fuse-overlayfs",\n  "dns": ["8.8.8.8", "1.1.1.1", "8.8.4.4"],\n  "log-driver": "json-file",\n  "log-opts": {\n    "max-size": "10m",\n    "max-file": "3"\n  },\n  "default-ulimits": {\n    "nofile": {\n      "Name": "nofile",\n      "Hard": 65536,\n      "Soft": 65536\n    }\n  }\n}\n' > /etc/docker/daemon.json

# ── Fallback /etc/resolv.conf — configure_vps overwrites this on first boot ───
# This guarantees DNS works during configure_vps itself (before it writes resolv.conf).
RUN printf 'nameserver 8.8.8.8\nnameserver 1.1.1.1\nnameserver 8.8.4.4\noptions edns0 trust-ad\n' \
        > /tmp/resolv.conf.default && \
    # Store as a template; configure_vps will copy it to /etc/resolv.conf
    cp /tmp/resolv.conf.default /etc/resolv.conf.default

# ── /etc/hosts baseline so hostname resolution works before configure_vps ──────
RUN printf '127.0.0.1\tlocalhost\n127.0.1.1\tDarkNodes-VPS\n::1\tlocalhost ip6-localhost ip6-loopback\n' \
        > /etc/hosts

# Install tmate (static binary — works on all Ubuntu versions)
RUN set -eux; \
    arch="$(uname -m)"; \
    case "$arch" in \
        x86_64)  tmate_arch='amd64' ;; \
        aarch64) tmate_arch='arm64v8' ;; \
        armv7l)  tmate_arch='arm32v7' ;; \
        *) echo >&2 "unsupported arch for tmate: $arch"; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/tmate-io/tmate/releases/download/2.4.0/tmate-2.4.0-static-linux-${tmate_arch}.tar.xz" \
        -o /tmp/tmate.tar.xz && \
    tar -xf /tmp/tmate.tar.xz -C /tmp && \
    mv /tmp/tmate-2.4.0-static-linux-${tmate_arch}/tmate /usr/local/bin/tmate && \
    chmod +x /usr/local/bin/tmate && \
    rm -rf /tmp/tmate* && \
    tmate -V

# Install sshx (web terminal)
RUN curl -sSf https://sshx.io/get | sh -s -- -y && \
    sshx --version 2>/dev/null || true

# Set systemd as entrypoint.
ENTRYPOINT [ "/sbin/init", "--log-level=err" ]
