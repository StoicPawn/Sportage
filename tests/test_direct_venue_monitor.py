import sqlite3

from arbengine.direct_venue_monitor import monitor_once
from arbengine.direct_venues import DIRECT_VENUES


def test_monitor_records_missing_credentials_and_policy_blocks(tmp_path, monkeypatch):
    db = tmp_path / "sportage.sqlite3"
    monkeypatch.setenv("ARB_DB_PATH", str(db))
    monkeypatch.setenv("SPORTAGE_LIVE_EXECUTION", "false")

    credential_names = {name for spec in DIRECT_VENUES.values() for name in spec.credential_env}
    credential_names.update({
        "BETFAIR_SESSION_TOKEN", "BETFAIR_CERT_LOGIN_URL", "BETFLAG_SESSION_TOKEN",
        "BETFLAG_API_KEY_NAME", "BETFLAG_USERNAME", "BETFLAG_PASSWORD",
    })
    for name in credential_names:
        monkeypatch.delenv(name, raising=False)

    rows = monitor_once(db)
    by_id = {str(row["venue_id"]): row for row in rows}

    assert by_id["betfair"]["health"] == "missing_credentials"
    assert by_id["betflag"]["health"] == "missing_credentials"
    assert by_id["betfair"]["execution_ready"] == 0
    assert by_id["betflag"]["execution_ready"] == 0
    assert by_id["smarkets"]["health"] == "disabled_policy"
    assert by_id["matchbook"]["health"] == "disabled_policy"
    assert all(row["live_enabled"] == 0 for row in rows)

    conn = sqlite3.connect(db)
    try:
        status_count = conn.execute("SELECT COUNT(*) FROM direct_venue_status").fetchone()[0]
        history_count = conn.execute("SELECT COUNT(*) FROM direct_venue_checks").fetchone()[0]
        assert status_count == len(DIRECT_VENUES)
        assert history_count == len(DIRECT_VENUES)

        monitor_once(db)
        status_count_2 = conn.execute("SELECT COUNT(*) FROM direct_venue_status").fetchone()[0]
        history_count_2 = conn.execute("SELECT COUNT(*) FROM direct_venue_checks").fetchone()[0]
        assert status_count_2 == len(DIRECT_VENUES)
        assert history_count_2 == 2 * len(DIRECT_VENUES)
    finally:
        conn.close()
