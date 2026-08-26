#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SERVICE_NAME="neu-box-worker.service"
INSTALLED_INSTALLER="/usr/local/sbin/neu-box-install"

usage() {
    cat <<'EOF'
用法: ./run.sh [命令] [参数]

不带命令时进入 Neu Box 管理菜单。

命令:
  menu                         打开交互菜单
  install [发布目录]           首次安装 worker
  upgrade [发布目录]           升级到指定发布目录
  rollback                     回滚程序和数据库（保留二次确认）
  status                       显示安装状态
  start|stop|restart           管理 worker 服务
  service-status               显示 systemd 服务状态
  logs                         跟踪 worker 日志
  build                        从源码构建发布包
  test                         运行 worker 单元测试
  -h, --help                   显示帮助

发布目录必须是已解压、包含 manifest.json 和 neu-box-install 的目录。
在发布目录中运行本脚本时，install/upgrade 默认使用当前目录。
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
            0) return 0 ;;
            *)
                echo "无效选项: $choice" >&2
                pause_menu
                ;;
        esac
    done
}

command="${1:-menu}"
if (($#)); then
    shift
fi

case "$command" in
    menu) menu ;;
    install|upgrade) deploy_release "$command" "${1:-}" ;;
    rollback) rollback_release ;;
    status) deployment_status ;;
    start|stop|restart) service_action "$command" ;;
    service-status) service_status ;;
    logs) service_logs ;;
    build) build_release ;;
    test) run_tests ;;
    -h|--help|help) usage ;;
    *)
        echo "error: 未知命令: $command" >&2
        usage >&2
        exit 2
        ;;
esac
