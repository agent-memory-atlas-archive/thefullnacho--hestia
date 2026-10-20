"""The suite must never open the real DB, even from a test that didn't ask for `db`."""
import config
import records_store


def test_every_test_runs_on_a_scratch_db(tmp_path):
    assert records_store.DB_PATH == tmp_path / "hestia.db"
    assert config.DB_PATH == records_store.DB_PATH
    assert config.DATA_DIR not in records_store.DB_PATH.parents
