# syntax=docker/dockerfile:1.7
# media-base — общий базовый образ сервисов, работающих с видео (core-api, vision-pipeline,
# camera-sim). Содержит FFmpeg, собранный ТОЛЬКО под LGPL (SPEC.md §2.7).
#
# Зачем своя сборка: колёса PyAV с PyPI содержат FFmpeg с libx264/libx265 (GPL). Здесь FFmpeg
# собирается без --enable-gpl и --enable-nonfree; H.264-кодировщик — libopenh264 (BSD-2-Clause,
# нужен только симулятору камер). Заголовки nv-codec-headers (MIT) включают NVDEC для Фазы 2.
# PyAV в сервисных образах собирается из исходников против этой сборки (--no-binary-package av).
#
# Сборка завершается ошибкой, если в конфигурации FFmpeg окажется GPL или nonfree.

ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm

FROM ${PYTHON_IMAGE} AS ffmpeg-build
ARG FFMPEG_VERSION=9.0.2
ARG FFMPEG_SHA256=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e
ARG NV_CODEC_HEADERS=n13.0.19.0
ARG NV_CODEC_HEADERS_SHA256=86d15d1a7c0ac73a0eafdfc57bebfeba7da8264595bf531cf4d8db1c22940116
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential pkg-config nasm curl xz-utils ca-certificates libopenh264-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl -fsSL -o nvch.tar.gz "https://github.com/FFmpeg/nv-codec-headers/archive/refs/tags/${NV_CODEC_HEADERS}.tar.gz" \
 && echo "${NV_CODEC_HEADERS_SHA256}  nvch.tar.gz" | sha256sum -c - \
 && tar xzf nvch.tar.gz && make -C "nv-codec-headers-${NV_CODEC_HEADERS}" install PREFIX=/usr/local
RUN curl -fsSL -o ffmpeg.tar.xz "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" \
 && echo "${FFMPEG_SHA256}  ffmpeg.tar.xz" | sha256sum -c - \
 && tar xJf ffmpeg.tar.xz
WORKDIR /build/ffmpeg-${FFMPEG_VERSION}
RUN ./configure \
      --prefix=/opt/ffmpeg \
      --extra-version=qoima-lgpl \
      --enable-shared --disable-static \
      --disable-doc --disable-debug --disable-ffplay \
      --disable-autodetect --enable-pthreads \
      --enable-libopenh264 \
      --enable-ffnvcodec --enable-nvdec --enable-cuvid \
 && make -j"$(nproc)" && make install
# Лицензионная проверка сборки: LGPL, без GPL и nonfree.
ENV LD_LIBRARY_PATH=/opt/ffmpeg/lib
RUN /opt/ffmpeg/bin/ffmpeg -hide_banner -buildconf > /build/buildconf.txt \
 && /opt/ffmpeg/bin/ffmpeg -hide_banner -L > /build/license.txt \
 && grep -q "Lesser General Public" /build/license.txt \
 && ! grep -qE -- "--enable-(gpl|nonfree)" /build/buildconf.txt

FROM ${PYTHON_IMAGE}
RUN apt-get update \
 && apt-get install -y --no-install-recommends libopenh264-7 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ffmpeg-build /opt/ffmpeg /opt/ffmpeg
COPY --from=ffmpeg-build /build/buildconf.txt /opt/ffmpeg/BUILDCONF.txt
RUN echo /opt/ffmpeg/lib > /etc/ld.so.conf.d/ffmpeg.conf && ldconfig
ENV PATH=/opt/ffmpeg/bin:$PATH \
    PKG_CONFIG_PATH=/opt/ffmpeg/lib/pkgconfig
