#!/bin/sh
# neubox 的独立构建入口（随源码构建，不进发布包）。
# 产物: ./neubox（不入库，不生成校验和）。
#
#   ./scripts/build.sh                  本机架构
#   GOARCH=arm64 ./scripts/build.sh     交叉构建
#   NEUBOX_GO=$HOME/go/bin/go ./scripts/build.sh
#
# 版本号与 Worker 同源：取自仓库根 src/neu_box/__init__.py 的 __version__，
# 发布包构建（deploy/build_release.py）注入的是同一个值，客户端不再独立发版。
#
# 工具链要求由 go.mod 的 go 指令保证；GOTOOLCHAIN=local 禁止自动下载新工具链，
# 所以过旧的 go 会直接构建失败，而不是悄悄换一个版本去编译。
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_ROOT="$(cd "$ROOT/../.." && pwd)"
cd "$ROOT"

GO_BIN="${NEUBOX_GO:-go}"
TARGET=./cmd/neubox

if ! command -v "$GO_BIN" >/dev/null 2>&1; then
    echo "error: 找不到 $GO_BIN（可用 NEUBOX_GO 指定路径）" >&2
    exit 1
fi

VERSION_FILE="$REPO_ROOT/src/neu_box/__init__.py"
if [ ! -f "$VERSION_FILE" ]; then
    echo "error: 找不到 Worker 版本文件: $VERSION_FILE" >&2
    exit 1
fi
VERSION="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$VERSION_FILE" | head -n 1 | tr -d '[:space:]')"
if [ -z "$VERSION" ]; then
    echo "error: 无法从 $VERSION_FILE 解析 __version__" >&2
    exit 1
fi

ARCH="${GOARCH:-$("$GO_BIN" env GOARCH)}"
# version 变量住在 internal/cli，-X 必须给完整 import 路径。
VERSION_SYMBOL=github.com/neusbox/neu_box/client/neubox/internal/cli.version

CGO_ENABLED=0 GOOS=linux GOARCH="$ARCH" GOTOOLCHAIN=local \
    "$GO_BIN" build -trimpath -buildvcs=false -tags=netgo,osusergo \
    -ldflags "-s -w -X $VERSION_SYMBOL=$VERSION" \
    -o neubox "$TARGET"
