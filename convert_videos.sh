#!/usr/bin/env bash

#===============================================================================
# Docflix Media Suite — CLI Batch Converter (H.265/HEVC)
# Converts all MKV files in current directory to optimized H.265 format
# Supports CPU (libx265) and GPU encoding (NVIDIA NVENC, Intel QSV, AMD VAAPI)
#===============================================================================

set -euo pipefail

# Configuration
BITRATE="2M"
CRF=""  # Empty = use bitrate mode, set value (0-51) for CRF mode
PRESET="ultrafast"
AUDIO_CODEC="copy"
OUTPUT_SUFFIX="-2mbps-UF_265"
LOG_FILE="video_convert_$(date +%Y%m%d_%H%M%S).log"
SKIP_EXISTING=true
CLEANUP_ORIGINALS=false
GPU_BACKEND=""          # '' = CPU, 'nvenc', 'qsv', or 'vaapi'
GPU_PRESET="p1"         # NVENC: p1-p7, QSV: veryfast-veryslow
GPU_SHORT=""            # Short label for output suffix (NVENC, QSV, VAAPI)

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

#-------------------------------------------------------------------------------
# Logging functions
#-------------------------------------------------------------------------------
log() {
    local level="$1"
    shift
    local message="$*"
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo -e "${timestamp} [${level}] ${message}" | tee -a "$LOG_FILE"
}

log_info()    { log "${BLUE}INFO${NC}"    "$@"; }
log_success() { log "${GREEN}SUCCESS${NC}" "$@"; }
log_warning() { log "${YELLOW}WARNING${NC}" "$@"; }
log_error()   { log "${RED}ERROR${NC}"    "$@"; }

#-------------------------------------------------------------------------------
# Check prerequisites and GPU availability
#-------------------------------------------------------------------------------
check_prerequisites() {
    # Check if ffmpeg is installed
    if ! command -v ffmpeg &> /dev/null; then
        log_error "ffmpeg is not installed. Please install it first."
        log_error "  Ubuntu/Debian: sudo apt install ffmpeg"
        log_error "  Fedora: sudo dnf install ffmpeg"
        log_error "  macOS: brew install ffmpeg"
        exit 1
    fi

    # Check if zenity is available (optional)
    if ! command -v zenity &> /dev/null; then
        log_warning "zenity not found. Will use terminal notifications instead."
        USE_ZENITY=false
    else
        USE_ZENITY=true
    fi

    log_info "Prerequisites check passed"
    log_info "ffmpeg version: $(ffmpeg -version | head -n1)"

    # Check GPU availability if a GPU backend was requested
    if [[ -n "$GPU_BACKEND" ]]; then
        local encoder_list
        encoder_list=$(ffmpeg -encoders 2>&1)

        case "$GPU_BACKEND" in
            nvenc)
                if echo "$encoder_list" | grep -qE "(h265_nvenc|hevc_nvenc)"; then
                    log_success "NVIDIA GPU encoder (hevc_nvenc) detected"
                    log_info "GPU preset: ${GPU_PRESET} (p1=fastest, p7=best quality)"
                    if command -v nvidia-smi &> /dev/null; then
                        local gpu_name
                        gpu_name=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1)
                        log_info "GPU: ${gpu_name}"
                    fi
                else
                    log_error "NVIDIA GPU encoder (hevc_nvenc) not available in ffmpeg"
                    log_error "Falling back to CPU encoding (libx265)"
                    GPU_BACKEND=""
                fi
                ;;
            qsv)
                if echo "$encoder_list" | grep -q "hevc_qsv"; then
                    log_success "Intel QSV encoder (hevc_qsv) detected"
                    log_info "QSV preset: ${GPU_PRESET}"
                else
                    log_error "Intel QSV encoder (hevc_qsv) not available in ffmpeg"
                    log_error "Falling back to CPU encoding (libx265)"
                    GPU_BACKEND=""
                fi
                ;;
            vaapi)
                if echo "$encoder_list" | grep -q "hevc_vaapi"; then
                    log_success "VAAPI encoder (hevc_vaapi) detected"
                    if [[ ! -e /dev/dri/renderD128 ]]; then
                        log_warning "/dev/dri/renderD128 not found — VAAPI may fail"
                    fi
                else
                    log_error "VAAPI encoder (hevc_vaapi) not available in ffmpeg"
                    log_error "Falling back to CPU encoding (libx265)"
                    GPU_BACKEND=""
                fi
                ;;
            *)
                log_error "Unknown GPU backend: ${GPU_BACKEND}"
                log_error "Supported backends: nvenc, qsv, vaapi"
                log_error "Falling back to CPU encoding (libx265)"
                GPU_BACKEND=""
                ;;
        esac
    fi
}

