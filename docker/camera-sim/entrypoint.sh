#!/bin/sh
set -eu
mkdir -p /run/sim
[ -f /run/sim/fault ] || echo none > /run/sim/fault
sed -e "s|@SIM_USER@|${SIM_USER}|" -e "s|@SIM_PASSWORD@|${SIM_PASSWORD}|" /sim/mediamtx.yml.tmpl > /sim/mediamtx.yml
python3 /sim/onvif_sim.py &
exec mediamtx /sim/mediamtx.yml
