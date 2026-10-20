# syntax=docker/dockerfile:1.7
# Симулятор IP-камеры для разработки и e2e-тестов (`qoima sim`): RTSP (MediaMTX + ffmpeg на
# LGPL-сборке media-base) и ONVIF Profile S (WS-Discovery, GetProfiles, GetStreamUri с
# WS-Security). Не входит в поставку объекта.
ARG MEDIAMTX_IMAGE=bluenviron/mediamtx:1.21.2
ARG MEDIA_BASE=qoima/media-base:dev
FROM ${MEDIAMTX_IMAGE} AS mediamtx

FROM ${MEDIA_BASE}
COPY --from=mediamtx /mediamtx /usr/local/bin/mediamtx
COPY docker/camera-sim/ /sim/
RUN chmod +x /sim/source.sh /sim/entrypoint.sh
ENV SIM_USER=admin \
    SIM_PASSWORD=qoima-sim \
    SIM_FPS=15
EXPOSE 554 80 3702/udp
ENTRYPOINT ["/sim/entrypoint.sh"]
