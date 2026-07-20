# Minimal DarkNodes VPS — systemd + Docker-in-Docker, fully functional.
# Users install any tool with:  apt install <package>   or   install <package>
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
    NEEDRESTART_MODE=a \
    NEEDRESTART_SUSPEND=1 \
    DEBIAN_PRIORITY=critical

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 1 — repos + packages
# Fastest cached layer: only rebuilt when the package list changes.
# ZERO inline # comments inside apt-get install — /bin/sh (dash) on Ubuntu
# passes them as literal package names → exit code 1.
# ─────────────────────────────────────────────────────────────────────────────
RUN if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then \
        sed -i \
            's/^Components: main$/Components: main restricted universe multiverse/' \
            /etc/apt/sources.list.d/ubuntu.sources ; \
    else \
        sed -i \
            -e 's/^# \(deb.*universe\)/\1/' \
            -e 's/^# \(deb.*multiverse\)/\1/' \
            -e 's/^# \(deb.*restricted\)/\1/' \
            /etc/apt/sources.list ; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        systemd \
        systemd-sysv \
        libsystemd0 \
        dbus \
        dbus-user-session \
        udev \
        kmod \
        iproute2 \
        iptables \
        fuse3 \
        fuse-overlayfs \
        curl \
        wget \
        ca-certificates \
        gnupg \
        locales \
        tzdata \
        sudo \
        passwd \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 2 — configuration
