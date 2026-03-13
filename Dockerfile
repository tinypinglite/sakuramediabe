FROM python:3.10-slim-bookworm
RUN sed -i s@/deb.debian.org/@/mirrors.tuna.tsinghua.edu.cn/@g /etc/apt/sources.list.d/debian.sources
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PUID=1000 \
    PGID=1000

RUN apt-get update && apt-get install -y \
    vim \
    imagemagick \
    supervisor \
    ffmpeg \
    ca-certificates \
    ocl-icd-libopencl1 \
    intel-opencl-icd \
    libze1 \
    libdrm2 \
    libglib2.0-0 \
    libtbb12 \
    && apt-get clean \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY lib/mtn /usr/bin/mtn
COPY requirements.txt /app/requirements.txt
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN pip3 install --no-cache-dir -r /app/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
RUN chmod +x /docker-entrypoint.sh \
    && groupadd --system app \
    && useradd --create-home --shell /bin/bash --gid app app \
    && chmod +x /usr/bin/mtn

COPY src /app/src
COPY supervisord.conf /etc/supervisor/supervisord.conf
VOLUME ["/data/db", "/data/cache/assets", "/data/cache/gfriends", "/data/indexes", "/data/logs", "/data/lib/joytag", "/data/config"]
EXPOSE 8000 9001
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["start"]
