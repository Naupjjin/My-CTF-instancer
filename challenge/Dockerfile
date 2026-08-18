FROM ubuntu:25.04

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -qy gcc gcc-multilib make xinetd && \
    rm -rf /var/lib/apt/lists/*

RUN useradd -m chal

# Bake the challenge into the image so it needs no volume mounts.
# (The docker-compose.yml still mounts ./share for local development.)
COPY share/ /home/chal/
COPY xinetd.template /etc/xinetd.d/chal.template
COPY entrypoint.sh /entrypoint.sh

RUN cd /home/chal && make && \
    chown -R root:root /home/chal && \
    chmod -R 755 /home/chal && \
    chmod 755 /entrypoint.sh

# Which port to listen on is the instancer's call: it hands every instance its
# own. This is only the fallback for running the image by hand.
ENV CHAL_PORT=41240

CMD ["/entrypoint.sh"]
