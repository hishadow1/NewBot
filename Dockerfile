FROM ubuntu:24.04

ENV container=docker
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y \
    systemd \
    systemd-sysv \
    dbus \
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
    software-properties-common \
    iproute2 \
    net-tools \
    docker.io && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /run/sshd
RUN ssh-keygen -A

RUN echo "root:root" | chpasswd

RUN sed -i 's/^#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config && \
    sed -i 's/^#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config

RUN systemctl enable ssh || true

STOPSIGNAL SIGRTMIN+3

VOLUME ["/sys/fs/cgroup"]

CMD ["/sbin/init"]
