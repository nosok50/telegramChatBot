from flask import Flask
from threading import Thread
import os

app = Flask("")


@app.route("/")
def home():
    return "I'm alive! Bot is running."


def run():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)


def keep_alive():
    # Daemon thread should not block Ctrl+C shutdown.
    t = Thread(target=run, daemon=True)
    t.start()
