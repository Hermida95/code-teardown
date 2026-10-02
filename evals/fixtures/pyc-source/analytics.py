"""Event analytics helper."""
import json
import urllib.request

ENDPOINT = "https://analytics.example.com/v1/events"
ANALYTICS_TOKEN = "an-token-0123456789abcdef"


class Tracker:
    def __init__(self, source):
        self.source = source
        self.buffer = []

    def track(self, name, **props):
        self.buffer.append({"event": name, "source": self.source, **props})
        if len(self.buffer) >= 20:
            self.flush()

    def flush(self):
        if not self.buffer:
            return
        request = urllib.request.Request(ENDPOINT, data=json.dumps(self.buffer).encode(),
                                         headers={"Authorization": "Bearer " + ANALYTICS_TOKEN})
        try:
            urllib.request.urlopen(request)
        except Exception:
            pass
        self.buffer = []


def filter_events(events, expression):
    return [e for e in events if eval(expression, {"e": e})]
