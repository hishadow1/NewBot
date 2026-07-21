# ─── DarkNodes VPS Image ─────────────────────────────────────────────────────
# standard --privileged Docker without requiring the sysbox runtime.
#
# Bot-specific additions layered on top:
#   • openssh-server   — SSH access on port 22 (exposed via host port binding)
#   • fuse-overlayfs   — storage driver for nested Docker without root overlay
#   • tmate            — terminal sharing sessions
#   • sshx             — web-based terminal sessions
#
# Runtime requirements:
#   --privileged  --cgroupns=host
#   --tmpfs /run:exec,mode=755,size=256m
#   --tmpfs /run/lock:size=64m
#   --tmpfs /tmp:exec,size=512m
#   Named volumes for /var/lib/docker  /home  /root  /opt  (persistence)
# ─────────────────────────────────────────────────────────────────────────────

ARG UBUNTU_VERSION="24.04"
FROM ubuntu:${UBUNTU_VERSION}

ARG UBUNTU_VERSION
ENV DOCKER_VERSION=27.3.1 \
    DOCKER_COMPOSE_VERSION=v2.29.7 \
    BUILDX_VERSION=v0.17.1 \
    DEBIAN_FRONTEND=noninteractive \
    container=docker

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 1 — systemd + base system packages
# Identical to cruizba/ubuntu-dind structure; extended with openssh-server and
# fuse-overlayfs for VPS access and nested-Docker storage.
# ─────────────────────────────────────────────────────────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        systemd \
        systemd-sysv \
        libsystemd0 \
        ca-certificates \
        dbus \
        iptables \
        iproute2 \
        kmod \
        locales \
        sudo \
        udev \
        curl \
        wget \
        openssh-server \
        fuse3 \
        fuse-overlayfs && \
    \
    # Prevent journald from reading kernel messages from /dev/kmsg
    echo "ReadKMsg=no" >> /etc/systemd/journald.conf && \
    \
    # Housekeeping
    apt-get clean -y && \
    rm -rf \
       /var/cache/debconf/* \
       /var/lib/apt/lists/* \
       /var/log/* \
       /tmp/* \
       /var/tmp/* \
       /usr/share/doc/* \
       /usr/share/man/* \
       /usr/share/local/* && \
    \
    # Create default 'admin/admin' user
    useradd --create-home --shell /bin/bash admin && \
    echo "admin:admin" | chpasswd && \
    usermod -aG sudo admin && \
    printf 'admin ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/admin && \
    chmod 440 /etc/sudoers.d/admin

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 2 — mask systemd units unnecessary inside a container
# Source: cruizba/ubuntu-dind + additional units from nestybox recommendations
# ─────────────────────────────────────────────────────────────────────────────
RUN systemctl mask \
        systemd-udevd.service \
        systemd-udevd-kernel.socket \
        systemd-udevd-control.socket \
        systemd-modules-load.service \
        sys-kernel-debug.mount \
        sys-kernel-tracing.mount \
        systemd-networkd-wait-online.service \
        systemd-logind.service \
        getty.service \
        getty.target \
        2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 3 — iptables backend (follows cruizba/ubuntu-dind logic)
#  - Ubuntu 22.04/24.04: legacy backend
#  - Ubuntu 26.04+: nft backend (iptables-legacy can't init nat table in 1.8.11)
# ─────────────────────────────────────────────────────────────────────────────
RUN set -eux; \
    case "${UBUNTU_VERSION}" in \
        20.04) ;; \
        22.04|24.04) update-alternatives --set iptables /usr/sbin/iptables-legacy ;; \
        *) update-alternatives --set iptables /usr/sbin/iptables-nft ;; \
    esac

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 4 — Docker Engine (daemon + CLI)
# Uses the official get.docker.com installer, same as cruizba/ubuntu-dind.
# ─────────────────────────────────────────────────────────────────────────────
RUN curl -fsSL https://get.docker.com -o /tmp/get-docker.sh \
    && sh /tmp/get-docker.sh --version ${DOCKER_VERSION} \
    && usermod -a -G docker admin \
    && rm /tmp/get-docker.sh \
    && docker --version

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 5 — Docker Buildx
# ─────────────────────────────────────────────────────────────────────────────
RUN set -eux; \
    arch="$(uname -m)"; \
    case "$arch" in \
        x86_64)  buildx_arch='linux-amd64'  ;; \
        armhf)   buildx_arch='linux-arm-v6' ;; \
        armv7)   buildx_arch='linux-arm-v7' ;; \
        aarch64) buildx_arch='linux-arm64'  ;; \
        *) echo >&2 "error: unsupported architecture ($arch)"; exit 1 ;; \
    esac && \
    wget -qO /tmp/docker-buildx \
        "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.${buildx_arch}" && \
    mkdir -p /usr/local/lib/docker/cli-plugins && \
    install -m 755 /tmp/docker-buildx /usr/local/lib/docker/cli-plugins/docker-buildx && \
    rm /tmp/docker-buildx && \
    docker buildx version

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 6 — Docker Compose
# ─────────────────────────────────────────────────────────────────────────────
RUN curl --retry 5 --retry-max-time 40 -fsSL \
    "https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_VERSION}/docker-compose-$(uname -s)-$(uname -m)" \
    -o /usr/local/bin/docker-compose \
    && chmod 755 /usr/local/bin/docker-compose \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && ln -sf /usr/local/bin/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose \
    && docker compose version

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 7 — Docker daemon config
# fuse-overlayfs: works in --privileged containers without host kernel overlay
# support; more portable than overlay2 for nested Docker.
# ─────────────────────────────────────────────────────────────────────────────
RUN mkdir -p /etc/docker && \
    printf '{\n  "storage-driver": "fuse-overlayfs",\n  "dns": ["8.8.8.8", "1.1.1.1"],\n  "log-driver": "json-file",\n  "log-opts": {"max-size": "10m", "max-file": "3"}\n}\n' \
    > /etc/docker/daemon.json

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 8 — SSH setup
# ─────────────────────────────────────────────────────────────────────────────
RUN mkdir -p /run/sshd /etc/ssh/sshd_config.d && \
    ssh-keygen -A && \
    echo "root:root" | chpasswd && \
    sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/'          /etc/ssh/sshd_config && \
    sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config && \
    systemctl enable ssh && \
    systemctl enable docker

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 9 — tmate (terminal sharing; static binary from GitHub releases)
# ─────────────────────────────────────────────────────────────────────────────
RUN arch="$(uname -m)" && \
    if [ "$arch" = "x86_64" ];   then ta="amd64"   ; \
    elif [ "$arch" = "aarch64" ]; then ta="arm64v8" ; \
    elif [ "$arch" = "armv7l" ];  then ta="arm32v7" ; \
    else echo "unsupported arch for tmate: $arch" >&2; exit 1; fi && \
    curl -fsSL \
        "https://github.com/tmate-io/tmate/releases/download/2.4.0/tmate-2.4.0-static-linux-${ta}.tar.xz" \
        -o /tmp/tmate.tar.xz && \
    tar -xf /tmp/tmate.tar.xz -C /tmp && \
    install -m 755 "/tmp/tmate-2.4.0-static-linux-${ta}/tmate" /usr/local/bin/tmate && \
    rm -rf /tmp/tmate* && \
    tmate -V

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 10 — sshx (web-based terminal sessions)
# ─────────────────────────────────────────────────────────────────────────────
RUN curl -sSf https://sshx.io/get | sh -s -- install \
    && sshx --version 2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
# Correct stop signal for systemd containers (as per cruizba/ubuntu-dind)
STOPSIGNAL SIGRTMIN+3

# Named volume for Docker daemon data (avoids storage-driver issues)
VOLUME /var/lib/docker

# systemd as PID 1 — the only correct init for a systemd container
ENTRYPOINT ["/sbin/init", "--log-level=err"]
