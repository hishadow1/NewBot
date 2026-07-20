# This Dockerfile is based upon sysbox example images: https://github.com/nestybox/dockerfiles/
# but with some modifications to have a more generic image.
ARG UBUNTU_VERSION="26.04"
FROM ubuntu:${UBUNTU_VERSION}

ARG UBUNTU_VERSION
ENV DOCKER_VERSION=29.6.1 \
    DOCKER_COMPOSE_VERSION=v5.3.0 \
    BUILDX_VERSION=v0.35.0

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
    # Create default 'admin/admin' user
    useradd --create-home --shell /bin/bash admin && echo "admin:admin" | chpasswd && usermod -aG sudo admin

# ── Enable universe + multiverse + restricted repos ──────────────────────────
# Must be a separate layer so apt lists are fresh after repo changes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common \
        apt-transport-https \
        gnupg \
        lsb-release && \
    add-apt-repository -y universe && \
    add-apt-repository -y multiverse && \
    add-apt-repository -y restricted && \
    apt-get update

# ── Comprehensive package install — everything a VPS user expects ─────────────
RUN DEBIAN_FRONTEND=noninteractive apt-get install -y \
    \
    # ── Core CLI & shell tools ────────────────────────────────────────────────
    bash-completion \
    command-not-found \
    less \
    man-db \
    manpages \
    manpages-dev \
    moreutils \
    tree \
    watch \
    bc \
    file \
    dos2unix \
    jq \
    pv \
    dialog \
    whiptail \
    expect \
    at \
    cron \
    \
    # ── Editors ───────────────────────────────────────────────────────────────
    nano \
    vim \
    neovim \
    \
    # ── Network tools ─────────────────────────────────────────────────────────
    curl \
    wget \
    net-tools \
    iputils-ping \
    iputils-tracepath \
    dnsutils \
    bind9-dnsutils \
    nmap \
    netcat-openbsd \
    traceroute \
    telnet \
    tcpdump \
    iperf3 \
    whois \
    openssh-client \
    openssl \
    socat \
    ncat \
    \
    # ── File & archive tools ──────────────────────────────────────────────────
    unzip \
    zip \
    tar \
    gzip \
    bzip2 \
    xz-utils \
    p7zip-full \
    rsync \
    rclone \
    \
    # ── System monitoring & process tools ────────────────────────────────────
    htop \
    iotop \
    iftop \
    ncdu \
    procps \
    lsof \
    strace \
    ltrace \
    sysstat \
    dstat \
    glances \
    \
    # ── Terminal multiplexers ─────────────────────────────────────────────────
    tmux \
    screen \
    \
    # ── System info / fun ─────────────────────────────────────────────────────
    neofetch \
    fastfetch \
    lshw \
    dmidecode \
    inxi \
    hwinfo \
    \
    # ── Build tools & compilers ───────────────────────────────────────────────
    build-essential \
    gcc \
    g++ \
    make \
    cmake \
    pkg-config \
    autoconf \
    automake \
    libtool \
    patch \
    binutils \
    \
    # ── Dev libraries (commonly needed for compiling packages) ────────────────
    libssl-dev \
    libffi-dev \
    zlib1g-dev \
    libreadline-dev \
    libbz2-dev \
    libsqlite3-dev \
    libncurses5-dev \
    libncursesw5-dev \
    liblzma-dev \
    libgdbm-dev \
    libdb5.3-dev \
    libexpat1-dev \
    libmpdec-dev \
    libxml2-dev \
    libxslt1-dev \
    libcurl4-openssl-dev \
    \
    # ── Python ────────────────────────────────────────────────────────────────
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    python3-setuptools \
    python3-wheel \
    pipx \
    \
    # ── Node.js (from apt — latest LTS via NodeSource added below) ───────────
    nodejs \
    npm \
    \
    # ── Ruby ──────────────────────────────────────────────────────────────────
    ruby \
    ruby-dev \
    rubygems \
    \
    # ── Perl ──────────────────────────────────────────────────────────────────
    perl \
    \
    # ── Go ────────────────────────────────────────────────────────────────────
    golang \
    \
    # ── Rust (via apt, full toolchain via rustup below) ──────────────────────
    rustc \
    cargo \
    \
    # ── Java ──────────────────────────────────────────────────────────────────
    default-jdk \
    default-jre \
    \
    # ── Database clients ──────────────────────────────────────────────────────
    mysql-client \
    postgresql-client \
    redis-tools \
    sqlite3 \
    \
    # ── Web servers (for testing/dev) ─────────────────────────────────────────
    apache2-utils \
    nginx \
    \
    # ── Security & auth ───────────────────────────────────────────────────────
    fail2ban \
    ufw \
    libpam-google-authenticator \
    \
    # ── VCS ───────────────────────────────────────────────────────────────────
    git \
    git-lfs \
    subversion \
    mercurial \
    \
    # ── Misc utilities ────────────────────────────────────────────────────────
    coreutils \
    util-linux \
    hostname \
    psmisc \
    \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── Node.js LTS via NodeSource (replaces the older apt version) ───────────────
RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g npm@latest yarn pnpm pm2 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ── pip: globally useful Python packages ─────────────────────────────────────
RUN pip3 install --break-system-packages --no-cache-dir \
    requests \
    httpx \
    flask \
    fastapi \
    uvicorn \
    gunicorn \
    django \
    sqlalchemy \
    alembic \
    psycopg2-binary \
    pymysql \
    redis \
    celery \
    pydantic \
    rich \
    typer \
    click \
    paramiko \
    cryptography \
    pillow \
    numpy \
    pandas \
    scipy \
    matplotlib \
    pytest \
    black \
    ruff \
    mypy \
    ipython \
    jupyter \
    2>/dev/null || pip3 install --no-cache-dir \
    requests httpx flask fastapi uvicorn gunicorn \
    rich typer click paramiko cryptography 2>/dev/null || true

# Disable systemd services/units that are unnecessary within a container.
RUN systemctl mask systemd-udevd.service \
                   systemd-udevd-kernel.socket \
                   systemd-udevd-control.socket \
                   systemd-modules-load.service \
                   sys-kernel-debug.mount \
                   sys-kernel-tracing.mount

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

# Use fuse-overlayfs storage driver — overlay2 cannot nest on an overlay2 host
# without native-overlay-diff kernel support; fuse-overlayfs always works under --privileged.
RUN mkdir -p /etc/docker && echo '{"storage-driver":"fuse-overlayfs"}' > /etc/docker/daemon.json

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