#-------------------------------------------------------------------------------
# Show usage
#-------------------------------------------------------------------------------
show_usage() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Convert all MKV files in current directory to H.265/HEVC format.
Supports CPU (libx265) and GPU encoding (NVIDIA NVENC, Intel QSV, AMD VAAPI).

Options:
    -b, --bitrate BIT     Set video bitrate (default: 2M)
    -q, --crf N           Set CRF quality (0-51, lower=better, default: disabled)
                          CPU: 18-28 recommended | GPU: 15-25 recommended
    -p, --preset PRESET   Set CPU ffmpeg preset (default: ultrafast)
    -g, --gpu             Use NVIDIA GPU encoding (NVENC)
    --qsv                 Use Intel Quick Sync Video encoding
    --vaapi               Use VAAPI encoding (AMD / Intel on Linux)
    -P, --gpu-preset N    Set GPU preset (NVENC: p1-p7, QSV: veryfast-veryslow)
    -s, --suffix SUFFIX   Set output filename suffix (default: -2mbps-UF_265)
    -o, --overwrite       Overwrite existing output files (default: skip)
    -c, --cleanup         Delete original files after successful conversion
    -n, --no-log          Disable logging to file
    -h, --help            Show this help message

Examples:
    $(basename "$0")                      # Convert with CPU defaults
    $(basename "$0") -g                   # Use NVIDIA GPU (fastest preset)
    $(basename "$0") -g -P p5             # GPU with balanced quality preset
    $(basename "$0") --qsv                # Use Intel QSV encoding
    $(basename "$0") --vaapi              # Use VAAPI encoding (AMD/Intel)
    $(basename "$0") -b 4M -p slow        # Higher quality CPU encode
    $(basename "$0") -c                   # Convert and cleanup originals
    $(basename "$0") -o                   # Force overwrite existing files

CPU Presets (fastest to best quality):
    ultrafast, superfast, veryfast, faster, fast, medium, slow, slower, veryslow

NVENC GPU Presets (p1=fastest to p7=best quality):
    p1, p2, p3, p4, p5, p6, p7

QSV Presets (fastest to best quality):
    veryfast, faster, fast, medium, slow, slower, veryslow

Note: GPU encoding is significantly faster but may produce slightly larger files
      at equivalent quality. Recommended for batch conversions.
      VAAPI has no user-selectable presets — quality is controlled via bitrate/CRF.

EOF
}

#-------------------------------------------------------------------------------
# Parse command line arguments
#-------------------------------------------------------------------------------
_set_gpu_backend() {
    GPU_BACKEND="$1"
    GPU_SHORT="$2"
    local default_preset="$3"
    if [[ "$GPU_PRESET" == "p1" ]]; then  # only override if still default
        GPU_PRESET="$default_preset"
    fi
}

_update_suffix() {
    # Only update suffix if user hasn't explicitly set one via -s
    [[ "$SUFFIX_EXPLICIT" == true ]] && return
    if [[ -n "$GPU_BACKEND" ]]; then
        local p="$GPU_PRESET"
        [[ -z "$p" ]] && p="default"
        if [[ -n "$CRF" ]]; then
            OUTPUT_SUFFIX="-CRF${CRF}-${GPU_SHORT}_${p}"
        else
            OUTPUT_SUFFIX="-${BITRATE}-${GPU_SHORT}_${p}"
        fi
    else
        if [[ -n "$CRF" ]]; then
            OUTPUT_SUFFIX="-CRF${CRF}-x265_${PRESET}"
        else
            OUTPUT_SUFFIX="-${BITRATE}-UF_265"
        fi
    fi
}

SUFFIX_EXPLICIT=false

parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            -b|--bitrate)
                BITRATE="$2"
                shift 2
                ;;
            -q|--crf)
                CRF="$2"
                shift 2
                ;;
            -p|--preset)
                PRESET="$2"
                shift 2
                ;;
            -g|--gpu)
                _set_gpu_backend "nvenc" "NVENC" "p1"
                shift
                ;;
            --qsv)
                _set_gpu_backend "qsv" "QSV" "medium"
                shift
                ;;
            --vaapi)
                _set_gpu_backend "vaapi" "VAAPI" ""
                shift
                ;;
            -P|--gpu-preset)
                GPU_PRESET="$2"
                shift 2
                ;;
            -s|--suffix)
                OUTPUT_SUFFIX="$2"
                SUFFIX_EXPLICIT=true
                shift 2
                ;;
            -o|--overwrite)
                SKIP_EXISTING=false
                shift
                ;;
            -c|--cleanup)
                CLEANUP_ORIGINALS=true
                shift
                ;;
            -n|--no-log)
                LOG_FILE="/dev/null"
                shift
                ;;
            -h|--help)
                show_usage
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                show_usage
                exit 1
                ;;
        esac
    done
    _update_suffix
}

