#!/bin/bash
set -e

KERNEL_VERSION=$(uname -r)
BUILD_DIR="/lib/modules/${KERNEL_VERSION}/build"
PREBUILT_HEADERS="/opt/kernel-headers"

# BCC looks for kernel headers at /lib/modules/<version>/build
# If the hostPath /lib/modules is mounted read-only, we overlay with a writable copy
if [ ! -f "$BUILD_DIR/include/linux/kconfig.h" ]; then
    echo "Setting up kernel headers for BCC (running kernel: ${KERNEL_VERSION})..."

    # Try direct symlink first (works if /lib/modules is writable)
    if mkdir -p "/lib/modules/${KERNEL_VERSION}" 2>/dev/null && \
       rm -f "$BUILD_DIR" 2>/dev/null && \
       ln -sf "$PREBUILT_HEADERS" "$BUILD_DIR" 2>/dev/null; then
        echo "Headers linked: $BUILD_DIR -> $PREBUILT_HEADERS"
    else
        # Read-only mount: use tmpfs overlay
        echo "Host /lib/modules is read-only, creating writable overlay..."
        OVERLAY="/tmp/modules-overlay"
        mkdir -p "$OVERLAY/${KERNEL_VERSION}"
        # Copy existing contents if any
        cp -a /lib/modules/${KERNEL_VERSION}/* "$OVERLAY/${KERNEL_VERSION}/" 2>/dev/null || true
        # Create the symlink in the overlay
        rm -f "$OVERLAY/${KERNEL_VERSION}/build"
        ln -sf "$PREBUILT_HEADERS" "$OVERLAY/${KERNEL_VERSION}/build"
        # Bind mount over the original
        mount --bind "$OVERLAY" /lib/modules
        echo "Headers available via overlay: /lib/modules/${KERNEL_VERSION}/build -> $PREBUILT_HEADERS"
    fi
fi

# Run the agent
exec python -m perfcatch.agent.daemon
