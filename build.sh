#!/usr/bin/env bash
# =============================================================================
# build.sh — doris-mcp-server / doris-mcp-client 构建脚本
#
#   构建：  ./build.sh linux-x64        # Linux x86_64
#          ./build.sh linux-arm64      # Linux ARM64 (信创)
#          ./build.sh macos-x64        # macOS Intel
#          ./build.sh macos-arm64      # macOS Apple Silicon
#          ./build.sh                  # 自动检测当前平台
#
#   每次构建产出两个自包含包：
#     dist/doris-mcp-server-{version}-{platform}.tar.gz
#     dist/doris-mcp-client-{version}-{platform}.tar.gz
#
#   清理：  ./build.sh clean
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_NAME="doris-mcp-server"
VERSION="${VERSION:-0.3.0}"
PYTHON_DIR="$SCRIPT_DIR/python"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"
DIST_DIR="$SCRIPT_DIR/dist"

# ── Python Standalone 配置 ───────────────────────────────────────────────
PY_STANDALONE_RELEASE="${PY_STANDALONE_RELEASE:-20250115}"
PY_VERSION="${PY_VERSION:-3.10.16}"

# ── 颜色 ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── 平台解析 ────────────────────────────────────────────────────────────
# 返回 "标签|下载标识"  e.g. "linux-x64|x86_64-unknown-linux-gnu"
_resolve_platform() {
    case "$1" in
        linux-x64)    echo "linux-x64|x86_64-unknown-linux-gnu" ;;
        linux-arm64)  echo "linux-arm64|aarch64-unknown-linux-gnu" ;;
        macos-x64)    echo "macos-x64|x86_64-apple-darwin" ;;
        macos-arm64)  echo "macos-arm64|aarch64-apple-darwin" ;;
        *) _error "Unknown: '$1'. Valid: linux-x64, linux-arm64, macos-x64, macos-arm64"
           exit 1 ;;
    esac
}

_detect_native() {
    local os arch
    case "$(uname -s)" in
        Linux)  os="linux" ;;
        Darwin) os="macos" ;;
        *) _error "Unsupported OS"; exit 1 ;;
    esac
    case "$(uname -m)" in
        x86_64)        arch="x64" ;;
        aarch64|arm64) arch="arm64" ;;
        *) _error "Unsupported arch"; exit 1 ;;
    esac
    echo "${os}-${arch}"
}

