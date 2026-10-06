#!/usr/bin/env bash
# Download Essentia's Discogs-EffNet genre and mood models into the acquisition
# state directory (mounted at /state, so the app finds them in /state/models).
# Models: https://essentia.upf.edu/models.html, licensed CC BY-NC-ND 4.0.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
state=${ACQUISITION_STATE_DIR:-$(grep -s '^ACQUISITION_STATE_DIR=' .env | cut -d= -f2-)}
target=${1:-${state:-config/acquisition}/models}
base=https://essentia.upf.edu/models
mkdir -p "$target"
while read -r sum path; do
  file=$target/${path##*/}
  if [[ -f $file ]] && echo "$sum  $file" | sha256sum --check --quiet >/dev/null 2>&1; then continue; fi
  for attempt in 1 2 3 4 5 6; do
    curl -sSf --connect-timeout 20 --max-time 300 -o "$file.part" "$base/$path" && break
    sleep 5
  done
  echo "$sum  $file.part" | sha256sum --check --quiet
  mv "$file.part" "$file"
done <<'MODELS'
3ed9af50d5367c0b9c795b294b00e7599e4943244f4cbd376869f3bfc87721b1 feature-extractors/discogs-effnet/discogs-effnet-bs64-1.pb
3885ba078a35249af94b8e5e4247689afac40deca4401a4bc888daf5a579c01c classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.pb
2d367319d9b782ffa10f69abf0e805b3ac4e10899025e5bdbaceda3919b243e0 classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.json
de322aee7f4da29ecbd149c86e7964bd6fabe41ca85698a0be40a39d49e88633 classification-heads/mood_happy/mood_happy-discogs-effnet-1.pb
ed4601bc396b23367d29cf45aa327366cdfdbd3e2baeb7a3f7893237f1ffd9e6 classification-heads/mood_happy/mood_happy-discogs-effnet-1.json
4865cba49968b6ec295db3e8af6b4a7bb506b1a646628b9f07ee9910d52df82c classification-heads/mood_sad/mood_sad-discogs-effnet-1.pb
7f2c00099ad8255af33e43ab77d1ac2c86a1dd9f46d3e5ea2a9a52246f5033d9 classification-heads/mood_sad/mood_sad-discogs-effnet-1.json
7705284e3a67f23f04d3f2fd75e18a82c0e70db8875b7b6f7061f2432de80858 classification-heads/mood_aggressive/mood_aggressive-discogs-effnet-1.pb
81773e95d78db1b93283d73b2d06344d1ff79685b57d9428a40d47fdfcf537b8 classification-heads/mood_aggressive/mood_aggressive-discogs-effnet-1.json
2c5aa6666b58fe80429a2dc677a135e9995922889b5a399af87d1c15f0ebb71d classification-heads/mood_relaxed/mood_relaxed-discogs-effnet-1.pb
86c0fe1c2c6d49bf08537bc2d3a602204feaede011ed119c1fc6c36270f60e6a classification-heads/mood_relaxed/mood_relaxed-discogs-effnet-1.json
bbd773a27002978179dd6fc5ffb75b7c41e6a7e412b5c5254d51a025e5b9373e classification-heads/mood_party/mood_party-discogs-effnet-1.pb
a7f15c6e05db7c3e9226646a87162ebb9cc00aea8bdbbc910aedafa7f4a8fdfb classification-heads/mood_party/mood_party-discogs-effnet-1.json
e1251f02cdc846445e2bc1fb3fe9963e32728c5c000e009188ae47c8963cb4c4 classification-heads/danceability/danceability-discogs-effnet-1.pb
589e61dc05f0d7935be4b5fa7ab4341e5dc7cfecc6898f83503558564790cd75 classification-heads/danceability/danceability-discogs-effnet-1.json
MODELS
echo "Models ready in $target"
