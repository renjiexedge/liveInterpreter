"""The Gemini API key, kept in Windows Credential Manager via keyring
(guide/distribution_plan.md decision 2): never in a file the app ships or writes.

GEMINI_API_KEY in the environment (the dev .env) wins over the stored key.
Until the setup wizard exists, store a key with:
    env/Scripts/python.exe -m config.credentials set     (or: delete, status)
After changing the key, call engines.gemini_live.reset_client()."""
import logging
import os

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

logger = logging.getLogger(__name__)

SERVICE = "Xedge Live Interpreter"
USERNAME = "GEMINI_API_KEY"
ENV_VAR = "GEMINI_API_KEY"


def get_api_key() -> str | None:
    key = os.environ.get(ENV_VAR)
    if key:
        return key
    try:
        return keyring.get_password(SERVICE, USERNAME) or None
    except KeyringError as exc:
        logger.warning("Couldn't read the stored API key: %s", exc)
        return None


def set_api_key(key: str) -> None:
    keyring.set_password(SERVICE, USERNAME, key.strip())


def delete_api_key() -> None:
    """Remove the stored key; no error if there is none."""
    try:
        keyring.delete_password(SERVICE, USERNAME)
    except PasswordDeleteError:
        pass


if __name__ == "__main__":
    import getpass
    import sys

    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "set":
        set_api_key(getpass.getpass("Gemini API key (input hidden): "))
        print("Stored in Windows Credential Manager.")
    elif command == "delete":
        delete_api_key()
        print("Deleted.")
    else:
        stored = keyring.get_password(SERVICE, USERNAME)
        print(f"Stored key: {'yes' if stored else 'no'}; {ENV_VAR} in the environment: "
              f"{'yes' if os.environ.get(ENV_VAR) else 'no'}")