#-------------------------------------------------------------------------------
# Convert a single video file
#-------------------------------------------------------------------------------
convert_file() {
    local input_file="$1"
    local output_file="$2"
    local file_num="$3"
    local total_files="$4"

    log_info "[${file_num}/${total_files}] Converting: $(basename "$input_file")"
    
    # Show progress
    local progress
    progress=$((file_num * 100 / total_files))
    echo -ne "${BLUE}Progress: ${progress}% (${file_num}/${total_files})${NC}\r"

    # Build encoder command based on GPU backend and CRF/Bitrate
    local encoder_opts
    local mode_desc
    local hwaccel_opts=""

    case "$GPU_BACKEND" in
        nvenc)
            hwaccel_opts="-hwaccel cuda -hwaccel_output_format cuda"
            if [[ -n "$CRF" ]]; then
                encoder_opts="-c:v hevc_nvenc -preset ${GPU_PRESET} -cq ${CRF}"
                mode_desc="NVIDIA (NVENC) CRF ${CRF}"
            else
                encoder_opts="-c:v hevc_nvenc -preset ${GPU_PRESET} -b:v ${BITRATE}"
                mode_desc="NVIDIA (NVENC) ${BITRATE}"
            fi
            ;;
        qsv)
            hwaccel_opts="-hwaccel qsv -hwaccel_output_format qsv"
            if [[ -n "$CRF" ]]; then
                encoder_opts="-c:v hevc_qsv -preset ${GPU_PRESET} -global_quality ${CRF}"
                mode_desc="Intel (QSV) CRF ${CRF}"
            else
                encoder_opts="-c:v hevc_qsv -preset ${GPU_PRESET} -b:v ${BITRATE}"
                mode_desc="Intel (QSV) ${BITRATE}"
            fi
            ;;
        vaapi)
            hwaccel_opts="-hwaccel vaapi -hwaccel_output_format vaapi -vaapi_device /dev/dri/renderD128"
            if [[ -n "$CRF" ]]; then
                encoder_opts="-c:v hevc_vaapi -qp ${CRF}"
                mode_desc="AMD/VAAPI CRF ${CRF}"
            else
                encoder_opts="-c:v hevc_vaapi -b:v ${BITRATE}"
                mode_desc="AMD/VAAPI ${BITRATE}"
            fi
            ;;
        *)
            # CPU x265 encoding
            if [[ -n "$CRF" ]]; then
                encoder_opts="-c:v libx265 -preset ${PRESET} -crf ${CRF}"
                mode_desc="CPU (libx265) CRF ${CRF}"
            else
                encoder_opts="-c:v libx265 -preset ${PRESET} -b:v ${BITRATE} -minrate ${BITRATE} -maxrate ${BITRATE} -bufsize ${BITRATE%M}M"
                mode_desc="CPU (libx265) ${BITRATE}"
            fi
            ;;
    esac

    local preset_label="${GPU_PRESET:-$PRESET}"
    [[ -z "$GPU_BACKEND" ]] && preset_label="$PRESET"
    log_info "Using encoder: ${mode_desc} (preset: ${preset_label})"

    # Convert the video
    if ffmpeg -y $hwaccel_opts -i "$input_file" \
        $encoder_opts \
        -c:a "$AUDIO_CODEC" \
        -stats \
        -progress pipe:1 \
        "$output_file" 2>&1 | tee -a "$LOG_FILE" | grep -E "(frame|fps|size|time|bitrate|speed)" | tail -1; then
        
        log_success "[${file_num}/${total_files}] Successfully converted: $(basename "$output_file")"
        
        # Cleanup original if requested
        if [[ "$CLEANUP_ORIGINALS" == true ]]; then
            log_info "Removing original: $(basename "$input_file")"
            rm -f "$input_file"
        fi
        
        return 0
    else
        log_error "[${file_num}/${total_files}] Failed to convert: $(basename "$input_file")"
        # Remove partial output file if it exists
        rm -f "$output_file"
        return 1
    fi
}

#-------------------------------------------------------------------------------
# Format seconds into human-readable time
#-------------------------------------------------------------------------------
format_time() {
    local total_seconds=$1
    local hours=$((total_seconds / 3600))
    local minutes=$(((total_seconds % 3600) / 60))
    local seconds=$((total_seconds % 60))
    
    if [[ $hours -gt 0 ]]; then
        printf "%dh %dm %ds" $hours $minutes $seconds
    elif [[ $minutes -gt 0 ]]; then
        printf "%dm %ds" $minutes $seconds
    else
        printf "%ds" $seconds
    fi
}

