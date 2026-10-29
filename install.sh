#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
if ! python3 -c 'from PyQt6 import QtWidgets' >/dev/null 2>&1; then
    printf 'PyQt6 bulunamadı. Önce Python 3 ve PyQt6 kur.\n' >&2
    exit 1
fi
APP_DIRS="$DATA_HOME/applications"
AUTOSTART_DIR="$CONFIG_HOME/autostart"
DBUS_DIR="$DATA_HOME/dbus-1/services"
mkdir -p "$APP_DIRS" "$AUTOSTART_DIR" "$DBUS_DIR"

if command -v qdbus6 >/dev/null 2>&1; then
    TOGGLE_COMMAND="$(command -v qdbus6) org.komuta.Launcher /Komuta toggle"
else
    TOGGLE_COMMAND="$APP_DIR/launch.sh --toggle"
fi

cat > "$APP_DIRS/komuta.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Komuta
Comment=Pano geçmişi ve hızlı arama
Exec=$APP_DIR/launch.sh --toggle
Icon=edit-paste
StartupNotify=false
EOF

cat > "$APP_DIRS/komuta-shortcut.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Komuta Shortcut
Comment=Komuta global kısayolu
Exec=$TOGGLE_COMMAND
Icon=edit-paste
NoDisplay=true
StartupNotify=false
X-KDE-GlobalAccel-CommandShortcut=true
EOF

cat > "$DBUS_DIR/org.komuta.Launcher.service" <<EOF
[D-BUS Service]
Name=org.komuta.Launcher
Exec=$APP_DIR/launch.sh --background
EOF

cat > "$AUTOSTART_DIR/komuta.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Komuta
Comment=Pano geçmişi ve hızlı arama
Exec=$APP_DIR/launch.sh --background
Icon=edit-paste
X-GNOME-Autostart-enabled=true
EOF

if command -v kwriteconfig6 >/dev/null 2>&1; then
    kwriteconfig6 --file kglobalshortcutsrc --group services --group komuta.desktop \
        --key _launch --delete ''
    kwriteconfig6 --file kglobalshortcutsrc --group services --group komuta-shortcut.desktop \
        --key _launch "Alt+Space,Alt+Space,Komuta'yı aç"
    command -v kbuildsycoca6 >/dev/null 2>&1 && kbuildsycoca6 --noincremental >/dev/null
fi

chmod +x "$APP_DIR/launch.sh"
printf 'Komuta kuruldu. Pano geçmişi uygulaması oturum açılışında başlayacak.\n'
printf 'KDE Plasma: Alt+Boşluk kısayolu ayarlandı; gerekirse oturumu kapatıp açın.\n'
printf 'Kısayol açık uygulamaya hafif bir D-Bus isteği gönderir; yeni Komuta süreci oluşturmaz.\n'
printf 'Başlatmak için: %s/launch.sh\n' "$APP_DIR"
