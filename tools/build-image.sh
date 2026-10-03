#!/usr/bin/env bash
# 构建 new-api 镜像。
#
# 版本号直接取自 VERSION 文件（例如 v1.0.0-rc.41+zt.1），构建时不做任何派生，
# 所以直接跑 docker build 也能得到正确的版本号。发布新一版时手工把末尾计数器加一。
#
# 用法:
#   tools/build-image.sh                            给 x86_64 构建，载入本地 docker
#   tools/build-image.sh --dry-run                  只打印版本号和 tag，不构建
#   tools/build-image.sh --platform linux/arm64     给 arm64 构建
#   tools/build-image.sh --tar                      额外导出 dist/<tag>.tar，便于传到服务器
#   tools/build-image.sh --push --registry harbor.example.com/library
#
# 构建后确认镜像里记录的版本号:
#   docker image inspect <tag> \
#     --format '{{index .Config.Labels "org.opencontainers.image.version"}}'

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

PLATFORM="linux/amd64"
TAG=""
REGISTRY=""
PUSH=""
TAR=""
TAR_DIR="dist"
NO_CACHE=""
DRY_RUN=""

usage() {
  cat <<'EOF'
用法: tools/build-image.sh [选项]

  --platform PLATFORM   目标平台，默认 linux/amd64（x86_64 服务器）
  --tag TAG             镜像 tag，默认由 VERSION 内容生成（+ 换成 -）
  --registry REGISTRY   镜像仓库前缀，例如 harbor.example.com/library
  --push                构建后推送到仓库（默认只载入本地 docker）
  --tar                 构建后另存为 tar（默认写到 dist/，文件名带版本号）
  --tar-dir DIR         tar 输出目录，默认 dist/
  --no-cache            不使用构建缓存
  --dry-run             只打印将要使用的版本号和 tag，不执行构建
  -h, --help            显示本帮助
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform) PLATFORM="${2:?--platform 需要参数}"; shift 2 ;;
    --tag)      TAG="${2:?--tag 需要参数}"; shift 2 ;;
    --registry) REGISTRY="${2:?--registry 需要参数}"; shift 2 ;;
    --push)     PUSH=1; shift ;;
    --tar)      TAR=1; shift ;;
    --tar-dir)  TAR_DIR="${2:?--tar-dir 需要参数}"; shift 2 ;;
    --no-cache) NO_CACHE=1; shift ;;
    --dry-run)  DRY_RUN=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

cd "$REPO"

FULL="$(tr -d '[:space:]' < VERSION)"
REVISION="$(git rev-parse HEAD)"

# 前端「可更新」检测用这个正则解析版本号：自定义后缀必须写在 + 后面。
# 写到 - 后面（例如 v1.0.0-rc-41.1-zt）会让解析返回 null，徽标静默失效、不再提示更新。
if ! printf '%s\n' "$FULL" | grep -Eq \
  '^v?[0-9]+(\.[0-9]+){2,}(-(alpha|beta|rc|patch)(\.[0-9]+)?(-i18nfix\.[0-9]+)?)?(\+[0-9a-zA-Z.-]+)?$'; then
  echo "VERSION 里的 '$FULL' 不是前端可解析的版本号格式。" >&2
  echo "自定义后缀要放在 + 后面，例如 v1.0.0-rc.41+zt.1。" >&2
  exit 2
fi

# Docker tag 不接受 + 号，换成 -
: "${TAG:=new-api:$(printf '%s' "$FULL" | tr '+' '-')}"
[[ -n "$REGISTRY" ]] && TAG="${REGISTRY%/}/${TAG}"

# tar 文件名从 tag 派生（去掉 / 和 :），所以导出物自带版本号，多个版本不会混淆。
TAR_PATH=""
if [[ -n "$TAR" ]]; then
  TAR_PATH="${TAR_DIR}/$(printf '%s' "$TAG" | tr '/:' '--').tar"
fi

# 多平台产物无法载入本地 docker 守护进程，只能推送。
if [[ "$PLATFORM" == *,* && -z "$PUSH" ]]; then
  echo "多平台构建不能配合 --load 使用，请加 --push，或把 --platform 收窄成单个平台。" >&2
  exit 2
fi

# docker save 只能作用于本地镜像，推送到仓库后本地不一定留副本。
if [[ -n "$TAR" && -n "$PUSH" ]]; then
  echo "--tar 需要镜像留在本地 docker 里，不能和 --push 同时使用。" >&2
  exit 2
fi

echo "版本号   : $FULL"
echo "镜像 tag : $TAG"
echo "目标平台 : $PLATFORM"
[[ -n "$TAR_PATH" ]] && echo "导出 tar : $TAR_PATH"
echo

if [[ -n "$DRY_RUN" ]]; then
  echo "--dry-run：未执行构建。"
  exit 0
fi

OUTPUT_FLAG=("--load")
[[ -n "$PUSH" ]] && OUTPUT_FLAG=("--push")

BUILD_ARGS=(
  --platform "$PLATFORM"
  "${OUTPUT_FLAG[@]}"
  --label "org.opencontainers.image.version=$FULL"
  --label "org.opencontainers.image.revision=$REVISION"
  -t "$TAG"
)
[[ -n "$NO_CACHE" ]] && BUILD_ARGS+=(--no-cache)

echo "开始构建 $(date '+%H:%M:%S')"
docker buildx build "${BUILD_ARGS[@]}" .
echo "构建结束 $(date '+%H:%M:%S')"

if [[ -z "$PUSH" ]]; then
  echo
  echo "镜像内记录的版本号："
  docker image inspect "$TAG" \
    --format '  {{index .Config.Labels "org.opencontainers.image.version"}}'
fi

if [[ -n "$TAR_PATH" ]]; then
  mkdir -p "$TAR_DIR"
  echo
  echo "导出镜像 tar："
  docker save "$TAG" -o "$TAR_PATH"
  echo "  $TAR_PATH  $(du -h "$TAR_PATH" | cut -f1)"
  echo "  传到服务器后：docker load -i $(basename "$TAR_PATH")"
fi