#-------------------------------------------------------------------------------
# Main function
#-------------------------------------------------------------------------------
main() {
    parse_args "$@"
    check_prerequisites

    # Record start time
    local start_time
    start_time=$(date +%s)

    local p_folder="${PWD}"
    local c_folder="${PWD##*/}"

    log_info "========================================="
    log_info "Docflix Media Suite — CLI Converter Started"
    log_info "========================================="
    log_info "Working directory: ${p_folder}"
    case "$GPU_BACKEND" in
        nvenc)  log_info "Encoder: NVIDIA GPU (NVENC)" ;;
        qsv)    log_info "Encoder: Intel (QSV)" ;;
        vaapi)  log_info "Encoder: AMD / VAAPI" ;;
        *)      log_info "Encoder: CPU (libx265)" ;;
    esac
    if [[ -n "$GPU_BACKEND" ]]; then
        log_info "GPU Preset: ${GPU_PRESET:-none}"
    else
        log_info "CPU Preset: ${PRESET}"
    fi
    if [[ -n "$CRF" ]]; then
        log_info "Quality: CRF ${CRF} (constant quality mode)"
    else
        log_info "Bitrate: ${BITRATE} (constant bitrate mode)"
    fi
    log_info "Skip existing: ${SKIP_EXISTING}"
    log_info "Cleanup originals: ${CLEANUP_ORIGINALS}"
    log_info "Log file: ${LOG_FILE}"
    log_info "========================================="

    # Find all MKV files (handle special characters in filenames)
    local files=()
    local skipped=0
    local converted=0
    local failed=0

    # Use nullglob to handle case where no files match
    shopt -s nullglob
    for file in *.mkv *.MKV *.mp4 *.MP4 *.avi *.AVI *.mov *.MOV *.wmv *.WMV *.flv *.FLV *.webm *.WEBM *.ts *.TS *.m2ts *.M2TS *.mts *.MTS; do
        # Skip if it's an output file (already converted)
        if [[ "$file" == *"${OUTPUT_SUFFIX}"* ]]; then
            log_info "Skipping output file: $file"
            continue
        fi
        files+=("$file")
    done
    shopt -u nullglob

    # Check if any files were found
    if [[ ${#files[@]} -eq 0 ]]; then
        log_warning "No video files found in ${p_folder}"

        if [[ "$USE_ZENITY" == true ]]; then
            zenity --warning \
                --title "No Files Found" \
                --text "No video files found in: ${p_folder}" \
                --width=400
        fi
        
        exit 0
    fi

    local total_files=${#files[@]}
    log_info "Found ${total_files} video file(s) to convert"
    echo ""

    # Process each file
    for i in "${!files[@]}"; do
        local input_file="${files[$i]}"
        local base_name="${input_file%.*}"
        local output_file="${base_name}${OUTPUT_SUFFIX}.mkv"
        local file_num=$((i + 1))

        # Check if output already exists
        if [[ -f "$output_file" ]]; then
            if [[ "$SKIP_EXISTING" == true ]]; then
                log_info "[${file_num}/${total_files}] Skipping (output exists): $(basename "$input_file")"
                ((skipped++))
                continue
            else
                log_info "[${file_num}/${total_files}] Overwriting: $(basename "$output_file")"
            fi
        fi

        # Convert the file
        if convert_file "$input_file" "$output_file" "$file_num" "$total_files"; then
            ((converted++))
        else
            ((failed++))
        fi
        
        echo ""  # New line after progress
    done

    # Calculate elapsed time
    local end_time
    end_time=$(date +%s)
    local elapsed_seconds=$((end_time - start_time))
    local elapsed_time
    elapsed_time=$(format_time $elapsed_seconds)

    # Summary
    echo ""
    log_info "========================================="
    log_info "Conversion Summary"
    log_info "========================================="
    log_info "Total files found: ${total_files}"
    log_info "Successfully converted: ${converted}"
    log_info "Skipped (existing): ${skipped}"
    log_info "Failed: ${failed}"
    log_info "Time elapsed: ${elapsed_time}"
    log_info "========================================="

    # Show notification
    local message="Converted: ${converted}/${total_files}\nSkipped: ${skipped}\nFailed: ${failed}\n⏱ Time: ${elapsed_time}\n\nDirectory: ${p_folder}"
    
    if [[ "$failed" -gt 0 ]]; then
        message="${message}\n\n⚠️ Some conversions failed!"
    fi

    if [[ "$USE_ZENITY" == true ]]; then
        if [[ "$failed" -gt 0 ]]; then
            zenity --warning \
                --title "Video Conversion Complete (with errors)" \
                --text "$message" \
                --width=400
        else
            zenity --info \
                --title "Video Conversion Complete" \
                --text "$message" \
                --width=400
        fi
    else
        if [[ "$failed" -gt 0 ]]; then
            log_error "Conversion completed with errors. Check log: ${LOG_FILE}"
        else
            log_success "All conversions completed successfully!"
        fi
    fi

    # Exit with error code if any conversions failed
    if [[ "$failed" -gt 0 ]]; then
        exit 1
    fi

    exit 0
}

# Run main function with all arguments
main "$@"
