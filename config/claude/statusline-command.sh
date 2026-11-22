#!/usr/bin/env bash
# Claude Code status line starting point: labeled fields in Catppuccin Mocha colors. Copy it; setup never installs it.
# shellcheck disable=SC1003 # The art uses literal backslashes in single quotes.
input=$(cat)
field() { echo "$input" | jq -r "$1 // empty"; }

cwd=$(field '.workspace.current_dir // .cwd')
model=$(field '.model.display_name')
effort=$(field '.effort.level')
context=$(field '.context_window.used_percentage')
cost=$(field '.cost.total_cost_usd')
five_hour=$(field '.rate_limits.five_hour.used_percentage')
seven_day=$(field '.rate_limits.seven_day.used_percentage')
five_hour_reset=$(field '.rate_limits.five_hour.resets_at')
weekly_reset=$(field '.rate_limits.seven_day.resets_at')

# Saves the account's limits for skills that pace long runs, which can't see them. Sessions hold readings of different ages,
# so each limit keeps the freshest one: a missing figure or an earlier resets_at changes nothing, otherwise the new reading wins.
# A session renders right after its own reply, so its reading is current; within one window it can drop (a plan upgrade resets it).
if [ -n "$five_hour$seven_day" ]; then
  usage=~/.claude/usage.json
  echo "$input" | jq -c --arg saved "$(cat "$usage" 2>/dev/null)" '
    (($saved | try fromjson catch null | objects) // {}) as $old
    | def freshest($name):
        {used: .rate_limits[$name].used_percentage, resets: .rate_limits[$name].resets_at} as $new
        | {used: $old[$name], resets: $old[$name + "_resets_at"]} as $kept
        | if $new.used == null or ($kept.resets != null and $new.resets < $kept.resets) then $kept else $new end;
      freshest("five_hour") as $five | freshest("seven_day") as $seven
    | {five_hour: $five.used, seven_day: $seven.used, five_hour_resets_at: $five.resets, seven_day_resets_at: $seven.resets,
       at: (now | floor)}' > "$usage.$$" && mv "$usage.$$" "$usage"
fi

# Catppuccin Mocha
blue='137;180;250'; green='166;227;161'; mauve='203;166;247'; overlay='108;112;134'
peach='250;179;135'; red='243;139;168'; teal='148;226;213'; yellow='249;226;175'

c() { printf '\e[38;2;%sm%s\e[0m' "$1" "$2"; }
label() { c "$overlay" "$1: "; }
# Percentages go green, then yellow at 50%, then red at 80%.
pct() {
  local n color
  n=$(printf '%.0f' "$1")
  color=$green; [ "$n" -ge 50 ] && color=$yellow; [ "$n" -ge 80 ] && color=$red
  c "$color" "$n%"
}

# Prints " (resets in 2d 3h)" for a resets_at in Unix seconds or ISO time, or nothing when unknown or past.
reset_in() {
  local at="$1" left d h m
  [ -n "$at" ] || return
  [[ "$at" =~ ^[0-9]+$ ]] || at=$(date -d "$at" +%s 2>/dev/null)
  left=$(( ${at:-0} - $(date +%s) ))
  [ "$left" -gt 0 ] || return
  d=$((left / 86400)); h=$((left % 86400 / 3600)); m=$((left % 3600 / 60))
  if [ "$d" -gt 0 ]; then left="${d}d ${h}h"; elif [ "$h" -gt 0 ]; then left="${h}h ${m}m"; else left="${m}m"; fi
  printf ' %s%s%s' "$(c "$overlay" "(resets in ")" "$(c "$teal" "$left")" "$(c "$overlay" ")")"
}

parts=()
add() { parts+=("$1"); }

top=$(git -C "$cwd" --no-optional-locks rev-parse --show-toplevel 2>/dev/null)
dir=$(basename "${top:-$cwd}")
branch=$(git -C "$cwd" --no-optional-locks symbolic-ref --short -q HEAD 2>/dev/null)
if [ -n "$branch" ]; then
  dirty=''
  [ -n "$(git -C "$cwd" --no-optional-locks status --porcelain 2>/dev/null)" ] && dirty=", $(c "$peach" "uncommitted changes")"
  add "$(c "$blue" "$dir") $(c "$overlay" "(")$(label branch)$(c "$teal" "$branch")$dirty$(c "$overlay" ")")"
else
  add "$(c "$blue" "$dir")"
fi

[ -n "$model" ] && add "$(c "$mauve" "$model")${effort:+$(c "$overlay" ", ")$(c "$mauve" "$effort")}"
[ -n "$context" ] && add "$(label 'ctx used')$(pct "$context")"
[ -n "$cost" ] && add "$(label 'sess. cost')$(c "$peach" "$(printf '$%.2f' "$cost")")"
# Session tokens from the transcript, one entry per API message. "up" leaves out input read from the prompt cache.
transcript=$(field '.transcript_path')
if [ -f "$transcript" ]; then
  read -r up down < <(jq -rs '[.[] | select(.message.usage) | {id: .message.id, u: .message.usage}] | unique_by(.id) | map(.u)
    | "\(map(.input_tokens + (.cache_creation_input_tokens // 0)) | add // 0) \(map(.output_tokens) | add // 0)"' "$transcript" 2>/dev/null)
  human() { awk -v n="$1" 'BEGIN { if (n >= 1e6) printf "%.1fM", n / 1e6; else if (n >= 1e3) printf "%.1fk", n / 1e3; else printf "%d", n }'; }
  [ -n "$up" ] && add "$(label 'tokens up')$(c "$yellow" "$(human "$up")")$(c "$overlay" ", ")$(label down)$(c "$yellow" "$(human "$down")")"
fi
first_parts=("${parts[@]}")
parts=()
[ -n "$five_hour" ] && add "$(label '5-hr limit used')$(pct "$five_hour")$(reset_in "$five_hour_reset")"
[ -n "$seven_day" ] && add "$(label 'weekly limit used')$(pct "$seven_day")$(reset_in "$weekly_reset")"

sep=$(c "$overlay" ' | ')
join() { local out="$1"; shift; for p in "$@"; do out="$out$sep$p"; done; printf '%s' "$out"; }
line1=$(join "${first_parts[@]}")
line2=''
[ "${#parts[@]}" -gt 0 ] && line2=$(join "${parts[@]}")

# A holiday animation on the far right, one frame per second. settings.json re-runs this every second.
# Each month has one theme. Frames cycle by second, and each color list cycles with them.
white='205;214;244'
frame=$(date +%s)
# shellcheck disable=SC2034 # pick reads the arrays by name.
case $(date +%m) in
  01) # New Year's Day: champagne bubbles
    tops=('  \°.°/' '  \.°./' '  \°°./' '  \.°°/'); bottoms=('   _|_')
    top_colors=("$yellow"); bottom_colors=("$white") ;;
  02) # Valentine's Day: a beating heart
    tops=('  (\/)' '  (\/)' '   ♥'); bottoms=('   \/' '   \/' '')
    top_colors=("$red"); bottom_colors=("$red") ;;
  03) # St. Patrick's Day: gold sparkling over a pot between shamrocks
    tops=('  *°*°*' '  °*°*°'); bottoms=('♣ \___/ ♣' ' ♣\___/♣')
    top_colors=("$yellow"); bottom_colors=("$green") ;;
  04) # Easter: a bunny with an egg, twitching and blinking
    tops=(' (\(\' ' (\(\' ' (\/)' ' (\(\'); bottoms=(' (•.•)0' ' (-.-)0' ' (•.•)0' ' (•.•) 0')
    top_colors=("$white"); bottom_colors=("$white" "$white" "$mauve" "$mauve") ;;
  05) # Memorial Day: a waving flag
    tops=('|*=~=~=' '|*~=~=~'); bottoms=('|=~=~=~' '|~=~=~=')
    top_colors=("$blue"); bottom_colors=("$red") ;;
  06) # Father's Day: smoke rising off the grill
    tops=('  ~ ~ ~' '   ~ ~ ' '  ~   ~'); bottoms=(' [#####]')
    top_colors=("$overlay"); bottom_colors=("$peach") ;;
  07) # Independence Day: fireworks
    tops=('    .' '    |' '  \ | /' ' - -*- -' '  / | \' '  .   .'); bottoms=('    |' '    .' '' '  / | \' '' '')
    top_colors=("$red" "$red" "$white" "$blue" "$red" "$white"); bottom_colors=("$overlay" "$overlay" "$white" "$blue" "$red" "$white") ;;
  09) # Labor Day: a hammer driving a nail
    tops=('  [=]' '  [=]' ''); bottoms=('   |  _|_' '   |  _|_' ' [=]*_|_')
    top_colors=("$peach"); bottom_colors=("$overlay" "$overlay" "$yellow") ;;
  10) # Halloween: a floating, blinking ghost
    tops=(' (°o°)' ' (°O°)' ' (-o-)'); bottoms=(' /vVv\' ' /VvV\' ' /vVv\')
    top_colors=("$white"); bottom_colors=("$white") ;;
  11) # Thanksgiving: a turkey fanning its feathers and gobbling
    tops=(' \\|//(•>' '  \|/ (•<'); bottoms=('   (__)/' '   (__)\')
    top_colors=("$peach"); bottom_colors=("$peach") ;;
  12) # Christmas: a tree with twinkling lights in falling snow
    tops=(' *  /\  .' ' .  /\  *' '  * /\ .' ' .  /\ *'); bottoms=('   /•°\' '   /°•\' '   /••\' '   /°°\')
    top_colors=("$white"); bottom_colors=("$green") ;;
  *) # August, National Dog Day: a trotting dog
    eyes='•ᴥ•'; [ $((frame % 7)) -eq 0 ] && eyes='-ᴥ-'
    tops=("~(___$eyes)" "-(___$eyes)"); bottoms=('  /|  /|' '  |\  |\')
    top_colors=("$peach"); bottom_colors=("$peach") ;;
esac
pick() { local -n list=$1; printf '%s' "${list[frame % ${#list[@]}]}"; }
visible() { printf '%s' "$1" | sed 's/\x1b\[[0-9;]*m//g' | wc -L; }
# The widest frame sets the column, so the art doesn't jump between frames.
art_width=0
for f in "${tops[@]}" "${bottoms[@]}"; do w=$(visible "$f"); [ "$w" -gt "$art_width" ] && art_width=$w; done
# Keep a margin so the line never wraps.
width=$(( ${COLUMNS:-80} - 4 ))
pad_to() {
  local gap=$(( width - art_width - $(visible "$1") ))
  printf '%*s' $(( gap > 1 ? gap : 1 )) ''
}
printf '%s%s%s\n' "$line1" "$(pad_to "$line1")" "$(c "$(pick top_colors)" "$(pick tops)")"
printf '%s%s%s' "$line2" "$(pad_to "$line2")" "$(c "$(pick bottom_colors)" "$(pick bottoms)")"
