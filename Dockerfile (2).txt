# ─── DarkNodes VPS Image ──────────────────────────────────────────────────────
# Base: official Ubuntu 24.04 LTS, configured for systemd as PID 1 inside Docker.
# Requirements: --privileged --cgroupns=host, tmpfs on /run /run/lock /tmp
#
# Access methods baked in:
#   • OpenSSH  — port 22, exposed via host port binding (-p <port>:22)
#   • tmate    — pre-installed; sessions started post-deploy
#   • sshx     — pre-installed at /usr/local/bin/sshx; sessions started post-deploy
#
# Docker CLI uses the host daemon via a mounted socket (/var/run/docker.sock).
# The Docker daemon is NOT installed — only the CLI.

FROM ubuntu:24.04

# Tell systemd it is running inside a container
ENV container=docker
ENV DEBIAN_FRONTEND=noninteractive

# ── Core system + VPS tools ───────────────────────────────────────────────────
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
        tmate \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Docker CLI (host-daemon approach — no daemon inside the container) ────────
# Installs only docker-ce-cli from the official Docker apt repository.
# The container gains full Docker access by mounting /var/run/docker.sock.
RUN curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] \
        https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends docker-ce-cli && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# ── sshx — web-based SSH sessions ─────────────────────────────────────────────
# Installs the sshx binary using the official installer (install sub-command).
# Retries once on network failure (common in CI/build environments).
RUN curl -fsSL https://sshx.io/get | sh -s -- install || \
    (sleep 5 && curl -fsSL https://sshx.io/get | sh -s -- install)

# ── Mask systemd units that fail or are unnecessary inside a container ────────
# rescue/emergency targets are masked because they always fail inside Docker
# and cause systemctl is-system-running to report "maintenance".
RUN systemctl mask \
        rescue.service \
        rescue.target \
        emergency.service \
        emergency.target \
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

# ── SSH setup ─────────────────────────────────────────────────────────────────
RUN mkdir -p /run/sshd /etc/ssh/sshd_config.d && \
    ssh-keygen -A

# Default root password (overridden per-container at deploy time)
RUN echo "root:root" | chpasswd

RUN sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/'     /etc/ssh/sshd_config && \
    sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config

# Enable SSH via systemd so it starts automatically on boot
RUN systemctl enable ssh

# ── Correct stop signal for systemd ──────────────────────────────────────────
STOPSIGNAL SIGRTMIN+3

# ── Boot systemd as PID 1 ────────────────────────────────────────────────────
CMD ["/lib/systemd/systemd"]
