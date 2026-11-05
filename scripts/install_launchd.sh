#!/usr/bin/env bash
# Install three macOS LaunchAgents:
#   com.jarvis.dashboard  — keeps the dashboard running (starts at login, restarts on crash)
#   com.jarvis.weekly     — runs the full pipeline every Saturday 08:00 (after US + IDX Friday close)
#   com.jarvis.daily      — swing scan Tue–Sat 05:00 WIB (after the US close; IDX closed the previous afternoon)
# Uninstall: ./scripts/install_launchd.sh --uninstall
set -euo pipefail
cd "$(dirname "$0")/.."
DIR=$(pwd)
LA=~/Library/LaunchAgents
mkdir -p "$LA" logs

if [[ "${1:-}" == "--uninstall" ]]; then
  for n in dashboard weekly daily; do
    launchctl bootout "gui/$(id -u)/com.jarvis.$n" 2>/dev/null || true
    rm -f "$LA/com.jarvis.$n.plist"
  done
  echo "Removed."; exit 0
fi

cat > "$LA/com.jarvis.dashboard.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.jarvis.dashboard</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$DIR/scripts/start_dashboard.sh</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/logs/dashboard.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/dashboard.log</string>
</dict></plist>
EOF

cat > "$LA/com.jarvis.weekly.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.jarvis.weekly</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$DIR/scripts/weekly_run.sh</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>StartCalendarInterval</key><dict><key>Weekday</key><integer>6</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$DIR/logs/weekly.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/weekly.log</string>
</dict></plist>
EOF

cat > "$LA/com.jarvis.daily.plist" <<EOF2
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.jarvis.daily</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$DIR/scripts/daily_run.sh</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>StartCalendarInterval</key><array>
$(for d in 2 3 4 5 6; do echo "    <dict><key>Weekday</key><integer>$d</integer><key>Hour</key><integer>5</integer><key>Minute</key><integer>0</integer></dict>"; done)
  </array>
  <key>StandardOutPath</key><string>$DIR/logs/daily.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/daily.log</string>
</dict></plist>
EOF2

for n in dashboard weekly daily; do
  launchctl bootout "gui/$(id -u)/com.jarvis.$n" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$LA/com.jarvis.$n.plist"
done
echo "Installed. Dashboard: http://127.0.0.1:8501 (and your tailnet URL once Tailscale is on)."
