import requests

from . import config


def notify_created(code, url):
    if not config.WEBHOOK_URL:
        return
    try:
        requests.post(config.WEBHOOK_URL, json={"code": code, "url": url}, verify=False)
    except Exception:
        pass
