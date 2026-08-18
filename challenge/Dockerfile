FROM ubuntu:25.04

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -qy gcc gcc-multilib make xinetd && \
    rm -rf /var/lib/apt/lists/*

RUN useradd -m chal

# Bake the challenge into the image so it needs no volume mounts.
# (The docker-compose.yml still mounts ./share for local development.)
COPY share/ /home/chal/
COPY xinetd /etc/xinetd.d/chal

RUN cd /home/chal && make && \
    chown -R root:root /home/chal && \
    chmod -R 755 /home/chal

EXPOSE 41240

CMD ["/usr/sbin/xinetd", "-dontfork"]
