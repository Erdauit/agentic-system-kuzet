#!/bin/sh
# Источник видео симулятора: тестовая таблица testsrc2, кодировщик openh264 (BSD).
# Неисправность читается из /run/sim/fault: none | freeze | blur | black | overexposed |
# occlude | shift | stop. При смене неисправности ffmpeg перезапускается с новым фильтром.
set -u
PATH_NAME="$1"
case "$PATH_NAME" in
  *101) SIZE=1280x720; BITRATE=1500k ;;
  *) SIZE=640x360; BITRATE=400k ;;
esac
FPS="${SIM_FPS:-15}"
FAULT_FILE=/run/sim/fault
current() { cat "$FAULT_FILE" 2>/dev/null || echo none; }

while true; do
  FAULT="$(current)"
  if [ "$FAULT" = "stop" ]; then
    sleep 1
    continue
  fi
  INPUT="-re -f lavfi -i testsrc2=size=${SIZE}:rate=${FPS}"
  VF="null"
  case "$FAULT" in
    freeze)
      STILL="/tmp/still-${SIZE}.ppm"
      [ -f "$STILL" ] || ffmpeg -hide_banner -loglevel error -f lavfi -i "testsrc2=size=${SIZE}" -frames:v 1 -y "$STILL"
      INPUT="-re -loop 1 -framerate ${FPS} -i ${STILL}" ;;
    blur) VF="gblur=sigma=6" ;;  # boxblur — GPL-фильтр, в LGPL-сборке его нет
    black) VF="lutyuv=y=16:u=128:v=128" ;;
    overexposed) VF="lutyuv=y=250:u=128:v=128" ;;
    occlude) VF="drawbox=x=0:y=0:w=iw*0.85:h=ih:color=gray:t=fill" ;;
    shift) VF="crop=w=iw*0.6:h=ih*0.6:x=iw*0.4:y=ih*0.35,scale=w=${SIZE%x*}:h=${SIZE#*x}" ;;
  esac
  # shellcheck disable=SC2086 # INPUT намеренно разбивается на аргументы
  ffmpeg -hide_banner -loglevel error $INPUT -vf "$VF,format=yuv420p" \
    -c:v libopenh264 -b:v "$BITRATE" -g "$((FPS * 2))" \
    -f rtsp -rtsp_transport tcp "rtsp://127.0.0.1:554/${PATH_NAME}" &
  PID=$!
  while kill -0 "$PID" 2>/dev/null; do
    if [ "$(current)" != "$FAULT" ]; then
      kill "$PID" 2>/dev/null
      break
    fi
    sleep 1
  done
  wait "$PID" 2>/dev/null
  sleep 0.5
done
