#!/usr/bin/env bash
# OpenList Image API bootstrap installer. Run as root on a systemd-based Linux host.
set -Eeuo pipefail

GITHUB_REPOSITORY="Qiscard/OpenList-Image-API"
GITEE_REPOSITORY="qiscard/OpenList-Image-API"
RELEASE_REF="v1.3.0"
UPDATE_REF="main"
APP_DIR="/opt/openlist-image-api"
CONFIG_DIR="/etc/openlist-image-api"
STATE_DIR="/var/lib/openlist-image-api"
SERVICE_USER="openlist-image"
SERVICE_NAME="openlist-image-api"
OPENLIST_INSTALL_DIR="/opt/openlist"
OPENLIST_SERVICE_NAME="openlist"
SOURCE="auto"
OPENLIST_DOWNLOAD_MODE="direct"
GITHUB_RAW_BASE="https://raw.githubusercontent.com/${GITHUB_REPOSITORY}/${RELEASE_REF}"
GITEE_RAW_BASE="https://gitee.com/${GITEE_REPOSITORY}/raw/${RELEASE_REF}"
OPENLIST_RELEASE_BASE="https://github.com/OpenListTeam/OpenList/releases/latest/download"
OPENLIST_PROXY_CANDIDATES=(
  "https://edgeone.gh-proxy.com"
  "https://hk.gh-proxy.com"
  "https://gh-proxy.com"
  "https://gh.dpik.top"
)

log() { printf '[openlist-image-api] %s\n' "$*" >&2; }
fail() { printf '[openlist-image-api] ERROR: %s\n' "$*" >&2; exit 1; }

require_root() {
  [[ "${EUID}" -eq 0 ]] || fail "run this installer with sudo bash"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

fetch_from() {
  local base="$1"
  local remote_path="$2"
  local local_path="$3"
  curl --fail --location --silent --show-error --retry 2 --retry-all-errors --connect-timeout 15 \
    "${base}/${remote_path}" --output "${local_path}"
}

download() {
  local remote_path="$1"
  local local_path="$2"
  case "${SOURCE}" in
    github)
      fetch_from "${GITHUB_RAW_BASE}" "${remote_path}" "${local_path}"
      ;;
    gitee)
      fetch_from "${GITEE_RAW_BASE}" "${remote_path}" "${local_path}"
      ;;
    auto)
      if ! fetch_from "${GITEE_RAW_BASE}" "${remote_path}" "${local_path}"; then
        log "Gitee source unavailable; falling back to GitHub"
        fetch_from "${GITHUB_RAW_BASE}" "${remote_path}" "${local_path}"
      fi
      ;;
    *)
      fail "invalid source: ${SOURCE}"
      ;;
  esac
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
  "listen_host": "0.0.0.0",
  "listen_port": 8790,
  "openlist_api_url": "http://127.0.0.1:5244",
  "openlist_token_file": "${CONFIG_DIR}/openlist.token",
  "state_dir": "${STATE_DIR}",
  "directories": [],
  "extensions": [".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"],
  "view_layout": "single",
  "delivery": "preview",
  "caption_mode": "path",
  "grid_gap": 12,
  "grid_scale": 150,
  "url_cache_size": 1000,
  "url_cache_ttl_seconds": 1800,
  "admin_token_file": "${CONFIG_DIR}/admin.token"
}
EOF
}

migrate_listen_host() {
  [[ -f "${CONFIG_DIR}/config.json" ]] || return
  python3 - "${CONFIG_DIR}/config.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
if data.get("listen_host") == "127.0.0.1":
    data["listen_host"] = "0.0.0.0"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("[openlist-image-api] migrated listen_host to 0.0.0.0 for NAT/public access")
PY
}

migrate_performance_defaults() {
  [[ -f "${CONFIG_DIR}/config.json" ]] || return
  python3 - "${CONFIG_DIR}/config.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
legacy_defaults = {
    "grid_scale": (125, 150),
    "url_cache_size": (200, 1000),
    "url_cache_ttl_seconds": (240, 1800),
}
changed = []
for key, (old_value, new_value) in legacy_defaults.items():
    if data.get(key) == old_value:
        data[key] = new_value
        changed.append(key)
if changed:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("[openlist-image-api] migrated performance defaults: " + ", ".join(changed))
PY
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

openlist_architecture() {
  case "$(uname -m)" in
    x86_64) echo "amd64" ;;
    aarch64) echo "arm64" ;;
    armv7l|armv7*) echo "arm-7" ;;
    armv6l|armv6*) echo "arm-6" ;;
    i386|i686) echo "386" ;;
    *) fail "unsupported OpenList architecture: $(uname -m)" ;;
  esac
}

