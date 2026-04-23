#!/usr/bin/env bash
# run.sh — Process all CCTV clips through the detection pipeline
# Usage: bash pipeline/run.sh [--api-url http://localhost:8000]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
LAYOUT="$REPO_ROOT/data/store_layout.json"
OUTPUT="$REPO_ROOT/data/events.jsonl"
MODEL="${MODEL_PATH:-$REPO_ROOT/yolov8m.pt}"
API_URL="${API_URL:-}"
EVERY_N=3

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Apex Retail — Store Intelligence Detection Pipeline"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# If CLIPS_DIR is set, process real clips. Otherwise run simulator.
if [[ -n "${CLIPS_DIR:-}" && -d "$CLIPS_DIR" ]]; then
    echo "📹 Processing CCTV clips from: $CLIPS_DIR"
    echo ""

    declare -A STORES=(
        ["STORE_BLR_001"]="BLR_001"
        ["STORE_BLR_002"]="BLR_002"
        ["STORE_MUM_001"]="MUM_001"
        ["STORE_DEL_001"]="DEL_001"
        ["STORE_HYD_001"]="HYD_001"
    )

    declare -A CAMERA_TYPES=(
        ["ENTRY"]="entry"
        ["FLOOR"]="main_floor"
        ["BILLING"]="billing"
    )

    for store_id in "${!STORES[@]}"; do
        store_code="${STORES[$store_id]}"
        for cam_key in "${!CAMERA_TYPES[@]}"; do
            cam_type="${CAMERA_TYPES[$cam_key]}"
            cam_id="CAM_${cam_key}_01"
            clip_file="$CLIPS_DIR/${store_code}_${cam_key}.mp4"

            if [[ ! -f "$clip_file" ]]; then
                echo "  ⚠️  Clip not found: $clip_file — skipping"
                continue
            fi

            echo "  ▶️  $store_id / $cam_id ($cam_type)"
            CLIP_START=$(date -u +"%Y-%m-%dT10:00:00Z")

            python "$SCRIPT_DIR/detect.py" \
                --video "$clip_file" \
                --store-id "$store_id" \
                --camera-id "$cam_id" \
                --camera-type "$cam_type" \
                --layout "$LAYOUT" \
                --output "$OUTPUT" \
                --clip-start "$CLIP_START" \
                --model "$MODEL" \
                --every-n-frames "$EVERY_N" \
                ${API_URL:+--api-url "$API_URL"}

            echo "  ✅ Done: $clip_file"
        done
    done

else
    echo "📊 No CLIPS_DIR set — running synthetic event generator"
    echo "   (Set CLIPS_DIR=/path/to/clips to process real footage)"
    echo ""

    API_ARG=""
    if [[ -n "$API_URL" ]]; then
        API_ARG="--api-url $API_URL"
    fi

    python "$SCRIPT_DIR/simulate.py" \
        --all-stores \
        --duration-minutes 60 \
        --visitors 50 \
        --output "$OUTPUT" \
        $API_ARG

fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Events written to: $OUTPUT"
echo "  To ingest into API: POST /events/ingest"
echo ""
echo "  Example:"
echo "    cat $OUTPUT | jq -c '{events: [.]}' | curl -s -X POST \\"
echo "      http://localhost:8000/events/ingest -H 'Content-Type: application/json' -d @-"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
