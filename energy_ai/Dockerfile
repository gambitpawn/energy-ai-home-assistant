ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-virtualenv

WORKDIR /opt/energy-ai

COPY requirements.txt /opt/energy-ai/requirements.txt
RUN python3 -m venv /opt/energy-ai/venv \
    && /opt/energy-ai/venv/bin/pip install --no-cache-dir -r /opt/energy-ai/requirements.txt

COPY app /opt/energy-ai/app
COPY run.sh /run.sh

RUN chmod a+x /run.sh

CMD ["/run.sh"]
