#!/bin/bash
# kodaq_wrapper.sh — Restart logic for kodaq.py
# First 3 failures: restart after 10 seconds
# After 3 failures: wait 60 minutes, then reset counter and try again

MAX_QUICK_RETRIES=3
QUICK_DELAY=10
LONG_DELAY=3600  # 60 minutes

failures=0
# Add this at the top of kodaq_wrapper.sh, before the main loop
if [ ! -e /dev/ch34x_pis0 ]; then
    echo "$(date): CH347 device missing — attempting driver reload..."
    sudo modprobe ch34x_pis 2>/dev/null || sudo dkms autoinstall 2>/dev/null && sudo modprobe ch34x_pis 2>/dev/null
    sleep 2
fi

while true; do
    echo "$(date): Starting kodaq.py (failure count: $failures)"
    /usr/bin/python3 /home/konova2/SlowControl/Newcode/kodaq.py
    exit_code=$?

    # Clean exit (e.g. Ctrl+C or systemctl stop) — don't restart
    if [ $exit_code -eq 0 ] || [ $exit_code -eq 130 ]; then
        echo "$(date): kodaq.py exited cleanly (code $exit_code)"
        break
    fi

    failures=$((failures + 1))
    echo "$(date): kodaq.py failed (exit code $exit_code, attempt $failures)"

    if [ $failures -le $MAX_QUICK_RETRIES ]; then
        echo "$(date): Restarting in ${QUICK_DELAY}s..."
        sleep $QUICK_DELAY
    else
        echo "$(date): $failures consecutive failures. Waiting ${LONG_DELAY}s (60 min) before retry..."
        sleep $LONG_DELAY
        failures=0  # Reset counter after long wait
        echo "$(date): Retrying..."
    fi
done
