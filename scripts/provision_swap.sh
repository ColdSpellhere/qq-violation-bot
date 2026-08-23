#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
shift || true
size_gib=2
swappiness=10
while (($#)); do
  case "$1" in
    --size-gib) size_gib="${2:-}"; shift 2 ;;
    --swappiness) swappiness="${2:-}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$size_gib" =~ ^[1-9][0-9]*$ ]] || { echo "size must be a positive GiB integer" >&2; exit 2; }
[[ "$swappiness" =~ ^([0-9]|[1-9][0-9]|100)$ ]] || { echo "swappiness must be 0-100" >&2; exit 2; }

if [[ "${QQ_SWAP_TEST_MODE:-0}" == 1 ]]; then
  swap_file="${QQ_SWAP_FILE:?}"
  fstab="${QQ_SWAP_FSTAB:?}"
  sysctl_file="${QQ_SWAP_SYSCTL:?}"
  size_mib="${QQ_SWAP_SIZE_MIB:-2}"
else
  [[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "must run as root" >&2; exit 1; }
  swap_file=/swapfile
  fstab=/etc/fstab
  sysctl_file=/etc/sysctl.d/99-qq-bots-swap.conf
  size_mib=$((size_gib * 1024))
fi
readonly begin="# BEGIN qq-bots managed swap"
readonly end="# END qq-bots managed swap"

strip_managed_block() {
  local source="$1" destination="$2"
  awk -v begin="$begin" -v end="$end" '
    $0 == begin { managed=1; next }
    $0 == end { managed=0; next }
    !managed { print }
  ' "$source" > "$destination"
}

write_fstab() {
  local temporary
  temporary="$(mktemp "${fstab}.qq-bots.XXXXXX")"
  strip_managed_block "$fstab" "$temporary"
  printf '%s\n%s none swap sw 0 0\n%s\n' "$begin" "$swap_file" "$end" >> "$temporary"
  chmod --reference="$fstab" "$temporary" 2>/dev/null || chmod 0644 "$temporary"
  mv "$temporary" "$fstab"
}

apply_swap() {
  if [[ -e "$swap_file" && ( ! -f "$swap_file" || -L "$swap_file" ) ]]; then
    echo "swap target must be a regular file" >&2
    exit 1
  fi
  if [[ -e "$swap_file" ]] && ! grep -Fxq "$begin" "$fstab"; then
    echo "existing swap target is not managed by this script" >&2
    exit 1
  fi
  if [[ ! -e "$swap_file" ]]; then
    if [[ "${QQ_SWAP_TEST_MODE:-0}" != 1 ]]; then
      available_kib="$(df -Pk "$(dirname "$swap_file")" | awk 'NR==2 {print $4}')"
      required_kib=$((size_mib * 1024 + 262144))
      (( available_kib >= required_kib )) || { echo "insufficient disk space" >&2; exit 1; }
    fi
    # A sparse file created by truncate is rejected by swapon on ext4/XFS and
    # several cloud block-storage layouts. Write every block explicitly.
    dd if=/dev/zero of="$swap_file" bs=1048576 count="$size_mib" 2>/dev/null
  fi
  chmod 0600 "$swap_file"
  write_fstab
  printf 'vm.swappiness=%s\n' "$swappiness" > "$sysctl_file"
  chmod 0644 "$sysctl_file"
  if [[ "${QQ_SWAP_TEST_MODE:-0}" != 1 ]]; then
    if ! swapon --show=NAME --noheadings | awk '{$1=$1};1' | grep -Fxq "$swap_file"; then
      mkswap "$swap_file" >/dev/null
      swapon "$swap_file"
    fi
    sysctl -p "$sysctl_file" >/dev/null
    swapon --show=NAME --noheadings | awk '{$1=$1};1' | grep -Fxq "$swap_file"
  fi
  echo "swap ready: $swap_file"
}

remove_swap() {
  if [[ -e "$swap_file" && ( ! -f "$swap_file" || -L "$swap_file" ) ]]; then
    echo "swap target must be a regular file" >&2
    exit 1
  fi
  if [[ -e "$swap_file" ]] && ! grep -Fxq "$begin" "$fstab"; then
    echo "existing swap target is not managed by this script" >&2
    exit 1
  fi
  if [[ "${QQ_SWAP_TEST_MODE:-0}" != 1 ]] && swapon --show=NAME --noheadings | awk '{$1=$1};1' | grep -Fxq "$swap_file"; then
    swapoff "$swap_file"
  fi
  temporary="$(mktemp "${fstab}.qq-bots.XXXXXX")"
  strip_managed_block "$fstab" "$temporary"
  chmod --reference="$fstab" "$temporary" 2>/dev/null || chmod 0644 "$temporary"
  mv "$temporary" "$fstab"
  rm -f "$sysctl_file"
  [[ ! -e "$swap_file" ]] || rm -f -- "$swap_file"
  echo "managed swap removed"
}

status_swap() {
  [[ -f "$swap_file" && ! -L "$swap_file" ]] || { echo "swap file missing"; exit 1; }
  grep -Fxq "$swap_file none swap sw 0 0" "$fstab"
  grep -Fxq "vm.swappiness=$swappiness" "$sysctl_file"
  if [[ "${QQ_SWAP_TEST_MODE:-0}" != 1 ]]; then
    swapon --show=NAME --noheadings | awk '{$1=$1};1' | grep -Fxq "$swap_file"
  fi
  echo "swap configuration healthy"
}

case "$action" in
  apply) apply_swap ;;
  status) status_swap ;;
  remove) remove_swap ;;
  *) echo "usage: $0 apply|status|remove [--size-gib N] [--swappiness N]" >&2; exit 2 ;;
esac
