# ─── DarkNodes VPS Image — Full Docker-in-Docker ────────────────────────────
# Base: Ubuntu 24.04 LTS, systemd as PID 1, Docker Engine runs INSIDE the
# container (Docker-in-Docker / DinD).  Each VPS has its own isolated daemon.
#
# Runtime requirements:
#   --privileged  --cgroupns=host
#   --tmpfs /run:exec,mode=755,size=256m
#   --tmpfs /run/lock:size=64m
#   --tmpfs /tmp:exec,size=512m
#   Named volumes for /var/lib/docker  /home  /root  /opt  (persistence)
#
# Access methods baked in:
#   • OpenSSH  — port 22, exposed via host port binding (-p <port>:22)
#   • tmate    — pre-installed; sessions started post-deploy
#   • sshx     — pre-installed at /usr/local/bin/sshx; sessions started post-deploy

FROM ubuntu:24.04

# Tell systemd it is running inside a container
ENV container=docker
ENV DEBIAN_FRONTEND=noninteractive

# ── Enable universe repo (required for fuse-overlayfs and other extras) ───────
# Ubuntu 24.04 Docker images ship with only "main" enabled by default.
RUN sed -i 's/^Components: main$/Components: main restricted universe multiverse/' \
        /etc/apt/sources.list.d/ubuntu.sources

# ── Core system + VPS tools ───────────────────────────────────────────────────
# NOTE: tmate is NOT in Ubuntu 24.04 repos — installed from binary below.
#       ip6tables is bundled inside the iptables package (no separate pkg).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        systemd \
        systemd-sysv \
        dbus \
        dbus-user-session \
        openssh-server \
        sudo \
        curl \
        wget \
        nano \
        vim \
        git \
        unzip \
        zip \
        python3 \
        python3-pip \
        ca-certificates \
        gnupg \
        lsb-release \
        software-properties-common \
        iproute2 \
        net-tools \
        procps \
        build-essential \
        iptables \
        iptables-persistent \
        kmod \
        fuse-overlayfs \
        pigz \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── tmate — static binary from GitHub (not in Ubuntu 24.04 repos) ─────────────
RUN set -eux; \
    TMATE_VER="2.4.0"; \
    curl -fsSL \
        "https://github.com/tmate-io/tmate/releases/download/${TMATE_VER}/tmate-${TMATE_VER}-static-linux-amd64.tar.xz" \
        -o /tmp/tmate.tar.xz && \
    tar -xJf /tmp/tmate.tar.xz -C /tmp/ && \
    mv "/tmp/tmate-${TMATE_VER}-static-linux-amd64/tmate" /usr/local/bin/tmate && \
    chmod +x /usr/local/bin/tmate && \
    rm -rf /tmp/tmate* && \
    tmate -V

# ── Node.js LTS ───────────────────────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# ── Docker Engine (full daemon + CLI + Compose plugin) ────────────────────────
# Installs docker-ce (daemon), docker-ce-cli, containerd.io, and the
# docker-compose-plugin from the official Docker apt repository.
# The daemon runs inside each VPS container — no host socket is mounted.
RUN curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] \
        https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        docker-ce \
        docker-ce-cli \
        containerd.io \
        docker-buildx-plugin \
        docker-compose-plugin && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# ── Docker daemon config for DinD ─────────────────────────────────────────────
# overlay2 is the preferred storage driver; works inside --privileged containers
# with a persistent /var/lib/docker volume on the host.
# iptables and ip-forward are required for Docker networking inside the VPS.
RUN mkdir -p /etc/docker && \
    printf '{\n  "storage-driver": "fuse-overlayfs",\n  "log-driver": "json-file",\n  "log-opts": { "max-size": "10m", "max-file": "3" },\n  "iptables": true,\n  "ip-forward": true\n}\n' \
    > /etc/docker/daemon.json

# ── sshx — web-based SSH sessions ─────────────────────────────────────────────
# Installs the sshx binary from the official release script.
# Retries once on network failure (common in CI/build environments).
RUN curl -fsSL https://sshx.io/get | sh -s -- install || \
    (sleep 5 && curl -fsSL https://sshx.io/get | sh -s -- install)

# ── Mask systemd units that fail or are unnecessary inside a container ────────
RUN systemctl mask \
        dev-hugepages.mount \
        sys-fs-fuse-connections.mount \
        sys-kernel-config.mount \
        sys-kernel-debug.mount \
        sys-kernel-tracing.mount \
        display-manager.service \
        getty@.service \
        getty.target \
        graphical.target \
        kmod-static-nodes.service \
        modprobe@.service \
        proc-sys-fs-binfmt_misc.automount \
        proc-sys-fs-binfmt_misc.mount \
        systemd-binfmt.service \
        systemd-firstboot.service \
        systemd-hwdb-update.service \
        systemd-modules-load.service \
        systemd-remount-fs.service \
        systemd-udev-trigger.service \
        systemd-udevd.service \
        udev.service

# ── Enable services ────────────────────────────────────────────────────────────
# docker.service starts the Docker daemon at boot inside each VPS container.
RUN systemctl enable docker && \
    systemctl enable ssh

# ── SSH setup ─────────────────────────────────────────────────────────────────
RUN mkdir -p /run/sshd /etc/ssh/sshd_config.d && \
    ssh-keygen -A

# Default root password (overridden per-container at deploy time)
RUN echo "root:root" | chpasswd

RUN sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/'     /etc/ssh/sshd_config && \
    sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config

# ── Correct stop signal for systemd ──────────────────────────────────────────
STOPSIGNAL SIGRTMIN+3

# ── Boot systemd as PID 1 ────────────────────────────────────────────────────
CMD ["/lib/systemd/systemd"]
