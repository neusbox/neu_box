#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SERVICE_NAME="neu-box-worker.service"
INSTALLED_INSTALLER="/usr/local/sbin/neu-box-install"
CURRENT_MANIFEST="${NEU_BOX_CURRENT_MANIFEST:-/opt/neu-box/current/manifest.json}"
RELEASE_REPOSITORY="${NEU_BOX_RELEASE_REPOSITORY:-neusbox/neu_box}"
RELEASE_BASE_URL="${NEU_BOX_RELEASE_BASE_URL:-https://github.com}"

usage() {
    cat <<'EOF'
用法: ./run.sh [命令] [参数]

不带命令时进入 Neu Box 管理菜单。

命令:
  menu                         打开交互菜单
  install [发布目录]           首次安装 worker
  upgrade [发布目录]           升级到指定发布目录
  update [选项]                从 GitHub Release 在线更新
  check-update                 检查 GitHub Release 最新版本
  rollback                     回滚程序和数据库（保留二次确认）
  status                       显示安装状态
  start|stop|restart           管理 worker 服务
  service-status               显示 systemd 服务状态
  logs                         跟踪 worker 日志
  build                        从源码构建发布包
  test                         运行 worker 单元测试
  deployment-test [测试选项]  一键验收已部署的 worker（源码仓库）
  -h, --help                   显示帮助

发布目录必须是已解压、包含 manifest.json 和 neu-box-install 的目录。
在发布目录中运行本脚本时，install/upgrade 默认使用当前目录。

在线更新选项:
  --version VERSION            更新到指定版本（默认 latest）
  --yes                        跳过更新确认
  --force                      允许更新到数值上更旧的指定版本

可用 NEU_BOX_RELEASE_REPOSITORY 和 NEU_BOX_RELEASE_BASE_URL 覆盖下载源。
EOF
}

as_root() {
    if ((EUID == 0)); then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo -- "$@"
    else
        echo "error: 此操作需要 root，且系统没有 sudo" >&2
        return 1
    fi
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "error: 未找到命令: $1" >&2
        return 1
    fi
}

resolve_release_source() {
    local candidate="${1:-}"
    if [[ -z "$candidate" && -f "$SCRIPT_DIR/manifest.json" ]]; then
        candidate="$SCRIPT_DIR"
    fi
    if [[ -z "$candidate" ]]; then
        read -r -e -p "请输入已解压发布目录: " candidate
    fi
    if [[ ! -d "$candidate" ]]; then
        echo "error: 发布目录不存在: $candidate" >&2
        return 1
    fi
    candidate="$(cd -- "$candidate" && pwd -P)"
    if [[ ! -f "$candidate/manifest.json" ]]; then
        echo "error: 发布目录缺少 manifest.json: $candidate" >&2
        return 1
    fi
    if [[ ! -x "$candidate/neu-box-install" ]]; then
        echo "error: 发布目录缺少可执行的 neu-box-install: $candidate" >&2
        return 1
    fi
    printf '%s\n' "$candidate"
}

deploy_release() {
    local action="$1"
    local source
    source="$(resolve_release_source "${2:-}")"
    as_root "$source/neu-box-install" \
        "$action" --role worker --source "$source"
}

installed_installer() {
    if [[ -x "$INSTALLED_INSTALLER" ]]; then
        printf '%s\n' "$INSTALLED_INSTALLER"
        return 0
    fi
    if [[ -x "/opt/neu-box/current/neu-box-install" ]]; then
        printf '%s\n' "/opt/neu-box/current/neu-box-install"
        return 0
    fi
    echo "error: 尚未找到已安装的 neu-box-install" >&2
    return 1
}

rollback_release() {
    local installer
    installer="$(installed_installer)"
    as_root "$installer" rollback
}

deployment_status() {
    local installer
    installer="$(installed_installer)"
    as_root "$installer" status
}

service_action() {
    local action="$1"
    require_command systemctl
    as_root systemctl "$action" "$SERVICE_NAME"
}

service_status() {
    require_command systemctl
    as_root systemctl --no-pager --full status "$SERVICE_NAME"
}

service_logs() {
    require_command journalctl
    as_root journalctl -u "$SERVICE_NAME" -n 100 -f
}

require_source_tree() {
    if [[ ! -f "$SCRIPT_DIR/pyproject.toml" || ! -f "$SCRIPT_DIR/deploy/build_release.py" ]]; then
        echo "error: 此命令只能在 Neu Box 源码仓库中运行" >&2
        return 1
    fi
}

