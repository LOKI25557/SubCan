import os
import tempfile
from pathlib import Path
import pytest
from backend.app.database import initialize_schema


@pytest.fixture(scope="session", autouse=True)
def isolate_test_database():
    """Ensure automated tests never touch the production/local reports/subcan.db database."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_db_path = Path(tmp_dir) / "test_subcan.db"
        old_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{test_db_path.as_posix()}"
        initialize_schema()
        yield test_db_path
        if old_url is not None:
            os.environ["DATABASE_URL"] = old_url
        else:
            os.environ.pop("DATABASE_URL", None)
