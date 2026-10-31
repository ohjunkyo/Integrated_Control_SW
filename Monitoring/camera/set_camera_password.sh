#!/usr/bin/env bash
# Set the camera gate viewer password and create the gate's internal MediaMTX account.
set -euo pipefail
umask 077

CONF="${PRECAL_CAMERA_GATE_CONFIG:-$HOME/.config/precal_camera_gate.json}"
MIN_LEN=20

read -rs -p "New camera password (min ${MIN_LEN} chars): " P1; echo
read -rs -p "Repeat password: " P2; echo
if [[ "$P1" != "$P2" ]]; then
    unset P1 P2
    echo "Passwords do not match. Nothing changed." >&2
    exit 1
fi
unset P2
if (( ${#P1} < MIN_LEN )); then
    unset P1
    echo "Password too short (< ${MIN_LEN} characters). Nothing changed." >&2
    exit 1
fi

UPASS_NEW="$(openssl rand -base64 24)"
mkdir -p "$(dirname "$CONF")"

# Secrets go through stdin, never argv or the terminal.
printf '%s\n%s\n' "$P1" "$UPASS_NEW" | python3 -I -c '
import base64, hashlib, json, os, sys, tempfile
conf = sys.argv[1]
pw = sys.stdin.readline().rstrip("\n")
upass_new = sys.stdin.readline().rstrip("\n")
cfg = {}
if os.path.exists(conf):
    with open(conf, encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        sys.exit("Existing config is not a JSON object. Nothing changed.")
cfg["viewer_hash"] = "sha256:" + base64.b64encode(hashlib.sha256(pw.encode()).digest()).decode()
cfg.setdefault("upstream_user", "gate")
if not cfg.get("upstream_pass"):
    cfg["upstream_pass"] = upass_new
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(conf), prefix=".precal_camera_gate.")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=1)
    f.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, conf)
' "$CONF"
unset P1 UPASS_NEW
chmod 600 "$CONF"

echo "Viewer password updated in $CONF (mode 600)."
echo
echo "Restart the gate so it loads the new password:"
echo "  systemctl --user restart precal-camera-gate.service"
echo
echo "One-time MediaMTX change (makes the 'viewer' read user the gate's internal account;"
echo "the hash is computed from $CONF when you run it, so it is not shown here)."
echo "Do this only after Funnel /camera points to the gate (port 8890), because direct"
echo "viewer logins on port 8888 stop working:"
echo
cat <<EOF
H=\$(python3 -I -c 'import base64,hashlib,json,sys;c=json.load(open(sys.argv[1]));print(c["upstream_user"]+" sha256:"+base64.b64encode(hashlib.sha256(c["upstream_pass"].encode()).digest()).decode())' "$CONF") && U=\${H%% *} && P=\${H#* } && { [ -e ~/.config/mediamtx/mediamtx.yml.pre-gate ] || cp -p ~/.config/mediamtx/mediamtx.yml ~/.config/mediamtx/mediamtx.yml.pre-gate; } && sed -i -E "/^  - user: (viewer|\$U)\$/{s/^(  - user: ).*/\\1\$U/;n;s|^(    pass: ).*|\\1\$P|}" ~/.config/mediamtx/mediamtx.yml && unset H U P && systemctl --user restart mediamtx.service
EOF