# No downloads. Pure file writes + system config. Cached independently.
# /etc/hosts and /etc/resolv.conf are Docker bind-mounts during build and
# are READ-ONLY — never write to them here. resolv.conf is a symlink so
# we can rm it and replace it; hosts is a real bind-mount so we skip it.
# ─────────────────────────────────────────────────────────────────────────────
RUN printf 'APT::Acquire::Retries "5";\n'         > /etc/apt/apt.conf.d/80retries \
    && printf 'APT::Acquire::http::Timeout "30";\n' >> /etc/apt/apt.conf.d/80retries \
    && printf 'DPkg::Options:: "--force-confdef";\nDPkg::Options:: "--force-confold";\n' \
        > /etc/apt/apt.conf.d/90dpkg \
    && printf 'APT::Install-Recommends "false";\n' > /etc/apt/apt.conf.d/91norecommends \
    && printf 'APT::Get::Assume-Yes "true";\n'     > /etc/apt/apt.conf.d/92assumeyes \
    \
    && locale-gen en_US.UTF-8 \
    && update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 \
    && ln -snf /usr/share/zoneinfo/UTC /etc/localtime \
    && echo UTC > /etc/timezone \
    \
    && printf 'LANG=en_US.UTF-8\nLC_ALL=en_US.UTF-8\nLANGUAGE=en_US:en\nDEBIAN_FRONTEND=noninteractive\nTZ=UTC\n' \
        > /etc/environment \
    \
    && useradd --create-home --shell /bin/bash admin \
    && echo "admin:admin" | chpasswd \
    && usermod -aG sudo admin \
    && printf 'admin ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/admin \
    && chmod 440 /etc/sudoers.d/admin \
    && printf 'root ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/root \
    && chmod 440 /etc/sudoers.d/root \
    \
    && echo "ReadKMsg=no" >> /etc/systemd/journald.conf \
    \
    && systemctl mask \
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
        2>/dev/null || true \
    \
    && if [ "${UBUNTU_VERSION}" = "20.04" ] || [ "${UBUNTU_VERSION}" = "22.04" ] || [ "${UBUNTU_VERSION}" = "24.04" ]; then \
        update-alternatives --set iptables /usr/sbin/iptables-legacy 2>/dev/null || true ; \
    else \
        update-alternatives --set iptables /usr/sbin/iptables-nft    2>/dev/null || true ; \
    fi \
    \
    && printf '\n# DarkNodes VPS\nexport DEBIAN_FRONTEND=noninteractive\nexport LANG=en_US.UTF-8\nexport LC_ALL=en_US.UTF-8\nalias install="apt-get update && apt-get install -y"\nalias update="apt-get update && apt-get upgrade -y"\nalias ports="ss -tulpn"\nalias myip="curl -s ifconfig.me"\n' \
        | tee -a /root/.bashrc /home/admin/.bashrc /etc/skel/.bashrc > /dev/null \
    \
    && rm -f /etc/resolv.conf \
    && printf 'nameserver 8.8.8.8\nnameserver 1.1.1.1\nnameserver 8.8.4.4\noptions edns0 trust-ad\n' \
        > /etc/resolv.conf

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 3 — Docker engine
# ─────────────────────────────────────────────────────────────────────────────
RUN curl -fsSL https://get.docker.com -o /tmp/get-docker.sh \
    && sh /tmp/get-docker.sh --version ${DOCKER_VERSION} \
    && usermod -a -G docker admin \
    && rm /tmp/get-docker.sh \
    && docker --version

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 4 — Docker Buildx
# ─────────────────────────────────────────────────────────────────────────────
RUN arch="$(uname -m)" \
    && if [ "$arch" = "x86_64" ];  then ba="linux-amd64"  ; \
    elif [ "$arch" = "aarch64" ];  then ba="linux-arm64"  ; \
    elif [ "$arch" = "armv7l" ];   then ba="linux-arm-v7" ; \
    elif [ "$arch" = "armhf" ];    then ba="linux-arm-v6" ; \
    else echo "unsupported arch: $arch" >&2 ; exit 1 ; fi \
    && wget -qO /tmp/docker-buildx \
        "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.${ba}" \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && install -m 755 /tmp/docker-buildx /usr/local/lib/docker/cli-plugins/docker-buildx \
    && rm /tmp/docker-buildx \
    && docker buildx version

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 5 — Docker Compose
# ─────────────────────────────────────────────────────────────────────────────
RUN curl --retry 5 --retry-max-time 40 -fsSL \
    "https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_VERSION}/docker-compose-$(uname -s)-$(uname -m)" \
    -o /usr/local/bin/docker-compose \
    && chmod 755 /usr/local/bin/docker-compose \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && ln -sf /usr/local/bin/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose \
    && docker compose version

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 6 — Docker daemon config
# ─────────────────────────────────────────────────────────────────────────────
RUN mkdir -p /etc/docker \
    && printf '{\n  "storage-driver": "fuse-overlayfs",\n  "dns": ["8.8.8.8", "1.1.1.1", "8.8.4.4"],\n  "log-driver": "json-file",\n  "log-opts": {"max-size": "10m", "max-file": "3"},\n  "default-ulimits": {"nofile": {"Name": "nofile", "Hard": 65536, "Soft": 65536}}\n}\n' \
        > /etc/docker/daemon.json

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 7 — tmate
# ─────────────────────────────────────────────────────────────────────────────
RUN arch="$(uname -m)" \
    && if [ "$arch" = "x86_64" ];  then ta="amd64"   ; \
    elif [ "$arch" = "aarch64" ];  then ta="arm64v8" ; \
    elif [ "$arch" = "armv7l" ];   then ta="arm32v7" ; \
    else echo "unsupported arch for tmate: $arch" >&2 ; exit 1 ; fi \
    && curl -fsSL \
        "https://github.com/tmate-io/tmate/releases/download/2.4.0/tmate-2.4.0-static-linux-${ta}.tar.xz" \
        -o /tmp/tmate.tar.xz \
    && tar -xf /tmp/tmate.tar.xz -C /tmp \
    && install -m 755 "/tmp/tmate-2.4.0-static-linux-${ta}/tmate" /usr/local/bin/tmate \
    && rm -rf /tmp/tmate* \
    && tmate -V

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 8 — sshx
# ─────────────────────────────────────────────────────────────────────────────
RUN curl -sSf https://sshx.io/get | sh -s -- -y \
    && sshx --version 2>/dev/null || true

STOPSIGNAL SIGRTMIN+3
VOLUME /var/lib/docker
ENTRYPOINT ["/sbin/init", "--log-level=err"]
