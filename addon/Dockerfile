ARG BUILD_FROM
FROM ${BUILD_FROM}

# HA passes a Python-capable base image via BUILD_FROM.
RUN apk add --no-cache python3 py3-pip iputils \
 && pip3 install --no-cache-dir --break-system-packages websocket-client==1.9.2

WORKDIR /app
COPY reader.py run.sh app.wgt /app/
COPY img/ /app/img/
RUN chmod a+x /app/run.sh

# Persist config/token/images under /data — the add-on's own volume, which
# survives updates and rebuilds (it is only removed on uninstall). /share is
# SMB-exported, so the HA long-lived token must NOT be stored there.
# NOTE: HA's s6-overlay base sweeps the environment, so run.sh re-imports these
# with with-contenv; see run.sh for details.
ENV CONFIG_FILE=/data/config.json \
    TV_TOKEN_FILE=/data/tv_token \
    IMG_DIR=/data/img \
    PYTHONUNBUFFERED=1

CMD ["/app/run.sh"]
