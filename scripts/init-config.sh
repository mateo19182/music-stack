#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "${BASH_SOURCE[0]}")/.."
install -d -m 700 config/acquisition config/navidrome config/slskd config/deemix config/beets config/airvpn/gluetun
copy_if_missing() {
  if [[ ! -e "$2" ]]; then install -m 600 "$1" "$2"; fi
}
copy_if_missing .env.example .env
copy_if_missing examples/acquisition.config.json config/acquisition/config.json
copy_if_missing examples/navidrome.toml config/navidrome/navidrome.toml
copy_if_missing examples/slskd.yml config/slskd/slskd.yml
copy_if_missing examples/airvpn.env config/airvpn/airvpn.env
copy_if_missing examples/beets.yaml config/beets/config.yaml
printf '%s\n' 'Private configuration templates prepared. Set credentials, create the Deemix htpasswd file, and prepare media directories before starting services.'
