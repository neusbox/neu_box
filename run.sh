#!/usr/bin/env bash
set -Eeuo pipefail

readonly REPOSITORY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

usage() {
    cat <<'EOF'
用法: ./run.sh <命令> [参数]

源码仓库命令:
  build [构建参数]              构建 Worker RPM
  test [pytest 参数]            运行 Worker 单元测试
  deployment-test [测试参数]    验收已部署的 Worker
  help                          显示帮助

安装、升级和服务管理使用 RPM 与安装后的 /usr/sbin/neu-box；源码入口不承担
已部署系统的生命周期管理。
EOF
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "error: 未找到命令: $1" >&2
        return 1
    fi
}

run_uv() {
    require_command uv
    cd "$REPOSITORY_ROOT"
    unset VIRTUAL_ENV || true
    uv run --frozen "$@"
}

main() {
    local command="${1:-help}"
    if (($#)); then
        shift
    fi

    case "$command" in
        build)
            run_uv --group build deploy/build_release.py "$@"
            ;;
        test)
            run_uv pytest -q tests/unit "$@"
            ;;
        deployment-test)
            exec "$REPOSITORY_ROOT/tests/test_deployment.sh" "$@"
            ;;
        help|-h|--help)
            usage
            ;;
        *)
            echo "error: 未知命令: $command" >&2
            usage >&2
            return 2
            ;;
    esac
}

main "$@"
