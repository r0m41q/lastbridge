FROM python:3.14.10-slim-trixie@sha256:3cabfd7d7aaf394e6040e79fa1eec6ca1c52b74ed9188650472695d9a9326999 AS base

RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

FROM base AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    PATH="/app/venv/bin:$PATH"

WORKDIR /app

RUN python -m venv /app/venv \
    && /app/venv/bin/python -m pip install --no-cache-dir uv==0.13.6

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable --inexact \
    && /app/venv/bin/python -m pip uninstall -y uv pip

FROM base AS media-builder

# Build only the file/container/audio features needed to validate source audio.
# Debian verifies the source against its signed repository metadata. Carry the
# corresponding source archives and license in the runtime for redistribution.
RUN sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends build-essential pkg-config xz-utils

WORKDIR /build
RUN apt-get source ffmpeg=7:7.1.5-0+deb13u1 \
    && cd ffmpeg-7.1.5 \
    && ./configure --disable-everything --disable-autodetect --disable-network \
       --disable-doc --disable-debug --disable-shared --enable-static \
       --disable-x86asm --disable-avdevice --disable-swscale --disable-postproc \
       --enable-small --enable-ffmpeg --enable-ffprobe \
       --enable-avcodec --enable-avformat --enable-avfilter --enable-swresample \
       --enable-protocol=file --enable-demuxer=mov,matroska \
       --enable-decoder=aac,opus --enable-parser=aac,opus \
       --enable-muxer=null,mp4 --enable-bsf=aac_adtstoasc \
       --enable-encoder=pcm_s16le \
       --enable-filter=aresample,aformat,anull \
    && make -j2 ffmpeg ffprobe \
    && install -Dm755 ffmpeg /media/bin/ffmpeg \
    && install -Dm755 ffprobe /media/bin/ffprobe \
    && install -Dm644 COPYING.LGPLv2.1 /media/share/lastbridge-media/COPYING.LGPLv2.1 \
    && mkdir -p /media/share/lastbridge-media/source \
    && cp ../ffmpeg_*.dsc ../ffmpeg_*.tar.* /media/share/lastbridge-media/source/ \
    && cp config.h /media/share/lastbridge-media/config.h

FROM base AS runtime

COPY --from=media-builder /media/ /usr/local/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/venv/bin:$PATH"

WORKDIR /app

RUN python -m pip uninstall -y pip
COPY --from=builder /app/venv /app/venv

RUN useradd --uid 10001 --create-home lastbridge \
    && mkdir -p /app/data/cache /app/data/saved /app/credentials \
    && chown -R lastbridge:lastbridge /app/data /app/credentials

USER lastbridge
ENV LASTBRIDGE_HOST=0.0.0.0 LASTBRIDGE_DATA_DIR=/app/data
EXPOSE 5274
CMD ["lastbridge"]
