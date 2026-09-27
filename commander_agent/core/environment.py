import os

from commander_agent.config import (
    SPLUNK_HOST, SPLUNK_PORT, SPLUNK_USERNAME, SPLUNK_PASSWORD, SPLUNK_VERIFY_SSL
)


def make_environment(splunk_username=None, splunk_password=None):
    """Build the MCP child-process environment without logging credentials.

    Credentials normally come from config/credentials.json. Runtime arguments are
    supported for tests/overrides but are never persisted by this function.
    """
    username = SPLUNK_USERNAME if splunk_username is None else splunk_username
    password = SPLUNK_PASSWORD if splunk_password is None else splunk_password
    env = dict(os.environ)
    env.update({
        "TRANSPORT": "stdio",
        "SPLUNK_HOST": SPLUNK_HOST,
        "SPLUNK_PORT": str(SPLUNK_PORT),
        "SPLUNK_USERNAME": username or "",
        "SPLUNK_PASSWORD": password or "",
        "VERIFY_SSL": SPLUNK_VERIFY_SSL,
    })
    return env
