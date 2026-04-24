FROM public.ecr.aws/docker/library/ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    ca-certificates \
 && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip \
 && unzip /tmp/awscliv2.zip -d /tmp \
 && /tmp/aws/install \
 && rm -rf /tmp/awscliv2.zip /tmp/aws /var/lib/apt/lists/*

COPY hls_relay.sh /app/hls_relay.sh
RUN chmod +x /app/hls_relay.sh

CMD ["/bin/bash", "/app/hls_relay.sh"]
