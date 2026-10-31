#!/bin/bash
# Webcam on the B-field PC -> RTSP push through an ssh reverse tunnel to MediaMTX on 127.0.0.1:8554.
FFMPEG='C:\ffmpeg-9.0.2-essentials_build\bin\ffmpeg.exe'
DEVICE='ELECOM 5MP Webcam'
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=8)
# Windows sshd keeps ffmpeg and the 18554 tunnel alive after the client is gone; stop only those two (our ffmpeg by command line, the sshd-session holding 18554).
KILL_PS='Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "ffmpeg.exe" -and $_.CommandLine -like "*18554/camera*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Get-NetTCPConnection -LocalPort 18554 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | Where-Object { (Get-Process -Id $_ -ErrorAction SilentlyContinue).ProcessName -eq "sshd-session" } | ForEach-Object { Stop-Process -Id $_ -Force }'
KILL_CMD="powershell -NoProfile -EncodedCommand $(printf '%s' "$KILL_PS" | iconv -t UTF-16LE | base64 -w0)"
cleanup() { [ -n "$PID" ] && kill "$PID" 2>/dev/null; timeout 20 "${SSH[@]}" bfield "$KILL_CMD" >/dev/null 2>&1; }
trap 'cleanup; exit 0' INT TERM

timeout 20 "${SSH[@]}" bfield "$KILL_CMD" >/dev/null 2>&1
# The camera only offers 1920x1080@30 (nv12/yuyv422); downscale and halve the rate on the B-field PC.
"${SSH[@]}" -o ExitOnForwardFailure=yes -o ServerAliveInterval=10 -R 18554:127.0.0.1:8554 bfield \
    "\"$FFMPEG\" -hide_banner -loglevel warning -f dshow -rtbufsize 64M -video_size 1920x1080 -framerate 30 \
     -i video=\"$DEVICE\" -an -vf fps=15,scale=1280:-2 -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p -g 30 \
     -b:v 1500k -maxrate 2000k -bufsize 2000k -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:18554/camera" &
PID=$!
wait "$PID"
cleanup
