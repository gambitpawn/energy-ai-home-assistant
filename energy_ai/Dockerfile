ARG BUILD_VERSION
ARG BUILD_ARCH
FROM ghcr.io/home-assistant/base:3.22

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-virtualenv \
    py3-numpy \
    py3-scipy \
    py3-scikit-learn

WORKDIR /opt/energy-ai

COPY requirements.txt /opt/energy-ai/requirements.txt
RUN python3 -m venv --system-site-packages /opt/energy-ai/venv \
    && /opt/energy-ai/venv/bin/pip install --no-cache-dir -r /opt/energy-ai/requirements.txt \
    && /opt/energy-ai/venv/bin/python -c "import numpy, scipy, sklearn; print('ML stack:', numpy.__version__, scipy.__version__, sklearn.__version__)"

COPY app /opt/energy-ai/app
COPY run.sh /run.sh

RUN chmod a+x /run.sh

CMD ["/run.sh"]
