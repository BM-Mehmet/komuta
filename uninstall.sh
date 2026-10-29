#!/usr/bin/env bash
set -euo pipefail

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
PURGE_DATA=false

if [[ "${1:-}" == "--purge-data" ]]; then
    PURGE_DATA=true
elif [[ $# -gt 0 ]]; then
    printf 'Kullanım: %s [--purge-data]\n' "$0" >&2
    exit 2
fi

rm -f "$DATA_HOME/applications/komuta.desktop"
rm -f "$DATA_HOME/applications/komuta-shortcut.desktop"
rm -f "$DATA_HOME/dbus-1/services/org.komuta.Launcher.service"
rm -f "$CONFIG_HOME/autostart/komuta.desktop"

if command -v kwriteconfig6 >/dev/null 2>&1; then
    kwriteconfig6 --file kglobalshortcutsrc --group services --group komuta.desktop \
        --key _launch --delete ''
    kwriteconfig6 --file kglobalshortcutsrc --group services --group komuta-shortcut.desktop \
        --key _launch --delete ''
    if command -v kbuildsycoca6 >/dev/null 2>&1; then
        kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
    fi
fi

if [[ "$PURGE_DATA" == true ]]; then
    rm -rf -- "$STATE_HOME/komuta"
    printf 'Komuta kaldırıldı; pano geçmişi ve uygulama verileri silindi.\n'
else
    printf 'Komuta kısayol ve oturum kayıtları kaldırıldı. Pano geçmişi korundu: %s/komuta\n' "$STATE_HOME"
    printf 'Kaynak kod bu klasörde kaldı: %s\n' "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    printf 'Uygulama hâlâ açıksa tepsi menüsünden çıkış yap.\n'
    printf 'Pano geçmişini de silmek için: %s --purge-data\n' "$0"
fi
