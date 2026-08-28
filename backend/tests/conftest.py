import sys
from datetime import date
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.contracts import get_contract_store  # noqa: E402
from app.engines.reconciler import DataReconciler  # noqa: E402
from app.pipeline import DetectionPipeline  # noqa: E402

BASELINE_WINDOW = (date(2023, 10, 1), date(2023, 11, 1))
EVENT_WINDOW = (date(2023, 11, 1), date(2023, 12, 1))
OUTAGE_WINDOW = (date(2023, 11, 1), date(2023, 11, 8))


@pytest.fixture(scope="session")
def store():
    return get_contract_store()


@pytest.fixture(scope="session")
def reconciler():
    r = DataReconciler()
    yield r
    r.close()


@pytest.fixture(scope="session")
def periods(reconciler):
    return reconciler.period_summary("2023-10"), reconciler.period_summary("2023-11")


@pytest.fixture(scope="session")
def pipeline(reconciler, store):
    return DetectionPipeline(reconciler=reconciler, store=store)


@pytest.fixture(scope="session")
def provider(pipeline):
    return pipeline.provider


@pytest.fixture(scope="session")
def analysis(pipeline):
    return pipeline.analyse(BASELINE_WINDOW, EVENT_WINDOW)


@pytest.fixture(scope="session")
def outage_analysis(pipeline):
    return pipeline.analyse(BASELINE_WINDOW, OUTAGE_WINDOW)
