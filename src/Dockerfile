FROM python:3.11-slim-bookworm

# Install BCC and kernel headers
RUN apt-get update && apt-get install -y --no-install-recommends \
    bpfcc-tools \
    libbpfcc \
    libbpfcc-dev \
    python3-bpfcc \
    linux-headers-generic \
    kmod \
    bpftool \
    && rm -rf /var/lib/apt/lists/*

# Pre-merge kernel headers at build time for BCC compatibility
# BCC expects: <kdir>/include/, <kdir>/arch/<arch>/include/, <kdir>/arch/<arch>/include/generated/
RUN set -e; \
    MERGED="/opt/kernel-headers"; \
    mkdir -p "$MERGED"; \
    ARCH_HDR=""; COMMON_HDR=""; \
    for d in /usr/src/linux-headers-*; do \
        case "$d" in *common*) [ -d "$d/include/linux" ] && COMMON_HDR="$d" ;; \
                     *) [ -d "$d/include/generated" ] && ARCH_HDR="$d" ;; \
        esac; \
    done; \
    echo "COMMON=$COMMON_HDR ARCH=$ARCH_HDR"; \
    if [ -n "$COMMON_HDR" ]; then \
        cp -a "$COMMON_HDR/include" "$MERGED/include"; \
        cp -a "$COMMON_HDR/arch" "$MERGED/arch"; \
    fi; \
    ARCH=""; \
    case "$(dpkg --print-architecture)" in \
        arm64|aarch64) ARCH="arm64" ;; \
        amd64|x86_64) ARCH="x86" ;; \
    esac; \
    if [ -n "$ARCH" ] && [ -n "$ARCH_HDR" ]; then \
        if [ -d "$ARCH_HDR/arch/$ARCH/include/generated" ]; then \
            mkdir -p "$MERGED/arch/$ARCH/include/generated"; \
            cp -a "$ARCH_HDR/arch/$ARCH/include/generated/"* "$MERGED/arch/$ARCH/include/generated/"; \
        fi; \
        if [ -d "$ARCH_HDR/include/generated" ]; then \
            mkdir -p "$MERGED/include/generated"; \
            cp -an "$ARCH_HDR/include/generated/"* "$MERGED/include/generated/" 2>/dev/null || true; \
        fi; \
        if [ -d "$ARCH_HDR/include/config" ]; then \
            mkdir -p "$MERGED/include/config"; \
            cp -an "$ARCH_HDR/include/config/"* "$MERGED/include/config/" 2>/dev/null || true; \
        fi; \
    fi; \
    if [ -n "$ARCH" ]; then \
        GEN_ASM="$MERGED/arch/$ARCH/include/generated/asm"; \
        mkdir -p "$GEN_ASM"; \
        for hdr in "$MERGED/include/asm-generic/"*.h; do \
            name=$(basename "$hdr"); \
            [ -f "$MERGED/arch/$ARCH/include/asm/$name" ] && continue; \
            [ -f "$GEN_ASM/$name" ] && continue; \
            printf '#include <asm-generic/%s>\n' "$name" > "$GEN_ASM/$name"; \
        done; \
        GEN_UAPI_ASM="$MERGED/arch/$ARCH/include/generated/uapi/asm"; \
        mkdir -p "$GEN_UAPI_ASM"; \
        for hdr in "$MERGED/include/uapi/asm-generic/"*.h; do \
            name=$(basename "$hdr"); \
            [ -f "$MERGED/arch/$ARCH/include/uapi/asm/$name" ] && continue; \
            [ -f "$GEN_UAPI_ASM/$name" ] && continue; \
            printf '#include <asm-generic/%s>\n' "$name" > "$GEN_UAPI_ASM/$name"; \
        done; \
    fi; \
    echo "Kernel headers merged at $MERGED (arch=$ARCH)"

# Make system-installed bcc visible to pip's Python
RUN ln -sf /usr/lib/python3/dist-packages/bcc /usr/local/lib/python3.11/site-packages/bcc

WORKDIR /app

# Copy and install the package
COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir .

# Create data directory
RUN mkdir -p /var/lib/perfcatch

# Copy entrypoint script
COPY src/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Run the agent daemon
CMD ["/entrypoint.sh"]
