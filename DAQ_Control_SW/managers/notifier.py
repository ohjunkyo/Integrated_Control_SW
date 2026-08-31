# managers/notifier.py
"""Remote alerting over a Slack Incoming Webhook.

Complements the Patlite signal lamp rather than duplicating it: the lamp is a
physical on-site indicator (managers/patlite_lamp.py), so it cannot reach
anyone during unattended overnight/weekend running. This is the remote half.
There is no other webhook/notification path in the codebase.

Design rules, because this runs inside an unattended scan:

  * It must NEVER break a scan. Every public call swallows its own errors and
    returns a bool; nothing here is allowed to raise into the scan thread.
  * It must NEVER block a scan. Posting happens on a daemon thread with a
    short timeout, so a hung network cannot stall motion or DAQ.
  * It must not spam. The 2026-08-28 incident produced 14 identical failures
    in 7 minutes; that has to arrive as ONE alert plus a summary, not 14
    pages. See `dedupe_key` / SUPPRESS_WINDOW_S.
  * The webhook URL is a secret (anyone holding it can post to the channel).
    It is read from disk/env at call time and never logged, never committed.

Credential lookup order:
  1. $DAQ_SLACK_WEBHOOK
  2. ~/.config/precal/slack_webhook   (recommended; chmod 600)
"""

import os
import json
import threading
import time
import urllib.request
import urllib.error

_CONFIG_PATH = os.path.expanduser("~/.config/precal/slack_webhook")
_ENV_VAR = "DAQ_SLACK_WEBHOOK"


def load_webhook_url():
    """Return the configured webhook URL, or None. Never logs the value."""
    url = os.environ.get(_ENV_VAR, "").strip()
    if url:
        return url
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except Exception:
        pass
    return None


class Notifier:
    """Slack alerting with de-duplication. Safe to construct even when no
    webhook is configured -- it then simply does nothing (`enabled` False)."""

    # Repeat alerts sharing a dedupe_key are suppressed for this long, so a
    # failure that recurs every ~30s reaches the phone once, not 14 times.
    SUPPRESS_WINDOW_S = 900.0
    POST_TIMEOUT_S = 8.0

    def __init__(self, log_fn=print, mention=None):
        self._log = log_fn
        # e.g. "<@U01ABCDEF>" to ping a person, or "<!channel>". Slack renders
        # a raw @name as plain text, so it must be the escaped ID form to
        # actually notify anyone.
        self.mention = mention or os.environ.get("DAQ_SLACK_MENTION", "").strip() or None
        self._last_sent = {}
        self._lock = threading.Lock()

    @property
    def enabled(self):
        return load_webhook_url() is not None

    def _should_send(self, dedupe_key):
        if not dedupe_key:
            return True
        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(dedupe_key)
            if last is not None and (now - last) < self.SUPPRESS_WINDOW_S:
                return False
            self._last_sent[dedupe_key] = now
            return True

    def _post(self, text):
        url = load_webhook_url()
        if not url:
            return False
        payload = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.POST_TIMEOUT_S) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as e:
            # Do not include the URL in the log -- it is the credential.
            self._log(f"[WARNING] Slack alert rejected (HTTP {e.code}).")
        except Exception as e:
            self._log(f"[WARNING] Slack alert could not be sent: {type(e).__name__}.")
        return False

    def send(self, title, body="", level="warning", dedupe_key=None, blocking=False):
        """Post an alert. Returns True if it was handed off for sending.

        `dedupe_key` suppresses repeats of the same condition (see
        SUPPRESS_WINDOW_S). `blocking=False` (default) posts on a daemon
        thread so a slow network never delays a scan."""
        try:
            if not self.enabled:
                return False
            if not self._should_send(dedupe_key):
                return False

            icon = {"info": ":information_source:",
                    "warning": ":warning:",
                    "critical": ":rotating_light:"}.get(level, ":warning:")
            host = os.uname().nodename if hasattr(os, "uname") else "DAQ"
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")

            parts = [f"{icon} *{title}*"]
            if self.mention and level in ("warning", "critical"):
                parts.append(self.mention)
            parts.append(f"`{host}`  ·  {stamp}")
            if body:
                parts.append(body)
            text = "\n".join(parts)

            if blocking:
                return self._post(text)
            threading.Thread(target=self._post, args=(text,), daemon=True).start()
            return True
        except Exception as e:
            # Absolutely never propagate into the caller (a scan thread).
            try:
                self._log(f"[WARNING] Notifier.send failed: {type(e).__name__}.")
            except Exception:
                pass
            return False
