ARG BUILD_FROM
FROM ${BUILD_FROM}

# HA passes a Python-capable base image via BUILD_FROM.
RUN apk add --no-cache python3 py3-pip iputils \
 && pip3 install --no-cache-dir --break-system-packages websocket-client==1.9.2

WORKDIR /app
COPY reader.py run.sh app.wgt /app/
COPY img/ /app/img/
RUN chmod a+x /app/run.sh

# Persist config/token/images under /share so they survive add-on updates
# (the add-on's /data can be reset on update/reinstall; /share is mapped and stable).
ENV CONFIG_FILE=/share/yast-bridge/config.json \
    TV_TOKEN_FILE=/share/yast-bridge/tv_token \
    IMG_DIR=/share/yast-bridge/img \
    PYTHONUNBUFFERED=1

CMD ["/app/run.sh"]
