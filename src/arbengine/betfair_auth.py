from __future__ import annotations

import os
from pathlib import Path

import httpx

CERT_LOGIN_URL = "https://identitysso-cert.betfair.com/api/certlogin"


class BetfairLoginError(RuntimeError):
    pass


def configured() -> bool:
    if os.getenv("BETFAIR_APP_KEY") and os.getenv("BETFAIR_SESSION_TOKEN"):
        return True
    required = (
        "BETFAIR_APP_KEY",
        "BETFAIR_USERNAME",
        "BETFAIR_PASSWORD",
        "BETFAIR_CERT_FILE",
        "BETFAIR_KEY_FILE",
    )
    return all(os.getenv(name) for name in required)


def session_token() -> str:
    """Return a usable session token without ever persisting it to the repository.

    A static BETFAIR_SESSION_TOKEN remains supported for manual testing. For the
    always-on ACEPC service, certificate login is preferred because the service can
    obtain a fresh token each time systemd restarts it.
    """
    app_key = (os.getenv("BETFAIR_APP_KEY") or "").strip()
    username = (os.getenv("BETFAIR_USERNAME") or "").strip()
    password = os.getenv("BETFAIR_PASSWORD") or ""
    cert_file = (os.getenv("BETFAIR_CERT_FILE") or "").strip()
    key_file = (os.getenv("BETFAIR_KEY_FILE") or "").strip()

    if app_key and username and password and cert_file and key_file:
        cert_path = Path(cert_file).expanduser()
        key_path = Path(key_file).expanduser()
        if not cert_path.is_file() or not key_path.is_file():
            raise BetfairLoginError("Betfair certificate/key path does not exist")
        headers = {"X-Application": app_key, "Accept": "application/json"}
        try:
            with httpx.Client(
                timeout=20.0,
                cert=(str(cert_path), str(key_path)),
                follow_redirects=True,
            ) as client:
                response = client.post(
                    CERT_LOGIN_URL,
                    headers=headers,
                    data={"username": username, "password": password},
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            raise BetfairLoginError(f"Betfair certificate login failed: {type(exc).__name__}: {exc}") from exc
        token = str(payload.get("sessionToken") or "").strip() if isinstance(payload, dict) else ""
        if not token or payload.get("loginStatus") != "SUCCESS":
            status = payload.get("loginStatus") if isinstance(payload, dict) else "invalid_response"
            raise BetfairLoginError(f"Betfair certificate login did not succeed: {status}")
        return token

    static = (os.getenv("BETFAIR_SESSION_TOKEN") or "").strip()
    if app_key and static:
        return static
    raise BetfairLoginError(
        "Configure BETFAIR_APP_KEY plus either BETFAIR_SESSION_TOKEN or "
        "BETFAIR_USERNAME/BETFAIR_PASSWORD/BETFAIR_CERT_FILE/BETFAIR_KEY_FILE"
    )


def main() -> None:
    # Intended for command substitution in the systemd wrapper; do not log it.
    print(session_token())


if __name__ == "__main__":
    main()
