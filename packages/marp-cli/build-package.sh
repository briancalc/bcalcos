#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEBIAN_DIR="$SCRIPT_DIR/debian"

command -v dpkg-buildpackage >/dev/null 2>&1 || {
    echo "ERROR: dpkg-buildpackage not found." >&2
    exit 1
}

command -v dpkg-parsechangelog >/dev/null 2>&1 || {
    echo "ERROR: dpkg-parsechangelog not found." >&2
    exit 1
}

command -v curl >/dev/null 2>&1 || {
    echo "ERROR: curl not found." >&2
    exit 1
}

command -v sha256sum >/dev/null 2>&1 || {
    echo "ERROR: sha256sum not found." >&2
    exit 1
}

PACKAGE=$(dpkg-parsechangelog -l "$DEBIAN_DIR/changelog" -S Source)
VERSION=$(dpkg-parsechangelog -l "$DEBIAN_DIR/changelog" -S Version)
UPSTREAM_VERSION=$(printf '%s\n' "$VERSION" | sed 's/-[0-9][0-9]*$//')

if [ -z "$PACKAGE" ] || [ -z "$VERSION" ] || [ -z "$UPSTREAM_VERSION" ]; then
    echo "ERROR: Could not determine package version information." >&2
    exit 1
fi

EXPECTED_UPSTREAM_VERSION="4.5.0"

if [ "$UPSTREAM_VERSION" != "$EXPECTED_UPSTREAM_VERSION" ]; then
    echo "ERROR: This build script is for Marp $EXPECTED_UPSTREAM_VERSION." >&2
    echo "       Changelog specifies Marp $UPSTREAM_VERSION." >&2
    exit 1
fi

DOWNLOAD_URL="https://github.com/marp-team/marp-cli/releases/download/v${UPSTREAM_VERSION}/marp-cli-v${UPSTREAM_VERSION}-linux.tar.gz"

EXPECTED_SHA256="ae46ff054919dccd20f2402fd1747a2363ab2dc5a652f2d589df211ed5e26d88"

BUILD_ROOT=$(mktemp -d "/tmp/${PACKAGE}-build-XXXXXX")
SOURCE_DIR="$BUILD_ROOT/${PACKAGE}-${UPSTREAM_VERSION}"
DOWNLOAD_FILE="$BUILD_ROOT/marp-cli-v${UPSTREAM_VERSION}-linux.tar.gz"

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

mkdir -p "$SOURCE_DIR/usr/bin"

echo "==> Package:         $PACKAGE"
echo "==> Version:         $VERSION"
echo "==> Upstream:        $UPSTREAM_VERSION"
echo "==> Build workspace: $BUILD_ROOT"
echo

echo "==> Downloading Marp CLI..."

curl -fL --retry 3 -o "$DOWNLOAD_FILE" "$DOWNLOAD_URL"

echo "==> Verifying SHA256..."

ACTUAL_SHA256=$(sha256sum "$DOWNLOAD_FILE" | awk '{print $1}')

if [ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]; then
    echo "ERROR: SHA256 verification failed." >&2
    echo "Expected: $EXPECTED_SHA256" >&2
    echo "Actual:   $ACTUAL_SHA256" >&2
    exit 1
fi

echo "==> SHA256 verified."

echo "==> Extracting Marp CLI..."

tar -xzf "$DOWNLOAD_FILE" -C "$BUILD_ROOT"

MARP_BINARY=$(find "$BUILD_ROOT" -type f -name marp -perm -u+x | head -n 1)

if [ -z "$MARP_BINARY" ]; then
    echo "ERROR: Could not find Marp executable in release archive." >&2
    exit 1
fi

echo "==> Marp executable: $MARP_BINARY"

cp "$MARP_BINARY" "$SOURCE_DIR/usr/bin/marp"
chmod 0755 "$SOURCE_DIR/usr/bin/marp"

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
