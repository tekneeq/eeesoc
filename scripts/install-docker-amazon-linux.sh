#!/usr/bin/env bash
# Install Docker Engine on Amazon Linux 2 / 2023 and allow ec2-user to run it.
# Run once on the EC2 host (same idea as the julia box setup):
#   ./scripts/install-docker-amazon-linux.sh
set -euo pipefail

if command -v docker >/dev/null 2>&1; then
  echo "docker already on PATH: $(command -v docker)"
  docker --version
  exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Re-running with sudo..."
  exec sudo -E bash "$0" "$@"
fi

if [ -f /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
else
  echo "Cannot detect OS (missing /etc/os-release)"
  exit 1
fi

echo "Detected: ${NAME:-unknown} ${VERSION_ID:-}"

if command -v dnf >/dev/null 2>&1; then
  dnf install -y docker
elif command -v yum >/dev/null 2>&1; then
  yum install -y docker
else
  echo "Neither dnf nor yum found — install Docker manually:"
  echo "  https://docs.docker.com/engine/install/"
  exit 1
fi

systemctl enable --now docker

# Prefer the login user who invoked sudo, else ec2-user.
TARGET_USER="${SUDO_USER:-ec2-user}"
if id "$TARGET_USER" >/dev/null 2>&1; then
  usermod -aG docker "$TARGET_USER"
  echo "Added $TARGET_USER to the docker group."
fi

echo
echo "Docker installed: $(docker --version)"
echo "Log out and back in (or run: newgrp docker) so group membership applies,"
echo "then from ~/eeesoc run: ./deploy.sh"
