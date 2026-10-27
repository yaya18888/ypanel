#!/usr/bin/env bash
# =====================================================================
#  YPanel one-line installer / updater
#    curl -fsSL https://raw.githubusercontent.com/__OWNER__/__REPO__/main/get.sh | bash
#  With options:
#    curl -fsSL https://raw.githubusercontent.com/__OWNER__/__REPO__/main/get.sh | bash -s -- --with-mail=mail.example.com
#  Run the same command again to update (data and sites are kept).
# =====================================================================
set -euo pipefail
REPO="${YPANEL_REPO:-https://github.com/__OWNER__/__REPO__.git}"
BRANCH="${YPANEL_BRANCH:-main}"
DIR=/usr/local/src/ypanel

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo -i)"; exit 1; }
export DEBIAN_FRONTEND=noninteractive
command -v git >/dev/null 2>&1 || { apt-get update -y >/dev/null && apt-get install -y git ca-certificates >/dev/null; }

if [ -d "$DIR/.git" ]; then
  echo "==> Updating YPanel source ($BRANCH)"
  git -C "$DIR" fetch --depth 1 origin "$BRANCH"
  git -C "$DIR" reset --hard "origin/$BRANCH"
else
  echo "==> Downloading YPanel"
  rm -rf "$DIR"
  git clone --depth 1 -b "$BRANCH" "$REPO" "$DIR"
fi

cd "$DIR"
exec bash install.sh "$@"
