#!/bin/bash
# oMLX startup with proper memory management for 48GB Mac
#
# Model sizes (on disk / approximate in-memory):
#   8-bit 27B: 27GB / ~30GB  -> leaves ~13GB for KV cache + OS
#   4-bit 27B: 14GB / ~16GB  -> leaves ~27GB for KV cache + OS
#
# The --hot-cache-max-size flag is CRITICAL: it limits how much KV cache
# lives in GPU memory at once. Without it, oMLX tries to load all cache
# blocks into GPU RAM during prefix reconstruction, causing Metal OOM.
# Remaining cache pages stay on SSD and are paged in on demand (slower
# but prevents crashes).

exec /Users/jlacomba/omlx-venv/bin/omlx serve \
    --model-dir /Users/jlacomba/models \
    --api-key omlx-80ktncu2cdui9fal \
    --max-model-memory 40GB \
    --max-process-memory 85% \
    --hot-cache-max-size 8GB \
    --initial-cache-blocks 64 \
    --paged-ssd-cache-max-size 200GB \
    --max-concurrent-requests 1 \
    --log-level info
