#!/bin/bash

set -u

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m'

p_folder="${PWD}"
c_folder="${PWD##*/}"
shopt -s nullglob

# Configurable: number of parallel jobs (set to CPU cores)
MAX_JOBS=${MAX_JOBS:-$(nproc)}

############################
#Dependency Check          ##
############################

missing=()
for cmd in ffmpeg mkvmerge mkvpropedit mediainfo zenity; do
    command -v "$cmd" &>/dev/null || missing+=("$cmd")
done

if [ ${#missing[@]} -gt 0 ]; then
    echo -e "${RED}Missing required tools: ${missing[*]}${NC}"
    echo ""
    echo "Install with:"
    echo "  sudo apt install ffmpeg mkvtoolnix mediainfo zenity"
    exit 1
fi

#######################
#Convert Audio to AC3##
#######################

convert-audio-file () {
    local mkvfile="$1"
    local AC="ac-3" AC1="E-AC-3" AC2="AC-3"
    
    [ -f "$mkvfile" ] || return 0
    
    local acodec1
    acodec1=$(mediainfo "$mkvfile" "--output=Audio;%Format%" 2>/dev/null)
    
    if [ "$acodec1" = "$AC1" ] || [ "$acodec1" = "$AC" ] || [ "$acodec1" = "$AC2" ]; then
        cp "$mkvfile" "tmp/${mkvfile%.*}.mkv"
        echo -e "${GREEN}[SKIP]${NC} ${mkvfile} (already AC-3)"
    else
        echo -e "${YELLOW}[CONV]${NC} ${mkvfile}"
        ffmpeg -y -loglevel error -i "$mkvfile" -map 0 -vcodec copy -scodec copy -acodec ac3 -b:a 384k "tmp/${mkvfile%.*}.mkv"
    fi
}

convert-audio () {
    local files=()
    for f in *.mkv *.mp4; do
        [ -f "$f" ] && files+=("$f")
    done
    
    if [ ${#files[@]} -eq 0 ]; then
        echo -e "${RED}No video files found${NC}"
        return 1
    fi
    
    # Process files in parallel
    local running=0
    for file in "${files[@]}"; do
        convert-audio-file "$file" &
        ((running++))
        
        if [ $running -ge $MAX_JOBS ]; then
            wait -n  # Wait for any job to finish
            ((running--))
        fi
    done
    wait  # Wait for all remaining jobs
}

#############################
#Apply All Metadata in One Pass##
#############################

apply-metadata-file () {
    local mkvfile="$1"
    [ -f "$mkvfile" ] || return 0
    
    mkvpropedit "$mkvfile" \
        -e track:a1 -s language="eng" -s name="" \
        -e track:v1 -s language="und" -s name="" \
        -e track:s1 -s language="eng" -s name="English" \
        -e info -s title="" 2>/dev/null
    
    echo -e "${GREEN}[META]${NC} ${mkvfile}"
}

apply-metadata () {
    local files=()
    for f in *.mkv; do
        [ -f "$f" ] && files+=("$f")
    done
    
    local running=0
    for file in "${files[@]}"; do
        apply-metadata-file "$file" &
        ((running++))
        
        if [ $running -ge $MAX_JOBS ]; then
            wait -n
            ((running--))
        fi
    done
    wait
}

#######################################################
#Remove all tags, global tags, subtitles and chapters##
#######################################################

remove-tags-file () {
    local file="$1"
    [ -f "$file" ] || return 0
    
    mkvmerge -q -o "tmp/${file}" --no-track-tags --no-subtitles --no-global-tags --no-chapters "$file" 2>/dev/null
    echo -e "${GREEN}[TAGS]${NC} ${file}"
}

remove-tags () {
    mkdir -p tmp
    local files=()
    for f in *.mkv; do
        [ -f "$f" ] && files+=("$f")
    done
    
    local running=0
    for file in "${files[@]}"; do
        remove-tags-file "$file" &
        ((running++))
        
        if [ $running -ge $MAX_JOBS ]; then
            wait -n
            ((running--))
        fi
    done
    wait
}

####################################
#Rename Final Video File          ##
####################################

ren-final-vid () {

    for f in *.mkv; do
        [ -f "$f" ] && mv "$f" "${f%.mkv}_265.mkv"
    done
}

######################################################
#Mux MKV video file and Main Subtitle (srt) together##
######################################################

mux-rt-file () {
    local main="$1"
    [ -f "$main" ] || return 0
    
    local base1="${main%.mkv}"
    local srt1="${base1}.eng.srt"
    
    if [ -f "$srt1" ]; then
        mkvmerge -q -o "tmp/${base1}.mkv" -S "$main" "$srt1" 2>/dev/null
        echo -e "${GREEN}[MUX]${NC} ${main} + ${srt1}"
    else
        # No subtitle - just copy the file
        cp "$main" "tmp/${base1}.mkv"
        echo -e "${YELLOW}[NO SUB]${NC} ${main}"
    fi
}

mux-rt-files () {
    mkdir -p tmp
    local files=()
    for f in *.mkv; do
        [ -f "$f" ] && files+=("$f")
    done
    
    local running=0
    for file in "${files[@]}"; do
        mux-rt-file "$file" &
        ((running++))
        
        if [ $running -ge $MAX_JOBS ]; then
            wait -n
            ((running--))
        fi
    done
    wait
}

########################################################
#Mux MKV video file and Forced Subtitle (srt) together##
########################################################

mux-forced-file () {
    local main="$1"
    [ -f "$main" ] || return 0

    local base1="${main%.mkv}"
    local srt2="${base1}.eng.forced.srt"

    if [ -f "$srt2" ]; then
        # Output directly as .mkv (no intermediate .forced.mkv rename needed)
        if mkvmerge -o "tmp/${base1}.mkv" "$main" \
            --language 0:en \
            --track-name 0:Forced \
            --forced-display-flag 0:yes \
            "$srt2"; then
            echo -e "${GREEN}[MUX-F]${NC} ${main} + ${srt2}"
        else
            echo -e "${RED}[MUX-F FAILED]${NC} ${main} — copying without forced sub"
            cp "$main" "tmp/${base1}.mkv"
        fi
    else
        echo -e "${YELLOW}[NO FORCED SUB]${NC} ${main} — no file: ${srt2}"
        cp "$main" "tmp/${base1}.mkv"
    fi
}

mux-forced-files () {
    mkdir -p tmp
    local files=()
    for f in *.mkv; do
        [ -f "$f" ] && files+=("$f")
    done

    local running=0
    for file in "${files[@]}"; do
        mux-forced-file "$file" &
        ((running++))

        if [ $running -ge $MAX_JOBS ]; then
            wait -n
            ((running--))
        fi
    done
    wait
}

#######################
#Pre-flight File Check##
#######################

preflight-check () {
    local errors=0
    local warnings=0
    local log_file="${p_folder}/media-preflight.log"
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')

    # Helper: write to terminal, and only write FAIL/WARN to log file
    # tlog TYPE MESSAGE
    tlog () {
        local type="$1"; shift
        local msg="$*"
        case "$type" in
            FAIL)  echo -e "    ${RED}[FAIL]${NC} ${msg}" ;;
            WARN)  echo -e "    ${YELLOW}[WARN]${NC} ${msg}" ;;
            INFO)  echo -e "    ${YELLOW}[INFO]${NC} ${msg}" ;;
            OK)    echo -e "    ${GREEN}[OK]${NC}   ${msg}" ;;
            HEAD)  echo -e "${YELLOW}${msg}${NC}" ;;
            GHEAD) echo -e "${GREEN}${msg}${NC}" ;;
            RHEAD) echo -e "${RED}${msg}${NC}" ;;
            FILE)  echo -e "${YELLOW}  ── ${msg}${NC}" ;;
        esac
        # Only write warnings and errors to the log file
        if [[ "$type" == "FAIL" || "$type" == "WARN" ]]; then
            echo "  [${type}] ${msg}" >> "$log_file"
        fi
    }

    # Initialise log file
    {
        echo "════════════════════════════════════════════════════════"
        echo "  Media Processor — Pre-flight Check"
        echo "  Directory : ${p_folder}"
        echo "  Started   : ${timestamp}"
        echo "════════════════════════════════════════════════════════"
        echo ""
    } > "$log_file"

    echo -e "${YELLOW}═══════════════════════════════════════════════════════${NC}"
    echo -e "${YELLOW}  Pre-flight Check - Directory: ${c_folder}${NC}"
    echo -e "${YELLOW}═══════════════════════════════════════════════════════${NC}"
    echo ""

    # Collect video files
    local video_files=()
    for f in *.mkv *.mp4; do
        [ -f "$f" ] && video_files+=("$f")
    done

    if [ ${#video_files[@]} -eq 0 ]; then
        echo -e "${RED}  [FAIL] No video files (.mkv or .mp4) found${NC}"
        echo "  [FAIL] No video files (.mkv or .mp4) found" >> "$log_file"
        echo ""
        return 1
    fi

    echo -e "${GREEN}  [OK]${NC} Found ${#video_files[@]} video file(s)"
    echo ""

    for vid in "${video_files[@]}"; do
        local base="${vid%.*}"
        local file_ok=1
        local file_logged=0

        # Helper: write filename to log once before first FAIL/WARN for this file
        log_filename () {
            if [ $file_logged -eq 0 ]; then
                echo "  ── ${vid}" >> "$log_file"
                file_logged=1
            fi
        }

        echo -e "${YELLOW}  ── ${vid}${NC}"

        # Check 1: file is readable
        if [ ! -r "$vid" ]; then
            log_filename
            tlog FAIL "File is not readable"
            ((errors++)); file_ok=0
        fi

        # Check 2: file is not empty
        if [ ! -s "$vid" ]; then
            log_filename
            tlog FAIL "File is empty (0 bytes)"
            ((errors++)); file_ok=0
        fi

        # Check 3: filename must not contain problematic characters
        if echo "$vid" | grep -qP '[^\x00-\x7E]|[`$\\]'; then
            log_filename
            tlog FAIL "Filename contains unsupported characters"
            ((errors++)); file_ok=0
        fi

        # Check 4: filename should not already end in _265
        if [[ "$base" == *_265 ]]; then
            log_filename
            tlog FAIL "File appears already processed (_265 suffix found)"
            ((errors++)); file_ok=0
        fi

        # Check 5: leftover tmp file from a previous run
        if [ -f "tmp/${base}.mkv" ]; then
            log_filename
            tlog WARN "tmp/${base}.mkv already exists — leftover from a previous run?"
            ((warnings++))
        fi

        # Check 6: main subtitle file
        local srt="${base}.eng.srt"
        if [ -f "$srt" ]; then
            if [ ! -s "$srt" ]; then
                log_filename
                tlog FAIL "${srt} exists but is empty"
                ((errors++))
            else
                tlog OK "Main subtitle: ${srt}"
            fi
        else
            log_filename
            tlog WARN "No main subtitle found: ${srt}"
            ((warnings++))
        fi

        # Check 7: mediainfo can read the audio stream
        local acodec
        acodec=$(mediainfo "$vid" "--output=Audio;%Format%" 2>/dev/null)
        if [ -z "$acodec" ]; then
            log_filename
            tlog WARN "mediainfo could not read audio stream"
            ((warnings++))
        else
            tlog OK "Audio codec: ${acodec}"
        fi

        echo ""
    done

    # Summary
    local summary_time
    summary_time=$(date '+%Y-%m-%d %H:%M:%S')

    echo -e "${YELLOW}═══════════════════════════════════════════════════════${NC}"

    if [ $errors -gt 0 ]; then
        {
            echo "════════════════════════════════════════════════════════"
            echo "  Completed : ${summary_time}"
            echo "  Result    : FAILED — ${errors} error(s), ${warnings} warning(s)"
            echo "════════════════════════════════════════════════════════"
        } >> "$log_file"

        echo -e "${RED}  Pre-flight FAILED — ${errors} error(s), ${warnings} warning(s)${NC}"
        echo -e "${RED}  Fix the errors above before processing.${NC}"
        echo -e "${RED}  Log written to: ${log_file}${NC}"
        echo -e "${YELLOW}═══════════════════════════════════════════════════════${NC}"
        echo ""
        zenity --error \
            --title "Pre-flight Check Failed" \
            --text "Found ${errors} error(s) in ${c_folder}.\n\nFix the errors listed in:\n${log_file}" \
            --width=500 2>/dev/null || true
        return 1

    elif [ $warnings -gt 0 ]; then
        {
            echo "════════════════════════════════════════════════════════"
            echo "  Completed : ${summary_time}"
            echo "  Result    : PASSED with ${warnings} warning(s)"
            echo "════════════════════════════════════════════════════════"
        } >> "$log_file"

        echo -e "${GREEN}  Pre-flight PASSED${NC} — ${warnings} warning(s)"
        echo -e "${YELLOW}  Log written to: ${log_file}${NC}"
        echo -e "${YELLOW}═══════════════════════════════════════════════════════${NC}"
        echo ""
        if ! zenity --question \
            --title "Pre-flight Warnings" \
            --text "Pre-flight check passed with ${warnings} warning(s).\n\nSee log for details:\n${log_file}\n\nContinue processing?" \
            --width=500 2>/dev/null; then
            echo -e "${YELLOW}Processing cancelled by user.${NC}"
            echo "  Processing cancelled by user." >> "$log_file"
            return 1
        fi

    else
        # No issues — delete the log file, nothing to report
        rm -f "$log_file"
        echo -e "${GREEN}  Pre-flight PASSED — all checks OK${NC}"
        echo -e "${YELLOW}═══════════════════════════════════════════════════════${NC}"
        echo ""
    fi

    return 0
}

#######################
#Main Script Commands##
#######################

echo -e "${YELLOW}═══════════════════════════════════════════════════════${NC}"
echo -e "${YELLOW}  Media Processor - Directory: ${c_folder}${NC}"
echo -e "${YELLOW}  Parallel Jobs: ${MAX_JOBS}${NC}"
echo -e "${YELLOW}═══════════════════════════════════════════════════════${NC}"
echo ""

# Run pre-flight check — abort if it fails
preflight-check || exit 1

# Count video files for progress reporting
video_count=0
for f in *.mkv *.mp4; do
    [ -f "$f" ] && ((video_count++))
done

echo -e "${YELLOW}Processing ${video_count} file(s)...${NC}"
echo ""

echo -e "${YELLOW}[1/7] Converting Audio to AC-3${NC}"
mkdir -p tmp
convert-audio

if [ -z "$(ls -A tmp 2>/dev/null)" ]; then
    echo -e "${RED}Error: No files processed${NC}"
    rmdir tmp 2>/dev/null
    exit 1
fi

mv tmp/* .
rmdir tmp
echo ""

echo -e "${YELLOW}[2/7] Applying Metadata (all tracks in one pass)${NC}"
apply-metadata
echo ""

echo -e "${YELLOW}[3/7] Removing Tags, Subtitles and Chapters${NC}"
remove-tags
mv -f tmp/* .
rmdir tmp
echo ""

echo -e "${YELLOW}[4/7] Muxing Main Subtitles${NC}"
mux-rt-files
mv -f tmp/* . 2>/dev/null
rmdir tmp
echo ""

echo -e "${YELLOW}[5/7] Muxing Forced Subtitles${NC}"
mux-forced-files
mv -f tmp/* . 2>/dev/null
rmdir tmp 2>/dev/null
echo ""

echo -e "${YELLOW}[6/7] Renaming Final Files${NC}"
ren-final-vid
echo ""

echo -e "${YELLOW}[7/7] Setting Subtitle Names (post-mux)${NC}"
apply-metadata
echo ""

echo -e "${YELLOW}Cleaning up subtitle files...${NC}"
rm -f *.srt

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  COMPLETE - ${video_count} file(s) processed${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════${NC}"

zenity --info --title "Video Converted" --text "Processed ${video_count} file(s) in ${c_folder}\nDirectory: ${p_folder}" --width=400 2>/dev/null || true
