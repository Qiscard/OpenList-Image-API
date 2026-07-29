#!/usr/bin/env bash
# OpenList Image API bootstrap installer. Run as root on a systemd-based Linux host.
set -Eeuo pipefail

REPOSITORY="Qiscard/OpenList-Image-API"
RELEASE_REF="v1.0.0"
APP_DIR="/opt/openlist-image-api"
CONFIG_DIR="/etc/openlist-image-api"
STATE_DIR="/var/lib/openlist-image-api"
SERVICE_USER="openlist-image"
SERVICE_NAME="openlist-image-api"
RAW_BASE="https://raw.githubusercontent.com/${REPOSITORY}/${RELEASE_REF}"

log() { printf '[openlist-image-api] %s\n' "$*"; }
fail() { printf '[openlist-image-api] ERROR: %s\n' "$*" >&2; exit 1; }

require_root() {
  [[ "${EUID}" -eq 0 ]] || fail "run this installer with sudo bash"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

download() {
  local remote_path="$1"
  local local_path="$2"
  curl --fail --location --silent --show-error --retry 3 --connect-timeout 15 \
    "${RAW_BASE}/${remote_path}" --output "${local_path}"
}

create_service_user() {
  if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --home-dir "${STATE_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
  fi
}

create_default_config() {
  [[ -f "${CONFIG_DIR}/config.json" ]] && return
  cat > "${CONFIG_DIR}/config.json" <<EOF
{
  "listen_host": "127.0.0.1",
  "listen_port": 8790,
  "openlist_api_url": "http://127.0.0.1:5244",
  "openlist_token_file": "${CONFIG_DIR}/openlist.token",
  "state_dir": "${STATE_DIR}",
  "directories": [],
  "extensions": [".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"],
  "view_layout": "single",
  "delivery": "preview",
  "url_cache_size": 200,
  "url_cache_ttl_seconds": 240,
  "admin_token_file": "${CONFIG_DIR}/admin.token"
}
EOF
}

create_admin_token() {
  [[ -s "${CONFIG_DIR}/admin.token" ]] && return
  umask 077
  python3 - <<'PY' > "${CONFIG_DIR}/admin.token"
import secrets
print(secrets.token_urlsafe(32))
PY
}

create_systemd_unit() {
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=OpenList Image API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
ExecStart=/usr/bin/python3 ${APP_DIR}/openlist_image_api.py --config ${CONFIG_DIR}/config.json serve
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=${CONFIG_DIR} ${STATE_DIR}
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
EOF
}

create_tui_command() {
  cat > "/usr/local/bin/openlist-image-api" <<EOF
#!/usr/bin/env bash
exec /usr/bin/python3 ${APP_DIR}/openlist_tui.py "\$@"
EOF
  chmod 0755 "/usr/local/bin/openlist-image-api"
}

install_openlist() {
  local temporary
  temporary="$(mktemp -d)"
  trap 'rm -rf "${temporary}"' RETURN
  log "downloading the official OpenList v4 installer"
  curl --fail --location --silent --show-error --retry 3 \
    "https://res.oplist.org/script/v4.sh" --output "${temporary}/install-openlist-v4.sh"
  bash "${temporary}/install-openlist-v4.sh"
}

uninstall() {
  read -r -p "Remove this Image API installation and its local state? [y/N] " answer
  [[ "${answer}" == "y" || "${answer}" == "Y" ]] || exit 0
  systemctl disable --now "${SERVICE_NAME}" 2>/dev/null || true
  rm -f "/etc/systemd/system/${SERVICE_NAME}.service" "/usr/local/bin/openlist-image-api"
  rm -rf "${APP_DIR}" "${CONFIG_DIR}" "${STATE_DIR}"
  systemctl daemon-reload
  log "Image API removed. OpenList itself was not removed."
}

main() {
  require_root
  require_command curl
  require_command python3
  require_command sha256sum

  case "${1:-}" in
    --install-openlist)
      install_openlist
      exit 0
      ;;
    --uninstall)
      uninstall
      exit 0
      ;;
    ""|--install)
      ;;
    *)
      cat <<USAGE
Usage: sudo bash install.sh [--install | --install-openlist | --uninstall]
USAGE
      exit 2
      ;;
  esac

  local temporary
  temporary="$(mktemp -d)"
  trap 'rm -rf "${temporary}"' EXIT
  mkdir -p "${temporary}/src"
  download "SHA256SUMS" "${temporary}/SHA256SUMS"
  download "src/openlist_image_api.py" "${temporary}/src/openlist_image_api.py"
  download "src/openlist_tui.py" "${temporary}/src/openlist_tui.py"
  download "VERSION" "${temporary}/VERSION"
  (
    cd "${temporary}"
    sha256sum --check --status SHA256SUMS
  ) || fail "download checksum verification failed"

  create_service_user
  install -d -m 0755 "${APP_DIR}" "${CONFIG_DIR}" "${STATE_DIR}"
  install -m 0755 "${temporary}/src/openlist_image_api.py" "${APP_DIR}/openlist_image_api.py"
  install -m 0755 "${temporary}/src/openlist_tui.py" "${APP_DIR}/openlist_tui.py"
  install -m 0644 "${temporary}/VERSION" "${APP_DIR}/VERSION"
  create_default_config
  create_admin_token
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${CONFIG_DIR}" "${STATE_DIR}"
  chmod 0700 "${CONFIG_DIR}"
  chmod 0600 "${CONFIG_DIR}/config.json" "${CONFIG_DIR}/admin.token"
  create_systemd_unit
  create_tui_command
  systemctl daemon-reload
  systemctl enable --now "${SERVICE_NAME}"

  log "installation complete"
  log "run: sudo openlist-image-api"
  log "the API and WebUI listen on the local machine by default"
  log "set the OpenList token and choose image directories in the TUI before rebuilding the index"
}

main "$@"
