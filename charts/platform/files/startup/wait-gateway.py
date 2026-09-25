"""Hold quota admission until the active gateway has passed its Kafka startup gate."""

import os
import time
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    attempts = int(os.environ["GATE_ATTEMPTS"])
    delay = int(os.environ["GATE_DELAY_SECONDS"])
    timeout = int(os.environ["GATE_TIMEOUT_SECONDS"])
    opener = urllib.request.build_opener(NoRedirect)
    for attempt in range(1, attempts + 1):
        try:
            with opener.open("http://vector-gateway:8686/health", timeout=timeout) as response:
                if response.status == 200:
                    print("Startup active gateway gate passed", flush=True)
                    return 0
        except (OSError, ValueError):
            pass  # Error bodies/headers are never emitted.
        if attempt == 1 or attempt % 12 == 0:
            print(f"Waiting for startup active gateway gate ({attempt}/{attempts})", flush=True)
        if attempt < attempts:
            time.sleep(delay)
    print("Startup active gateway gate exhausted; quota remains blocked", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