openlist_proxy_speed() {
  local proxy="$1"
  local archive_url="$2"
  local metric code latency
  metric="$(curl --location --silent --show-error --range 0-0 --output /dev/null \
    --write-out '%{http_code} %{time_starttransfer}' --connect-timeout 5 --max-time 15 \
    "${proxy}/${archive_url}" 2>/dev/null || true)"
  code="${metric%% *}"
  latency="${metric#* }"
  if [[ "${code}" =~ ^(200|206|302)$ ]] && [[ "${latency}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    printf '%s\n' "${latency} ${proxy}"
  fi
}

select_openlist_download_url() {
  local architecture="$1"
  local archive_url="${OPENLIST_RELEASE_BASE}/openlist-linux-musl-${architecture}.tar.gz"
  if [[ "${OPENLIST_DOWNLOAD_MODE}" == "direct" ]]; then
    log "OpenList download: direct GitHub Releases"
    printf '%s\n' "${archive_url}"
    return
  fi

  log "Benchmarking OpenList proxy candidates..."
  local best_proxy=""
  local best_latency=""
  local proxy result latency
  for proxy in "${OPENLIST_PROXY_CANDIDATES[@]}"; do
    result="$(openlist_proxy_speed "${proxy}" "${archive_url}")"
    if [[ -z "${result}" ]]; then
      log "Proxy unavailable: ${proxy}"
      continue
    fi
    latency="${result%% *}"
    log "Proxy latency: ${proxy} ${latency}s"
    if [[ -z "${best_latency}" ]] || awk "BEGIN { exit !(${latency} < ${best_latency}) }"; then
      best_latency="${latency}"
      best_proxy="${proxy}"
    fi
  done
  if [[ -z "${best_proxy}" ]]; then
    log "No proxy passed the benchmark; falling back to direct GitHub Releases"
    printf '%s\n' "${archive_url}"
  else
    log "Selected fastest proxy: ${best_proxy} (${best_latency}s)"
    printf '%s\n' "${best_proxy}/${archive_url}"
  fi
}

create_openlist_systemd_unit() {
  cat > "/etc/systemd/system/${OPENLIST_SERVICE_NAME}.service" <<EOF
[Unit]
Description=OpenList service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${OPENLIST_INSTALL_DIR}
ExecStart=${OPENLIST_INSTALL_DIR}/openlist server
Restart=on-failure
RestartSec=3
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
}

install_openlist_embedded() {
  require_command find
  require_command tar
  require_command systemctl
  local architecture archive_url temporary extracted_binary
  architecture="$(openlist_architecture)"
  archive_url="$(select_openlist_download_url "${architecture}")"
  temporary="$(mktemp -d)"
  trap 'rm -rf "${temporary}"' RETURN

  log "Downloading OpenList for ${architecture} (two retries enabled)"
  curl --fail --location --silent --show-error --retry 2 --retry-all-errors --connect-timeout 15 \
    "${archive_url}" --output "${temporary}/openlist.tar.gz"
  tar -tzf "${temporary}/openlist.tar.gz" >/dev/null
  mkdir -p "${temporary}/extracted"
  tar -xzf "${temporary}/openlist.tar.gz" -C "${temporary}/extracted"
  extracted_binary="$(find "${temporary}/extracted" -type f -name openlist -print -quit)"
  [[ -n "${extracted_binary}" ]] || fail "OpenList archive does not contain the openlist binary"

  if systemctl is-active --quiet "${OPENLIST_SERVICE_NAME}"; then
    systemctl stop "${OPENLIST_SERVICE_NAME}"
  fi
  install -d -m 0755 "${OPENLIST_INSTALL_DIR}"
  install -m 0755 "${extracted_binary}" "${OPENLIST_INSTALL_DIR}/openlist"
  create_openlist_systemd_unit
  systemctl daemon-reload
  systemctl enable --now "${OPENLIST_SERVICE_NAME}"
  log "OpenList installed: $(${OPENLIST_INSTALL_DIR}/openlist version 2>/dev/null | head -n 1 || echo unknown)"
  log "OpenList runs as ${OPENLIST_SERVICE_NAME}. No Docker components are installed or managed."
}

set_download_ref() {
  local reference="$1"
  GITHUB_RAW_BASE="https://raw.githubusercontent.com/${GITHUB_REPOSITORY}/${reference}"
  GITEE_RAW_BASE="https://gitee.com/${GITEE_REPOSITORY}/raw/${reference}"
}

install_image_api() {
  local install_mode="$1"
  local temporary was_enabled=0 was_active=0
  temporary="$(mktemp -d)"
  mkdir -p "${temporary}/src"
  download "SHA256SUMS" "${temporary}/SHA256SUMS"
  download "install.sh" "${temporary}/install.sh"
  download "src/openlist_image_api.py" "${temporary}/src/openlist_image_api.py"
  download "src/openlist_tui.py" "${temporary}/src/openlist_tui.py"
  download "VERSION" "${temporary}/VERSION"
  (
    cd "${temporary}"
    sha256sum --check --status SHA256SUMS
  ) || fail "download checksum verification failed"

  if [[ "${install_mode}" == "update" ]]; then
    systemctl is-enabled --quiet "${SERVICE_NAME}" && was_enabled=1 || true
    systemctl is-active --quiet "${SERVICE_NAME}" && was_active=1 || true
    if (( was_active )); then
      systemctl stop "${SERVICE_NAME}"
    fi
  fi

  create_service_user
  install -d -m 0755 "${APP_DIR}" "${CONFIG_DIR}" "${STATE_DIR}"
  install -m 0755 "${temporary}/install.sh" "${APP_DIR}/install.sh"
  install -m 0755 "${temporary}/src/openlist_image_api.py" "${APP_DIR}/openlist_image_api.py"
  install -m 0755 "${temporary}/src/openlist_tui.py" "${APP_DIR}/openlist_tui.py"
  install -m 0644 "${temporary}/VERSION" "${APP_DIR}/VERSION"
  create_default_config
  migrate_listen_host
  migrate_performance_defaults
  create_admin_token
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${CONFIG_DIR}" "${STATE_DIR}"
  chmod 0700 "${CONFIG_DIR}"
  chmod 0600 "${CONFIG_DIR}/config.json" "${CONFIG_DIR}/admin.token"
  create_systemd_unit
  create_tui_command
  systemctl daemon-reload

  if [[ "${install_mode}" == "update" ]]; then
    if (( was_enabled )); then
      systemctl enable "${SERVICE_NAME}"
    else
      systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    fi
    if (( was_active )); then
      systemctl start "${SERVICE_NAME}"
    else
      systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    fi
    log "update complete from ${SOURCE}/${UPDATE_REF}; Image API service state restored"
  else
    systemctl enable --now "${SERVICE_NAME}"
    log "installation complete from ${SOURCE} source"
    log "run: sudo openlist-image-api"
    log "the Image API listens on all network interfaces by default; WebUI administration still requires its token"
    log "set the OpenList token and choose image directories in the TUI before rebuilding the index"
  fi
  rm -rf "${temporary}"
}

uninstall_image_api() {
  systemctl disable --now "${SERVICE_NAME}" 2>/dev/null || true
  rm -f "/etc/systemd/system/${SERVICE_NAME}.service" "/usr/local/bin/openlist-image-api"
  rm -rf "${APP_DIR}" "${CONFIG_DIR}" "${STATE_DIR}"
  if id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    userdel "${SERVICE_USER}" 2>/dev/null || true
  fi
  systemctl daemon-reload
  log "Image API service, command, scripts, configuration, and local state removed."
}

uninstall() {
  local scope="$1"
  case "${scope}" in
    api)
      uninstall_image_api
      ;;
    complete)
      uninstall_image_api
      systemctl disable --now "${OPENLIST_SERVICE_NAME}" 2>/dev/null || true
      rm -f "/etc/systemd/system/${OPENLIST_SERVICE_NAME}.service"
      rm -rf "${OPENLIST_INSTALL_DIR}"
      systemctl daemon-reload
      log "OpenList and Image API services, scripts, configuration, and local state removed."
      ;;
    *)
      fail "--uninstall must be api or complete"
      ;;
  esac
}

