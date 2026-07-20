# Minimal DarkNodes VPS — systemd + Docker-in-Docker, fully functional.
# Users install any tool they need with: apt install <package>
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

# ── Single apt pass ───────────────────────────────────────────────────────────
# Enable universe/multiverse via sed (no software-properties-common needed).
# One apt-get update, one install, one clean — fastest possible base layer.
RUN set -eux; \
    \
    # Enable universe + multiverse + restricted without add-apt-repository
    if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then \
        sed -i 's/^Components: main$/Components: main restricted universe multiverse/' \
            /etc/apt/sources.list.d/ubuntu.sources; \
    else \
        sed -i \
            -e 's/^# \(deb.*universe\)/\1/' \
            -e 's/^# \(deb.*multiverse\)/\1/' \
            -e 's/^# \(deb.*restricted\)/\1/' \
            /etc/apt/sources.list; \
    fi; \
    \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        \
        # systemd + bus + udev — required for full systemd operation inside the container
        systemd systemd-sysv libsystemd0 \
        dbus dbus-user-session \
        udev \
        \
        # cgroup + kernel helpers — required by systemd and Docker
        kmod \
        iproute2 \
        iptables \
        \
        # Docker storage driver deps (fuse-overlayfs for DinD without privileged overlay2)
        fuse3 fuse-overlayfs \
        \
        # Runtime essentials — needed to download Docker, tmate, sshx, and handle DNS
        curl wget ca-certificates gnupg \
        \
        # Locale + timezone
        locales tzdata \
        \
        # sudo + user management
        sudo passwd \
    && \
    \
    # apt behaviour config — makes `apt install <anything>` silent and non-interactive
    echo 'APT::Acquire::Retries "5";'         > /etc/apt/apt.conf.d/80retries && \
    echo 'APT::Acquire::http::Timeout "30";' >> /etc/apt/apt.conf.d/80retries && \
    echo 'DPkg::Options:: "--force-confdef";' > /etc/apt/apt.conf.d/90dpkg    && \
    echo 'DPkg::Options:: "--force-confold";'>> /etc/apt/apt.conf.d/90dpkg    && \
    echo 'APT::Install-Recommends "false";'   > /etc/apt/apt.conf.d/91norecommends && \
    echo 'APT::Get::Assume-Yes "true";'       > /etc/apt/apt.conf.d/92assumeyes    && \
    \
    # Locale + timezone
    locale-gen en_US.UTF-8 && update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 && \
    ln -snf /usr/share/zoneinfo/UTC /etc/localtime && echo UTC > /etc/timezone && \
    \
    # Admin user
    useradd --create-home --shell /bin/bash admin && \
    echo "admin:admin" | chpasswd && \
    usermod -aG sudo admin && \
    echo "admin ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/admin && chmod 440 /etc/sudoers.d/admin && \
    echo "root  ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/root  && chmod 440 /etc/sudoers.d/root && \
    \
    # Silence systemd journal read-kernel-message spam inside container
    echo "ReadKMsg=no" >> /etc/systemd/journald.conf && \
    \
    # Mask units that don't work / aren't needed in a container
    systemctl mask \
        systemd-udevd.service \
        systemd-udevd-kernel.socket \
        systemd-udevd-control.socket \
        systemd-modules-load.service \
        sys-kernel-debug.mount \
        sys-kernel-tracing.mount \
        systemd-networkd-wait-online.service \
        systemd-logind.service \
        getty.service \
        getty.target && \
    \
    # iptables backend — nft on modern Ubuntu, legacy on older
    case "${UBUNTU_VERSION}" in \
        20.04|22.04|24.04) update-alternatives --set iptables /usr/sbin/iptables-legacy ;; \
        *) update-alternatives --set iptables /usr/sbin/iptables-nft ;; \
    esac && \
    \
    # Bash aliases — `install <pkg>` shortcut so users never need to think about apt-get update
    printf '\n# DarkNodes VPS\nexport DEBIAN_FRONTEND=noninteractive\nexport LANG=en_US.UTF-8\nexport LC_ALL=en_US.UTF-8\nalias install="apt-get update && apt-get install -y"\nalias update="apt-get update && apt-get upgrade -y"\nalias ports="ss -tulpn"\nalias myip="curl -s ifconfig.me"\n' \
        | tee -a /root/.bashrc /home/admin/.bashrc /etc/skel/.bashrc > /dev/null && \
    \
    # Environment
    printf 'LANG=en_US.UTF-8\nLC_ALL=en_US.UTF-8\nLANGUAGE=en_US:en\nDEBIAN_FRONTEND=noninteractive\nTZ=UTC\n' \
        > /etc/environment && \
    \
    # DNS — remove the build-time read-only symlink, write a real file
    rm -f /etc/resolv.conf && \
    printf 'nameserver 8.8.8.8\nnameserver 1.1.1.1\nnameserver 8.8.4.4\noptions edns0 trust-ad\n' \
        > /etc/resolv.conf && \
    printf '127.0.0.1\tlocalhost\n127.0.1.1\tDarkNodes-VPS\n::1\tlocalhost ip6-localhost ip6-loopback\n' \
        > /etc/hosts && \
    \
    apt-get clean && \
    rm -rf /var/cache/debconf/* /var/lib/apt/lists/* /var/log/* /tmp/* /var/tmp/* \
           /usr/share/doc/* /usr/share/man/*

# ── Docker ────────────────────────────────────────────────────────────────────
RUN curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && \
    sh /tmp/get-docker.sh --version ${DOCKER_VERSION} && \
    usermod -a -G docker admin && \
    rm /tmp/get-docker.sh && \
    docker --version

# ── Docker Buildx ─────────────────────────────────────────────────────────────
RUN set -eux; \
    arch="$(uname -m)"; \
    case "$arch" in \
        x86_64)  ba='linux-amd64'  ;; \
        armhf)   ba='linux-arm-v6' ;; \
        armv7)   ba='linux-arm-v7' ;; \
        aarch64) ba='linux-arm64'  ;; \
        *) echo >&2 "unsupported arch: $arch"; exit 1 ;; \
    esac; \
    wget -qO /tmp/docker-buildx \
        "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.${ba}" && \
    mkdir -p /usr/local/lib/docker/cli-plugins && \
    install -m 755 /tmp/docker-buildx /usr/local/lib/docker/cli-plugins/docker-buildx && \
    rm /tmp/docker-buildx && \
    docker buildx version

# ── Docker Compose ────────────────────────────────────────────────────────────
RUN curl --retry 5 --retry-max-time 40 -fsSL \
    "https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_VERSION}/docker-compose-$(uname -s)-$(uname -m)" \
    -o /usr/local/bin/docker-compose && \
    chmod 755 /usr/local/bin/docker-compose && \
    mkdir -p /usr/local/lib/docker/cli-plugins && \
    ln -sf /usr/local/bin/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose && \
    docker compose version

# ── Docker daemon config ──────────────────────────────────────────────────────
RUN mkdir -p /etc/docker && printf '{\n\
  "storage-driver": "fuse-overlayfs",\n\
  "dns": ["8.8.8.8", "1.1.1.1", "8.8.4.4"],\n\
  "log-driver": "json-file",\n\
  "log-opts": {"max-size": "10m", "max-file": "3"},\n\
  "default-ulimits": {"nofile": {"Name": "nofile", "Hard": 65536, "Soft": 65536}}\n\
}\n' > /etc/docker/daemon.json

# ── tmate ─────────────────────────────────────────────────────────────────────
RUN set -eux; \
    arch="$(uname -m)"; \
    case "$arch" in \
        x86_64)  ta='amd64'   ;; \
        aarch64) ta='arm64v8' ;; \
        armv7l)  ta='arm32v7' ;; \
        *) echo >&2 "unsupported arch for tmate: $arch"; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/tmate-io/tmate/releases/download/2.4.0/tmate-2.4.0-static-linux-${ta}.tar.xz" \
        -o /tmp/tmate.tar.xz && \
    tar -xf /tmp/tmate.tar.xz -C /tmp && \
    install -m 755 /tmp/tmate-2.4.0-static-linux-${ta}/tmate /usr/local/bin/tmate && \
    rm -rf /tmp/tmate* && \
    tmate -V

# ── sshx ──────────────────────────────────────────────────────────────────────
RUN curl -sSf https://sshx.io/get | sh -s -- -y && \
    sshx --version 2>/dev/null || true

STOPSIGNAL SIGRTMIN+3
VOLUME /var/lib/docker
ENTRYPOINT ["/sbin/init", "--log-level=err"]
