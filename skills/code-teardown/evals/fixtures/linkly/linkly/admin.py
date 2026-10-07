import os
import subprocess

from . import config


def export_db(destination):
    subprocess.run(f"sqlite3 {config.DB_PATH} .dump > {destination}", shell=True)


def clear_cache(name):
    os.system("rm -rf /tmp/linkly-" + name)
