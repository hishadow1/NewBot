# ─── DarkNodes VPS Image ──────────────────────────────────────────────────────
FROM ubuntu:24.04

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
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Docker CLI (host-daemon approach — no daemon inside the container) ────────
RUN curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] \
        https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends docker-ce-cli && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# ── Mask container-hostile systemd units ─────────────────────────────────────
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

# ── SSH setup ─────────────────────────────────────────────────────────────────
RUN mkdir -p /run/sshd /etc/ssh/sshd_config.d && \
    ssh-keygen -A

RUN echo "root:root" | chpasswd

RUN sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/'     /etc/ssh/sshd_config && \
    sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config

RUN systemctl enable ssh

STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