# ════════════════════════════════════════════════════════════════════
# _ensure_python — 确保 python/ 有 Python 3.10 + 全部依赖
#
# 优先使用 DORIS_MCP_SYSTEM_PYTHON 指向的已有 Python（如 conda）
# 然后尝试下载 python-build-standalone
# ════════════════════════════════════════════════════════════════════
_ensure_python() {
    local platform="$1"

    if [ -x "$PYTHON_DIR/bin/python3" ] && "$PYTHON_DIR/bin/python3" --version >/dev/null 2>&1; then
        _info "Python ready: $("$PYTHON_DIR/bin/python3" --version 2>&1)"
        return 0
    fi
    if [ -d "$PYTHON_DIR" ]; then
        _warn "Python not runnable on this platform, re-creating..."
        rm -rf "$PYTHON_DIR"
    fi

    # ── Fallback 1: use system/conda Python if provided ──
    if [ -n "${DORIS_MCP_SYSTEM_PYTHON:-}" ] && [ -x "$DORIS_MCP_SYSTEM_PYTHON" ]; then
        _info "Using system Python: $DORIS_MCP_SYSTEM_PYTHON"
        local py_ver
        py_ver=$("$DORIS_MCP_SYSTEM_PYTHON" --version 2>&1)
        _info "Python version: $py_ver"
        
        # Copy real Python files into python/ dir (no symlinks)
        local py_root
        py_root=$(cd $(dirname $(dirname "$DORIS_MCP_SYSTEM_PYTHON")) && pwd)
        _info "Copying Python from $py_root to $PYTHON_DIR ..."
        rm -rf "$PYTHON_DIR"
        mkdir -p "$PYTHON_DIR"
        
        # Copy bin/
        cp -a "$py_root/bin/" "$PYTHON_DIR/bin/"
        # Copy lib/ (excluding heavy test/tkinter/idlelib)
        mkdir -p "$PYTHON_DIR/lib"
        for item in "$py_root/lib/"python* "$py_root/lib/"lib*.so*; do
            [ -e "$item" ] && cp -a "$item" "$PYTHON_DIR/lib/" 2>/dev/null || true
        done
        
        if [ ! -x "$PYTHON_DIR/bin/python3" ]; then
            _error "Failed to setup Python at $PYTHON_DIR/bin/python3"
            exit 1
        fi
        _info "Python $("$PYTHON_DIR/bin/python3" --version) ready"
        
        _info "Installing dependencies ..."
        "$PYTHON_DIR/bin/python3" -m pip install --quiet --upgrade pip 2>/dev/null || true
        "$PYTHON_DIR/bin/python3" -m pip install --quiet -r "$REQUIREMENTS"
        _info "Dependencies installed."
        return 0
    fi

    # ── Fallback 2: download python-build-standalone ──
    local tarball_name="cpython-${PY_VERSION}+${PY_STANDALONE_RELEASE}-${platform}-install_only_stripped.tar.gz"
    local url="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_STANDALONE_RELEASE}/${tarball_name}"

    _info "Downloading Python $PY_VERSION for $platform ..."
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    trap "rm -rf $tmp_dir" EXIT

    local tarball="$tmp_dir/$tarball_name"
    if command -v curl > /dev/null 2>&1; then
        curl -fsSL --connect-timeout 30 --max-time 600 -o "$tarball" "$url" || {
            _error "Download failed: $url"
            _error "Tip: set DORIS_MCP_SYSTEM_PYTHON=/path/to/python3.10 to use a local Python"
            exit 1
        }
    elif command -v wget > /dev/null 2>&1; then
        wget -q --timeout=30 --tries=3 -O "$tarball" "$url" || {
            _error "Download failed: $url"
            _error "Tip: set DORIS_MCP_SYSTEM_PYTHON=/path/to/python3.10 to use a local Python"
            exit 1
        }
    else
        _error "Need curl or wget"
        exit 1
    fi

    if [ ! -f "$tarball" ] || [ ! -s "$tarball" ]; then
        _error "Download failed: $url"
        _error "Tip: set DORIS_MCP_SYSTEM_PYTHON=/path/to/python3.10 to use a local Python"
        exit 1
    fi

    _info "Extracting ..."
    rm -rf "$PYTHON_DIR"
    mkdir -p "$PYTHON_DIR"
    tar xzf "$tarball" -C "$PYTHON_DIR" --strip-components=1

    if [ ! -x "$PYTHON_DIR/bin/python3" ] && [ -x "$PYTHON_DIR/bin/python3.10" ]; then
        ln -sf python3.10 "$PYTHON_DIR/bin/python3"
    fi
    if [ ! -x "$PYTHON_DIR/bin/python3" ]; then
        _error "Python binary not found"; exit 1
    fi
    _info "Python $("$PYTHON_DIR/bin/python3" --version) ready"

    if ! "$PYTHON_DIR/bin/python3" -m pip --version >/dev/null 2>&1; then
        "$PYTHON_DIR/bin/python3" -m ensurepip --upgrade 2>/dev/null || true
    fi
    _info "Installing dependencies ..."
    "$PYTHON_DIR/bin/python3" -m pip install --quiet --upgrade pip 2>/dev/null || true
    "$PYTHON_DIR/bin/python3" -m pip install --quiet -r "$REQUIREMENTS"
    _info "Dependencies installed."
}

