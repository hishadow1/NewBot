# Minimal DarkNodes VPS image — systemd + Docker-in-Docker.
# No extra packages pre-installed. Users run `apt install <anything>` or
# `install <package>` (alias) to get whatever tools they need.
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

# ── Systemd + base ────────────────────────────────────────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        systemd systemd-sysv libsystemd0 \
        ca-certificates dbus \
        fuse3 fuse-overlayfs \
        iptables iproute2 kmod \
        locales sudo udev \
        curl wget && \
    echo "ReadKMsg=no" >> /etc/systemd/journald.conf && \
    apt-get clean && \
    rm -rf /var/cache/debconf/* /var/lib/apt/lists/* /var/log/* /tmp/* /var/tmp/* \
           /usr/share/doc/* /usr/share/man/* /usr/share/local/* && \
    locale-gen en_US.UTF-8 && update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 && \
    printf 'LANG=en_US.UTF-8\nLC_ALL=en_US.UTF-8\nLANGUAGE=en_US:en\nDEBIAN_FRONTEND=noninteractive\nTZ=UTC\n' \
        > /etc/environment && \
    ln -snf /usr/share/zoneinfo/UTC /etc/localtime && echo UTC > /etc/timezone && \
    useradd --create-home --shell /bin/bash admin && \
    echo "admin:admin" | chpasswd && \
    usermod -aG sudo admin && \
    echo "admin ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/admin && chmod 440 /etc/sudoers.d/admin && \
    echo "root  ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/root  && chmod 440 /etc/sudoers.d/root

# ── apt config: retries, non-interactive, no recommends ──────────────────────
# This is what makes `apt install <anything>` just work inside the VPS.
RUN echo 'APT::Acquire::Retries "5";'           > /etc/apt/apt.conf.d/80retries && \
    echo 'APT::Acquire::http::Timeout "30";'   >> /etc/apt/apt.conf.d/80retries && \
    echo 'APT::Acquire::https::Timeout "30";'  >> /etc/apt/apt.conf.d/80retries && \
    echo 'DPkg::Options:: "--force-confdef";'   > /etc/apt/apt.conf.d/90dpkg    && \
    echo 'DPkg::Options:: "--force-confold";'  >> /etc/apt/apt.conf.d/90dpkg    && \
    echo 'APT::Install-Recommends "false";'     > /etc/apt/apt.conf.d/91norecommends && \
    echo 'APT::Get::Assume-Yes "true";'         > /etc/apt/apt.conf.d/92assumeyes && \
    echo 'APT::Get::Show-Upgraded "false";'    >> /etc/apt/apt.conf.d/92assumeyes

# ── Enable universe + multiverse + restricted so every package is available ───
RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common apt-transport-https gnupg lsb-release && \
    add-apt-repository -y universe && \
    add-apt-repository -y multiverse && \
    add-apt-repository -y restricted && \
    if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then \
        sed -i 's/^Components: main$/Components: main restricted universe multiverse/' \
            /etc/apt/sources.list.d/ubuntu.sources; \
    fi && \
    apt-get update && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ── Bash aliases — `install` shortcut so users can type `install python3` ─────
RUN printf '\n# DarkNodes VPS\nexport DEBIAN_FRONTEND=noninteractive\nalias install="apt-get update && apt-get install -y"\nalias update="apt-get update && apt-get upgrade -y"\nalias ports="ss -tulpn"\nalias myip="curl -s ifconfig.me"\n' \
    | tee -a /root/.bashrc /home/admin/.bashrc /etc/skel/.bashrc > /dev/null

# ── Disable unnecessary systemd units ────────────────────────────────────────
RUN systemctl mask \
        systemd-udevd.service \
        systemd-udevd-kernel.socket \
        systemd-udevd-control.socket \
        systemd-modules-load.service \
        sys-kernel-debug.mount \
        sys-kernel-tracing.mount \
        systemd-resolved.service \
        systemd-networkd-wait-online.service \
        systemd-logind.service \
        getty.service \
        getty.target

# ── iptables backend ──────────────────────────────────────────────────────────
RUN set -eux; \
    case "${UBUNTU_VERSION}" in \
        20.04) ;; \
        22.04|24.04) update-alternatives --set iptables /usr/sbin/iptables-legacy ;; \
        *) update-alternatives --set iptables /usr/sbin/iptables-nft ;; \
    esac

# ── Docker ────────────────────────────────────────────────────────────────────
RUN curl -fsSL https://get.docker.com -o get-docker.sh && \
    sh get-docker.sh --version ${DOCKER_VERSION} && \
    usermod -a -G docker admin && \
    rm get-docker.sh && \
    docker --version

# ── Docker Buildx ─────────────────────────────────────────────────────────────
RUN set -eux; \
    arch="$(uname -m)"; \
    case "$arch" in \
        x86_64)  buildx_arch='linux-amd64'  ;; \
        armhf)   buildx_arch='linux-arm-v6' ;; \
        armv7)   buildx_arch='linux-arm-v7' ;; \
        aarch64) buildx_arch='linux-arm64'  ;; \
        *) echo >&2 "unsupported arch: $arch"; exit 1 ;; \
    esac && \
    wget -O docker-buildx \
        "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.${buildx_arch}" && \
    mkdir -p /usr/local/lib/docker/cli-plugins && \
    chmod +x docker-buildx && \
    mv docker-buildx /usr/local/lib/docker/cli-plugins/docker-buildx && \
    docker buildx version

# ── Docker Compose ────────────────────────────────────────────────────────────
RUN curl --retry 5 --retry-max-time 40 \
    -L "https://github.com/docker/compose/releases/download/$DOCKER_COMPOSE_VERSION/docker-compose-$(uname -s)-$(uname -m)" \
    -o /usr/local/bin/docker-compose && \
    chmod 755 /usr/local/bin/docker-compose && \
    mkdir -p /usr/local/lib/docker/cli-plugins && \
    ln -s /usr/local/bin/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose

STOPSIGNAL SIGRTMIN+3
VOLUME /var/lib/docker

# ── Docker daemon config (fuse-overlayfs for DinD + DNS) ─────────────────────
RUN mkdir -p /etc/docker && \
    printf '{\n  "storage-driver": "fuse-overlayfs",\n  "dns": ["8.8.8.8", "1.1.1.1", "8.8.4.4"],\n  "log-driver": "json-file",\n  "log-opts": {"max-size": "10m", "max-file": "3"},\n  "default-ulimits": {"nofile": {"Name": "nofile", "Hard": 65536, "Soft": 65536}}\n}\n' \
    > /etc/docker/daemon.json

# ── DNS + hosts ───────────────────────────────────────────────────────────────
RUN printf 'nameserver 8.8.8.8\nnameserver 1.1.1.1\nnameserver 8.8.4.4\noptions edns0 trust-ad\n' \
        > /etc/resolv.conf.default && \
    cp /etc/resolv.conf.default /etc/resolv.conf && \
    printf '127.0.0.1\tlocalhost\n127.0.1.1\tDarkNodes-VPS\n::1\tlocalhost ip6-localhost ip6-loopback\n' \
        > /etc/hosts

# ── tmate ─────────────────────────────────────────────────────────────────────
RUN set -eux; \
    arch="$(uname -m)"; \
    case "$arch" in \
        x86_64)  tmate_arch='amd64'   ;; \
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

# ── sshx ──────────────────────────────────────────────────────────────────────
RUN curl -sSf https://sshx.io/get | sh -s -- -y && \
    sshx --version 2>/dev/null || true

ENTRYPOINT [ "/sbin/init", "--log-level=err" ]
