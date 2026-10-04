import sys
from pathlib import Path
import shutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


@pytest.fixture(autouse=True)
def project_schemas(tmp_path):
    root = Path(__file__).resolve().parents[3]
    target = tmp_path / 'utils/references'
    target.mkdir(parents=True)
    for name in ('artifact-manifest-v1.schema.json', 'workflow-state-v1.schema.json'):
        shutil.copy2(root / 'utils/references' / name, target / name)
