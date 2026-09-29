FROM python:3.13-slim

ARG KUBECTL_VERSION=v1.36.4
ARG TARGETARCH=amd64
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
    && curl -fsSL -o /tmp/kubectl.sha256 "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl.sha256" \
    && echo "$(cat /tmp/kubectl.sha256)  /usr/local/bin/kubectl" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/kubectl \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /tmp/kubectl.sha256

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY scripts/entrypoint.sh ./entrypoint.sh
RUN pip install --no-cache-dir . && chmod 0755 /app/entrypoint.sh
USER 10001:10001
ENTRYPOINT ["/app/entrypoint.sh"]
