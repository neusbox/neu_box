#!/usr/bin/env bash
# Neu Box Worker 实机一键验收入口。

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOYMENT_DIR="$SCRIPT_DIR/deployment"

WORKER_URL="${NEU_BOX_WORKER_URL:-http://127.0.0.1:59075}"
TEST_USER="${NEU_BOX_TEST_USER:-$(id -un)}"
TASK_TIMEOUT=120
REAPER_TIMEOUT=100
SKIP_DEVICE=0
SKIP_REAPER=0
POLL_INTERVAL=2

usage() {
    cat <<'EOF'
用法: tests/test_deployment.sh [选项]

选项:
  --url URL                 Worker 地址（默认 http://127.0.0.1:59075）
  --user USER               执行任务的系统用户（默认当前用户）
  --timeout SECONDS         单个任务的最长等待时间（默认 120）
  --reaper-timeout SECONDS  Reaper 回收的最长等待时间（默认 100）
  --skip-device             跳过所有真实设备占用及设备隔离测试
  --skip-reaper             跳过父子进程与设备最终回收测试
  -h, --help                显示帮助

环境变量:
  NEU_BOX_WORKER_URL        与 --url 相同
  NEU_BOX_TEST_USER         与 --user 相同

示例:
  sudo -u pengyt ./tests/test_deployment.sh
  ./tests/test_deployment.sh --url http://10.0.0.8:59075 --user pengyt
  ./tests/test_deployment.sh --skip-device
EOF
}

while (($#)); do
    case "$1" in
        --url)
            (($# >= 2)) || { echo "--url 缺少参数" >&2; exit 2; }
            WORKER_URL="$2"
            shift 2
            ;;
        --user)
            (($# >= 2)) || { echo "--user 缺少参数" >&2; exit 2; }
            TEST_USER="$2"
            shift 2
            ;;
        --timeout)
            (($# >= 2)) || { echo "--timeout 缺少参数" >&2; exit 2; }
            TASK_TIMEOUT="$2"
            shift 2
            ;;
        --reaper-timeout)
            (($# >= 2)) || { echo "--reaper-timeout 缺少参数" >&2; exit 2; }
            REAPER_TIMEOUT="$2"
            shift 2
            ;;
        --skip-device)
            SKIP_DEVICE=1
            shift
            ;;
        --skip-reaper)
            SKIP_REAPER=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "未知参数: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ ! "$TASK_TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
    echo "--timeout 必须是正整数" >&2
    exit 2
fi
if [[ ! "$REAPER_TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
    echo "--reaper-timeout 必须是正整数" >&2
    exit 2
fi
if [[ -z "$TEST_USER" ]]; then
    echo "测试用户不能为空" >&2
    exit 2
fi

for module in \
    common.sh \
    cases/core.sh \
    cases/log_streaming.sh \
    cases/resource_limits.sh \
    cases/task_cancel_delete.sh \
    cases/priority.sh \
    cases/device_allocation.sh \
    cases/device_isolation.sh \
    cases/multi_device_concurrency.sh \
    cases/sandbox_lifecycle.sh \
    cases/reaper.sh; do
    module_path="$DEPLOYMENT_DIR/$module"
    if [[ ! -r "$module_path" ]]; then
        echo "验收模块不存在或不可读: $module_path" >&2
        exit 2
    fi
    # shellcheck source=/dev/null
    source "$module_path"
done

deployment_init
print_deployment_header

run_core_tests
run_log_streaming_tests
run_resource_limit_tests
run_task_cancel_delete_tests
run_priority_tests
run_device_allocation_tests
run_device_isolation_tests
run_multi_device_concurrency_tests
run_sandbox_lifecycle_tests
run_reaper_tests

print_deployment_summary
