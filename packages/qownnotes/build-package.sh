#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEBIAN_DIR="$SCRIPT_DIR/debian"

command -v curl >/dev/null 2>&1 || {
    echo "ERROR: curl not found." >&2
    exit 1
}

command -v dpkg-buildpackage >/dev/null 2>&1 || {
    echo "ERROR: dpkg-buildpackage not found." >&2
    exit 1
}

command -v dpkg-parsechangelog >/dev/null 2>&1 || {
    echo "ERROR: dpkg-parsechangelog not found." >&2
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

EXPECTED_UPSTREAM_VERSION="26.8.11"

if [ "$UPSTREAM_VERSION" != "$EXPECTED_UPSTREAM_VERSION" ]; then
    echo "ERROR: This build script is for QOwnNotes $EXPECTED_UPSTREAM_VERSION." >&2
    echo "       Changelog specifies QOwnNotes $UPSTREAM_VERSION." >&2
    exit 1
fi

EXPECTED_SHA256="d80aae760e20fb092b406cbc76f3f7fc2bd946c5597df6a3f132d31107f56a87"

DOWNLOAD_URL="https://github.com/pbek/QOwnNotes/releases/download/v${UPSTREAM_VERSION}/qownnotes-${UPSTREAM_VERSION}.tar.xz"

BUILD_ROOT=$(mktemp -d "/tmp/${PACKAGE}-build-XXXXXX")
DOWNLOAD_FILE="$BUILD_ROOT/qownnotes-${UPSTREAM_VERSION}.tar.xz"
SOURCE_ARCHIVE="$DOWNLOAD_FILE"
SOURCE_DIR="$BUILD_ROOT/qownnotes-${UPSTREAM_VERSION}"

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

echo "==> Package:         $PACKAGE"
echo "==> Version:         $VERSION"
echo "==> Upstream:        $UPSTREAM_VERSION"
echo "==> Build workspace: $BUILD_ROOT"
echo

echo "==> Downloading QOwnNotes source..."

curl -fL "$DOWNLOAD_URL" -o "$DOWNLOAD_FILE"

echo "==> Verifying source archive SHA256..."

ACTUAL_SHA256=$(sha256sum "$DOWNLOAD_FILE" | awk '{print $1}')

if [ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]; then
    echo "ERROR: SHA256 verification failed." >&2
    echo "Expected: $EXPECTED_SHA256" >&2
    echo "Actual:   $ACTUAL_SHA256" >&2
    exit 1
fi

echo "==> SHA256 verified."

echo "==> Extracting QOwnNotes source..."

tar -xJf "$DOWNLOAD_FILE" -C "$BUILD_ROOT"

SOURCE_ARCHIVE="$BUILD_ROOT/${PACKAGE}_${UPSTREAM_VERSION}.orig.tar.xz"

cp "$DOWNLOAD_FILE" "$SOURCE_ARCHIVE"

echo "==> Installing Debian packaging metadata..."

rm -rf "$SOURCE_DIR/debian"
cp -a "$DEBIAN_DIR" "$SOURCE_DIR/debian"

echo "==> Building Debian package..."

(
    cd "$SOURCE_DIR"
    dpkg-buildpackage -us -uc
)


echo
echo "==> Build completed successfully."
echo
echo "==> Artifacts:"
find "$BUILD_ROOT" -maxdepth 1 -type f -printf '    %f\n' | sort

echo
echo "==> Build workspace:"
echo "    $BUILD_ROOT"
