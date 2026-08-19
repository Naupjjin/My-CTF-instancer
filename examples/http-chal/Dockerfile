FROM python:3.12-slim

RUN useradd -m chal

# Bake the challenge into the image so it needs no volume mounts.
# (The docker-compose.yml still mounts ./share for local development.)
COPY share/ /home/chal/
RUN chown -R root:root /home/chal && chmod -R 755 /home/chal

USER chal
WORKDIR /home/chal

# Which port to listen on is the instancer's call: it hands every instance its
# own. This is only the fallback for running the image by hand.
ENV CHAL_PORT=8080

CMD ["python3", "server.py"]