usage() {
  cat <<USAGE
Usage: sudo bash install.sh [options]

Options:
  --source github|gitee|auto        Select this project's source (default: auto)
  --install-openlist                Use the embedded, non-Docker OpenList installer
  --openlist-download direct|auto   Direct download or benchmark proxy candidates (default: direct)
  --update                          Update Image API from the latest main branch and restore its prior service state
  --uninstall api|complete          Remove only Image API, or remove both Image API and embedded OpenList
USAGE
}

main() {
  require_root

  local action="install"
  local uninstall_scope=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --source)
        [[ $# -ge 2 ]] || fail "--source requires a value"
        SOURCE="$2"
        shift 2
        ;;
      --install-openlist)
        action="install-openlist"
        shift
        ;;
      --openlist-download)
        [[ $# -ge 2 ]] || fail "--openlist-download requires a value"
        OPENLIST_DOWNLOAD_MODE="$2"
        shift 2
        ;;
      --update)
        action="update"
        shift
        ;;
      --uninstall)
        [[ $# -ge 2 ]] || fail "--uninstall requires api or complete"
        action="uninstall"
        uninstall_scope="$2"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        fail "unknown option: $1"
        ;;
    esac
  done

  case "${SOURCE}" in github|gitee|auto) ;; *) fail "--source must be github, gitee, or auto" ;; esac
  case "${OPENLIST_DOWNLOAD_MODE}" in direct|auto) ;; *) fail "--openlist-download must be direct or auto" ;; esac
  case "${action}" in
    install-openlist)
      require_command curl
      install_openlist_embedded
      ;;
    uninstall)
      uninstall "${uninstall_scope}"
      ;;
    update)
      require_command curl
      require_command python3
      require_command sha256sum
      set_download_ref "${UPDATE_REF}"
      install_image_api "update"
      ;;
    install)
      require_command curl
      require_command python3
      require_command sha256sum
      install_image_api "install"
      ;;
  esac
}

main "$@"