# ═════════════════════════════════════════════════════════════════════════
# _pack — 打包单个目标
# ═════════════════════════════════════════════════════════════════════════
_pack() {
    local name="$1"         # doris-mcp-server or doris-mcp-client
    local platform="$2"
    shift 2                 # 剩下的参数是需要打包的额外路径
    local pkg_name="${name}-${VERSION}-${platform}"
    local outfile="$DIST_DIR/${pkg_name}.tar.gz"

    _info "Packing: ${pkg_name}.tar.gz"

    local parent_dir base_name
    parent_dir="$(dirname "$SCRIPT_DIR")"
    base_name="$(basename "$SCRIPT_DIR")"

    cd "$parent_dir"
    tar czf "$outfile" \
        --exclude='.git' \
        --exclude='.gitignore' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='*.pyo' \
        --exclude='dist' \
        --exclude='build' \
        --exclude='*.egg-info' \
        --exclude='.DS_Store' \
        --exclude='python/include' \
        --exclude='python/share' \
        --exclude='python/lib/python3.10/test' \
        --exclude='python/lib/python3.10/idlelib' \
        --exclude='python/lib/python3.10/turtledemo' \
        --exclude='python/lib/python3.10/tkinter' \
        --exclude='python/lib/python3.10/ensurepip' \
        "$base_name/"python \
        "$@"

    local size
    size="$(du -sh "$outfile" | cut -f1)"
    echo "        ${pkg_name}.tar.gz  (${size})"
}

# ═════════════════════════════════════════════════════════════════════════
# build — 构建 server + client 两个包
# ═════════════════════════════════════════════════════════════════════════
build() {
    local platform_label="${1%%|*}"
    local platform_url="${1##*|}"

    _ensure_python "$platform_url"

    rm -rf "$DIST_DIR"
    mkdir -p "$DIST_DIR"

    local parent_dir base_name
    parent_dir="$(dirname "$SCRIPT_DIR")"
    base_name="$(basename "$SCRIPT_DIR")"

    # ── Server 包 ──
    _pack "doris-mcp-server" "$platform_label" \
        "$base_name/"src \
        "$base_name/"mcp-server.toml \
        "$base_name/"start-mcp-server.sh

    # ── Client 包 ──
    _pack "doris-mcp-client" "$platform_label" \
        "$base_name/"mcp-client \
        "$base_name/"mcp-client.sh

    echo ""
    echo "  ────────────────────────────────────────────"
    echo "  Build complete!  Platform: $platform_label"
    echo ""
    echo "  Server:  tar xzf doris-mcp-server-${VERSION}-${platform_label}.tar.gz"
    echo "           cd ${base_name} && ./start-mcp-server.sh"
    echo ""
    echo "  Client:  tar xzf doris-mcp-client-${VERSION}-${platform_label}.tar.gz"
    echo "           cd ${base_name} && ./mcp-client.sh ..."
    echo ""
    echo "  No network, no pip, no system Python needed."
    echo "  ────────────────────────────────────────────"
}

# ═════════════════════════════════════════════════════════════════════════
# clean
# ═════════════════════════════════════════════════════════════════════════
do_clean() {
    rm -rf "$DIST_DIR" "$SCRIPT_DIR/build" "$SCRIPT_DIR"/*.egg-info
    rm -rf "$PYTHON_DIR"
    _info "Cleaned: dist/, python/, build artifacts."
}

# ═════════════════════════════════════════════════════════════════════════
# main
# ═════════════════════════════════════════════════════════════════════════
case "${1:-}" in
    linux-x64|linux-arm64|macos-x64|macos-arm64)
        build "$(_resolve_platform "$1")"
        ;;
    clean)
        do_clean
        ;;
    ""|build)
        _native="$(_detect_native)"
        _info "Auto-detected: $_native"
        build "$(_resolve_platform "$_native")"
        ;;
    *)
        echo "Usage: $0 [linux-x64|linux-arm64|macos-x64|macos-arm64|clean]"
        echo ""
        echo "  linux-x64     Linux x86_64"
        echo "  linux-arm64   Linux ARM64"
        echo "  macos-x64     macOS Intel"
        echo "  macos-arm64   macOS Apple Silicon"
        echo "  clean         Remove build artifacts and python/"
        echo ""
        echo "  No argument = auto-detect and build"
        echo ""
        echo "  Produces two packages in dist/:"
        echo "    doris-mcp-server-{version}-{platform}.tar.gz"
        echo "    doris-mcp-client-{version}-{platform}.tar.gz"
        exit 1
        ;;
esac
