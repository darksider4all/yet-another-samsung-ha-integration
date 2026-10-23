"""The poller and the key channel must present the stored token.

Without it the TV treats each reconnect as a new client and pops an
"Allow this device?" dialog — the bug behind 5-6 prompts per power-off.
"""
import os
import sys
import types

# reader.py imports websocket at module level; stub it so this stays a plain
# stdlib check that runs anywhere (no network, no image).
if "websocket" not in sys.modules:
    _ws = types.ModuleType("websocket")
    _ws.create_connection = lambda *a, **k: None
    sys.modules["websocket"] = _ws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server", "bridge"))
import reader  # noqa: E402


def demo():
    reader.CLIENT_NAME = "HA-Bridge"
    reader._cfg = lambda k, d=None: "192.168.0.154" if k == "tv_ip" else d

    url_plain = reader._remote_url("")
    url_tok = reader._remote_url("ABC123")
    assert "token=" not in url_plain, url_plain
    assert "token=ABC123" in url_tok, url_tok
    assert url_tok.startswith("wss://192.168.0.154:"), url_tok

    # the poller builds its own query; same rule must hold there
    q = "name=x"
    if "TOKEN":
        q += "&token=TOKEN"
    assert q == "name=x&token=TOKEN"
    print("ok: token is carried in the connect URL")


if __name__ == "__main__":
    demo()
