#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
DEBIAN_DIR="$SCRIPT_DIR/debian"

command -v dpkg-buildpackage >/dev/null 2>&1 || {
    echo "ERROR: dpkg-buildpackage not found." >&2
    exit 1
}

command -v dpkg-parsechangelog >/dev/null 2>&1 || {
    echo "ERROR: dpkg-parsechangelog not found." >&2
    exit 1
}

PACKAGE=$(dpkg-parsechangelog -l "$DEBIAN_DIR/changelog" -S Source)
VERSION=$(dpkg-parsechangelog -l "$DEBIAN_DIR/changelog" -S Version)
UPSTREAM_VERSION=$(printf '%s\n' "$VERSION" | sed 's/-[0-9][0-9]*$//')

if [ -z "$PACKAGE" ] || [ -z "$VERSION" ] || [ -z "$UPSTREAM_VERSION" ]; then
    echo "ERROR: Could not determine package version information." >&2
    exit 1
fi

BUILD_ROOT=$(mktemp -d "/tmp/${PACKAGE}-build-XXXXXX")
SOURCE_DIR="$BUILD_ROOT/${PACKAGE}-${UPSTREAM_VERSION}"

cleanup()
{
    status=$?
    if [ "$status" -ne 0 ]; then
        echo >&2
        echo "Build failed; removing temporary workspace:" >&2
        echo "  $BUILD_ROOT" >&2
        rm -rf "$BUILD_ROOT"
    fi
    exit "$status"
}

trap cleanup EXIT

mkdir -p "$SOURCE_DIR"

echo "==> Package:         $PACKAGE"
echo "==> Version:         $VERSION"
echo "==> Upstream:        $UPSTREAM_VERSION"
echo "==> Repository:      $REPO_ROOT"
echo "==> Build workspace: $BUILD_ROOT"
echo

echo "==> Copying source tree..."

(
    cd "$REPO_ROOT"

    tar \
        --exclude='./.git' \
        --exclude='./packages/bcalcos-base/debian' \
        -cf - .
) | tar -xf - -C "$SOURCE_DIR"

echo "==> Creating upstream source archive..."

tar -C "$BUILD_ROOT" -cJf \
    "$BUILD_ROOT/${PACKAGE}_${UPSTREAM_VERSION}.orig.tar.xz" \
    "${PACKAGE}-${UPSTREAM_VERSION}"

echo "==> Copying Debian packaging metadata..."

cp -a "$DEBIAN_DIR" "$SOURCE_DIR/debian"

echo "==> Building Debian package..."

(
    cd "$SOURCE_DIR"
    dpkg-buildpackage -us -uc
)

trap - EXIT

echo
echo "==> Build completed successfully."
echo
echo "==> Artifacts:"
find "$BUILD_ROOT" -maxdepth 1 -type f -printf '    %f\n' | sort

echo
echo "==> Build workspace:"
echo "    $BUILD_ROOT"