build_release() {
    require_source_tree
    require_command uv
    cd "$SCRIPT_DIR"
    unset VIRTUAL_ENV || true
    uv run --frozen --group build deploy/build_release.py
}

run_tests() {
    require_source_tree
    require_command uv
    cd "$SCRIPT_DIR"
    unset VIRTUAL_ENV || true
    uv run --frozen pytest -q tests/unit
}

run_deployment_test() {
    require_source_tree
    if [[ ! -x "$SCRIPT_DIR/tests/test_deployment.sh" ]]; then
        echo "error: 缺少可执行测试脚本: $SCRIPT_DIR/tests/test_deployment.sh" >&2
        return 1
    fi
    cd "$SCRIPT_DIR"
    "$SCRIPT_DIR/tests/test_deployment.sh" "$@"
}

release_architecture() {
    local machine
    machine="$(uname -m)"
    case "${machine,,}" in
        x86_64|amd64) printf 'amd64\n' ;;
        aarch64|arm64) printf 'arm64\n' ;;
        *)
            echo "error: 不支持的系统架构: $machine" >&2
            return 1
            ;;
    esac
}

installed_version() {
    local installer="$1"
    local status_output version
    if [[ -r "$CURRENT_MANIFEST" ]]; then
        status_output="$(<"$CURRENT_MANIFEST")"
        version="$(sed -n \
            's/.*"version":[[:space:]]*"\([^"]*\)".*/\1/p' \
            <<<"$status_output" | head -n 1)"
    else
        if ! status_output="$("$installer" status)"; then
            echo "error: 无法读取当前安装状态" >&2
            return 1
        fi
        version="$(sed -n \
            's/.*"current_version":[[:space:]]*"\([^"]*\)".*/\1/p' \
            <<<"$status_output" | head -n 1)"
    fi
    if [[ -z "$version" ]]; then
        echo "error: 尚未安装 Neu Box，在线更新只适用于已安装节点" >&2
        return 1
    fi
    printf '%s\n' "$version"
}

version_is_older() {
    local candidate="$1"
    local current="$2"
    local first
    if [[ ! "$candidate" =~ ^[0-9]+([.][0-9A-Za-z+-]+)*$ ]] \
        || [[ ! "$current" =~ ^[0-9]+([.][0-9A-Za-z+-]+)*$ ]]; then
        return 1
    fi
    first="$(printf '%s\n%s\n' "$candidate" "$current" | sort -V | head -n 1)"
    [[ "$candidate" != "$current" && "$first" == "$candidate" ]]
}

resolve_release_tag() {
    local requested_version="$1"
    local base_url="${RELEASE_BASE_URL%/}"
    local repository="$RELEASE_REPOSITORY"
    local effective tag
    local -a protocol_args=()

    if [[ ! "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
        echo "error: 无效的 GitHub 仓库名: $repository" >&2
        return 1
    fi
    if [[ "$base_url" == https://* ]]; then
        protocol_args+=(--proto '=https' --proto-redir '=https')
    fi

    if [[ -n "$requested_version" ]]; then
        if [[ "$requested_version" == v* ]]; then
            tag="$requested_version"
        else
            tag="v$requested_version"
        fi
    else
        echo "正在查询 GitHub latest Release..." >&2
        if ! effective="$(curl \
            --fail --silent --show-error --location --head \
            --connect-timeout 10 --max-time 60 --retry 2 --retry-delay 1 \
            "${protocol_args[@]}" \
            --output /dev/null --write-out '%{url_effective}' \
            "$base_url/$repository/releases/latest")"; then
            echo "error: 查询 GitHub latest Release 失败" >&2
            return 1
        fi
        effective="${effective%%\?*}"
        effective="${effective%/}"
        tag="${effective##*/}"
        if [[ "$tag" == 'latest' ]]; then
            echo "error: GitHub latest Release 没有重定向到具体版本" >&2
            return 1
        fi
    fi

    if [[ ! "$tag" =~ ^v?[A-Za-z0-9][A-Za-z0-9._+-]*$ ]]; then
        echo "error: GitHub Release tag 不安全或格式无效: $tag" >&2
        return 1
    fi
    printf '%s\n' "$tag"
}

verify_download_checksum() {
    local archive="$1"
    local checksum_file="$2"
    local asset_name="$3"
    local -a lines=()
    local expected listed extra actual ignored

    mapfile -t lines < <(sed '/^[[:space:]]*$/d' "$checksum_file")
    if ((${#lines[@]} != 1)); then
        echo "error: Release checksum 文件必须且只能包含一条记录" >&2
        return 1
    fi
    read -r expected listed extra <<<"${lines[0]}"
    listed="${listed#\*}"
    if [[ ! "$expected" =~ ^[0-9A-Fa-f]{64}$ ]] \
        || [[ "$listed" != "$asset_name" ]] \
        || [[ -n "${extra:-}" ]]; then
        echo "error: Release checksum 文件格式或文件名不匹配" >&2
        return 1
    fi
    read -r actual ignored < <(sha256sum "$archive")
    if [[ "${actual,,}" != "${expected,,}" ]]; then
        echo "error: Release 压缩包 SHA256 校验失败" >&2
        return 1
    fi
    printf 'SHA256 校验通过: %s\n' "$asset_name"
}

extract_release_safely() {
    local archive="$1"
    local extract_dir="$2"
    local expected_root="$3"
    local listing="$extract_dir.members"
    local member normalized
    local count=0

    if ! tar -tzf "$archive" >"$listing"; then
        echo "error: Release 压缩包损坏或不是 tar.gz" >&2
        return 1
    fi
    while IFS= read -r member; do
        count=$((count + 1))
        if [[ -z "$member" || "$member" == /* ]]; then
            echo "error: Release 压缩包包含无效路径" >&2
            return 1
        fi
        normalized="${member#./}"
        if [[ "/$normalized/" == *'/../'* ]] \
            || [[ "$normalized" != "$expected_root" \
                && "$normalized" != "$expected_root/" \
                && "$normalized" != "$expected_root/"* ]]; then
            echo "error: Release 压缩包包含越界或非预期顶层路径: $member" >&2
            return 1
        fi
    done <"$listing"
    if ((count == 0)); then
        echo "error: Release 压缩包为空" >&2
        return 1
    fi

    mkdir -p "$extract_dir"
    if ! tar --extract --gzip --file "$archive" --directory "$extract_dir" \
        --no-same-owner --no-same-permissions; then
        echo "error: 解压 Release 失败" >&2
        return 1
    fi
}

online_update() (
    set -Eeuo pipefail
    local requested_version=''
    local assume_yes=0
    local force=0
    local check_only=0
    local installer current tag version architecture asset_name checksum_name
    local base_url release_url update_tmp archive checksum_file release_root release_dir
    local answer

    while (($#)); do
        case "$1" in
            --version)
                (($# >= 2)) || { echo "error: --version 缺少参数" >&2; return 2; }
                requested_version="$2"
                shift 2
                ;;
            --yes|-y)
                assume_yes=1
                shift
                ;;
            --force)
                force=1
                shift
                ;;
            --check-only)
                check_only=1
                shift
                ;;
            -h|--help)
                sed -n '/^在线更新选项:/,/^$/p' < <(usage)
                return 0
                ;;
            *)
                echo "error: 未知在线更新参数: $1" >&2
                return 2
                ;;
        esac
    done

    require_command curl
    require_command tar
    require_command sha256sum
    require_command sed
    require_command sort
    installer="$(installed_installer)"
    current="$(installed_version "$installer")"
    tag="$(resolve_release_tag "$requested_version")"
    version="${tag#v}"
    if [[ ! "$version" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]]; then
        echo "error: 无效的 Release 版本: $version" >&2
        return 1
    fi

    printf '当前版本: %s\n' "$current"
    printf '目标版本: %s (%s)\n' "$version" "$tag"
    if [[ "$version" == "$current" ]]; then
        echo "当前已经是目标版本，无需更新。"
        return 0
    fi
    if version_is_older "$version" "$current" && ((force == 0)); then
        echo "error: 目标版本 $version 旧于当前版本 $current；如确需降级请增加 --force" >&2
        return 1
    fi
    if ((check_only)); then
        echo "发现可用版本: $version"
        return 0
    fi

    if ((assume_yes == 0)); then
        if [[ ! -t 0 ]]; then
            echo "error: 非交互在线更新必须增加 --yes" >&2
            return 1
        fi
        read -r -p "将从 $current 在线更新到 $version，确认继续？[y/N] " answer
        case "$answer" in
            y|Y|yes|YES) ;;
            *) echo "已取消"; return 0 ;;
        esac
    fi

    architecture="$(release_architecture)"
    asset_name="neu-box-$version-linux-$architecture.tar.gz"
    checksum_name="$asset_name.sha256"
    base_url="${RELEASE_BASE_URL%/}"
    release_url="$base_url/$RELEASE_REPOSITORY/releases/download/$tag"
    update_tmp="$(mktemp -d "${TMPDIR:-/tmp}/neu-box-update.XXXXXX")"
    trap 'rm -rf -- "$update_tmp"' EXIT
    archive="$update_tmp/$asset_name"
    checksum_file="$update_tmp/$checksum_name"
    local -a protocol_args=()
    if [[ "$base_url" == https://* ]]; then
        protocol_args+=(--proto '=https' --proto-redir '=https')
    fi

    printf '正在下载 %s...\n' "$asset_name"
    curl --fail --silent --show-error --location \
        --connect-timeout 10 --max-time 1800 --retry 2 --retry-delay 1 \
        "${protocol_args[@]}" --output "$archive" \
        "$release_url/$asset_name"
    curl --fail --silent --show-error --location \
        --connect-timeout 10 --max-time 120 --retry 2 --retry-delay 1 \
        "${protocol_args[@]}" --output "$checksum_file" \
        "$release_url/$checksum_name"
    verify_download_checksum "$archive" "$checksum_file" "$asset_name"

    release_root="neu-box-$version-linux-$architecture"
    extract_release_safely "$archive" "$update_tmp/extracted" "$release_root"
    release_dir="$(resolve_release_source "$update_tmp/extracted/$release_root")"
    printf '发布包已验证，开始执行升级: %s\n' "$release_dir"
    # 使用本机已安装且受信任的安装器先验证发布目录，再执行迁移与切换；
    # 新包内的安装器只会在整个升级成功后由旧安装器原子替换。
    as_root "$installer" \
        upgrade --role worker --source "$release_dir"
    printf '在线更新完成: %s -> %s\n' "$current" "$version"
)

confirm_deploy() {
    local action="$1"
    local source
    source="$(resolve_release_source "${2:-}")"
    printf '即将执行 %s，发布目录: %s\n' "$action" "$source"
    read -r -p "确认继续？[y/N] " answer
    case "$answer" in
        y|Y|yes|YES)
            deploy_release "$action" "$source"
            ;;
        *)
            echo "已取消"
            ;;
    esac
}

pause_menu() {
    echo
    read -r -p "按 Enter 返回菜单..." _unused
}

run_menu_action() {
    if "$@"; then
        echo
        echo "操作完成"
    else
        local exit_code=$?
        echo
        echo "操作失败（exit=$exit_code）" >&2
    fi
    pause_menu
}

menu() {
    while true; do
        clear 2>/dev/null || true
        cat <<'EOF'
========================================
          Neu Box Worker 管理
========================================
  1) 首次安装
  2) 升级
  3) 回滚
  4) 查看安装状态
  5) 启动服务
  6) 停止服务
  7) 重启服务
  8) 查看服务状态
  9) 跟踪服务日志
 10) 构建发布包（源码仓库）
 11) 运行单元测试（源码仓库）
 12) 一键验收已部署 Worker（源码仓库）
 13) 从 GitHub Release 在线更新
  0) 退出
========================================
EOF
        read -r -p "请选择: " choice
        case "$choice" in
            1) run_menu_action confirm_deploy install ;;
            2) run_menu_action confirm_deploy upgrade ;;
            3) run_menu_action rollback_release ;;
            4) run_menu_action deployment_status ;;
            5) run_menu_action service_action start ;;
            6) run_menu_action service_action stop ;;
            7) run_menu_action service_action restart ;;
            8) run_menu_action service_status ;;
            9) run_menu_action service_logs ;;
            10) run_menu_action build_release ;;
            11) run_menu_action run_tests ;;
            12) run_menu_action run_deployment_test ;;
            13) run_menu_action online_update ;;
            0) return 0 ;;
            *)
                echo "无效选项: $choice" >&2
                pause_menu
                ;;
        esac
    done
}

main() {
    local command="${1:-menu}"
    if (($#)); then
        shift
    fi

    case "$command" in
        menu) menu ;;
        install|upgrade) deploy_release "$command" "${1:-}" ;;
        update) online_update "$@" ;;
        check-update) online_update --check-only "$@" ;;
        rollback) rollback_release ;;
        status) deployment_status ;;
        start|stop|restart) service_action "$command" ;;
        service-status) service_status ;;
        logs) service_logs ;;
        build) build_release ;;
        test) run_tests ;;
        deployment-test) run_deployment_test "$@" ;;
        -h|--help|help) usage ;;
        *)
            echo "error: 未知命令: $command" >&2
            usage >&2
            return 2
            ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
