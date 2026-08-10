"""Shared test configuration.

Environment is pinned *before* any ``src`` import because ``src.config.Settings``
is instantiated at module import time. In particular ``SANDBOX_ENABLED=false``
guarantees the suite never contacts a Docker daemon — the previous suite started
a real container as a side effect of importing the FastAPI app, which is what
made CI depend on the runner having Docker.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="wizard-test-data-"))
TEST_WORKSPACE_DIR = Path(tempfile.mkdtemp(prefix="wizard-test-ws-"))
# Nothing in the suite may read or write the developer's real credentials file.
TEST_CONFIG_DIR = Path(tempfile.mkdtemp(prefix="wizard-test-config-"))
# Both skill roots the suite could otherwise pick up from the machine it runs on.
TEST_SKILLS_BUILTIN_DIR = Path(tempfile.mkdtemp(prefix="wizard-test-skills-builtin-"))
TEST_SKILLS_PROJECT_DIR = Path(tempfile.mkdtemp(prefix="wizard-test-skills-project-"))

os.environ.update(
    {
        "ENV": "test",
        "SANDBOX_ENABLED": "false",
        # Pin the backend explicitly. `SANDBOX_ENABLED=false` alone now means
        # "no Docker", and the default `host` would spawn a *subprocess* that
        # imports pandas for every session the suite creates. The in-process
        # interpreter is what these tests exercise and assert on.
        "EXECUTION_BACKEND": "inprocess",
        # Pinned rather than inherited, so each test states the sandbox mode it
        # means to exercise. Nothing in the suite spawns a child to contain, and
        # `require` would make policy construction raise where a test only meant
        # to read it back.
        "HOST_SANDBOX": "off",
        "API_PROVIDER": "ollama",
        "DATA_DIR": str(TEST_DATA_DIR),
        "WORKSPACE_DIR": str(TEST_WORKSPACE_DIR),
        "LOG_DIR": str(TEST_DATA_DIR / "logs"),
        "REDIS_URL": "",
        "API_KEY": "",
        # Model discovery is the one component that dials out on its own. Port 1
        # on loopback is refused instantly, so provider tests assert the
        # unreachable path without waiting on a connect timeout (or resolving
        # `host.docker.internal`, which is a real host on some dev machines).
        "OLLAMA_BASE_URL": "http://127.0.0.1:1",
        "LMSTUDIO_BASE_URL": "http://127.0.0.1:1",
        # Same reason, for the cloud providers: `available_providers` and the
        # registry must never dial out to a real endpoint during a test.
        "OPENAI_BASE_URL": "http://127.0.0.1:1",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:1",
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "GATEWAY_API_URL": "",
        "GATEWAY_API_KEY": "",
        # Pinned rather than derived. The default is `local-only`, which would
        # refuse every cloud provider the suite exercises; `hybrid` lets each
        # test state the mode it means to test.
        "DATA_MODE": "hybrid",
        "DATA_SCHEMA_ONLY": "false",
        # Pinned rather than inherited so each test states the profile it means
        # to exercise. `ask-always` is also the shipped default, so the suite
        # runs against the behaviour a fresh install actually gets.
        "AGENT_PERMISSION_PROFILE": "ask-always",
        # Short, because a test that reaches this has already gone wrong: nothing
        # in the suite has a client to answer a consent prompt, so the only way
        # a run should ever wait here is a bug. Two seconds fails it visibly
        # instead of stalling the run for two minutes.
        "AGENT_CONSENT_TIMEOUT": "2",
        # Also where `connections.json` and the user-global skills directory
        # land, so no test can read or write a developer's real saved connections
        # or their own skills either.
        "WIZARD_CONFIG_DIR": str(TEST_CONFIG_DIR),
        # The other two skill roots, which `WIZARD_CONFIG_DIR` does not cover.
        # Empty means "derive it", and the derived answers are the shipped
        # `backend/skills/` and `.wizard/skills` under the working directory --
        # so without these the suite's behaviour would depend on what Wizard
        # happens to ship and on what the developer left in their checkout. Both
        # are pinned empty; a test wanting a skill writes one into a root it
        # points the registry at itself.
        "SKILLS_BUILTIN_DIR": str(TEST_SKILLS_BUILTIN_DIR),
        "SKILLS_PROJECT_DIR": str(TEST_SKILLS_PROJECT_DIR),
        # Installing a skill is the one thing in this package that dials out, so
        # it gets the same treatment as the provider URLs below: a port that
        # refuses instantly. Every install test injects a fake fetcher; this is
        # what turns "somebody forgot to" into an immediate failure instead of a
        # request to github.com from a test run.
        "SKILLS_REGISTRY_API": "http://127.0.0.1:1",
        "SKILLS_FETCH_TIMEOUT": "2",
        # Bounds every connector import in the suite. Small, because no test
        # needs a large frame to prove a read happened, and a ceiling nobody
        # crosses is a ceiling nobody has tested.
        "CONNECTOR_MAX_ROWS": "1000",
        # A connector that somehow reaches a real host has to fail while the
        # suite is still running, not after a driver's 30-second default. Same
        # reasoning as the port-1 provider URLs and AGENT_CONSENT_TIMEOUT above.
        # Note what this cannot do: no environment variable stops SQLAlchemy
        # dialling a DSN. That guarantee comes from the suite using SQLite and a
        # fake connector, never a networked one.
        "CONNECTOR_TIMEOUT": "2",
        "COUNCIL_ENABLED": "false",
        "VISION_ENABLED": "false",
        "RATE_LIMIT_MAX_REQUESTS": "10000",
        # Never download a transformer during a test run: it is slow and makes
        # the suite depend on network access.
        "EMBEDDINGS_FORCE_FALLBACK": "true",
        # The suite instantiates the app through `with TestClient(app) as client`
        # (see tests/integration/test_api.py and others), which does run FastAPI's
        # lifespan -- so an unpinned default here would spawn an llm-warmup thread
        # against OLLAMA_BASE_URL=http://127.0.0.1:1 on every such test. It fails
        # fast (refused instantly, not a timeout) and never raises, but "tests
        # never touch the network" should hold even for an attempt that fails on
        # purpose -- same reasoning as the port-1 provider URLs above.
        "LLM_WARM_ON_STARTUP": "false",
        # Same reasoning as LLM_WARM_ON_STARTUP above: the real llm_provider is
        # monkeypatched out in orchestrator tests, but any test exercising the
        # actual singleton should still get a pure no-op here, not a dial to
        # the port-1 provider URLs.
        "LLM_RELEASE_IDLE_MODELS": "false",
    }
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import matplotlib  # noqa: E402


matplotlib.use("Agg")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from src.core.agent.consent import consent_broker  # noqa: E402
from src.core.connectors.store import connection_store  # noqa: E402
from src.core.credentials import credential_store  # noqa: E402
from src.core.database import db_mgr  # noqa: E402
from src.core.llm.usage import usage_ledger  # noqa: E402
from src.core.semantic_cache import semantic_cache  # noqa: E402
from src.core.session import Session, session_manager  # noqa: E402
from src.core.skills import install as skill_install  # noqa: E402
from src.core.skills.index import install_index  # noqa: E402
from src.core.skills.registry import skill_registry  # noqa: E402


# --------------------------------------------------------------------------- #
# DataFrame fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def empty_df() -> pd.DataFrame:
    return pd.DataFrame()


@pytest.fixture
def simple_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "A": [1, 2, 3, 4, 5],
            "B": ["x", "y", "z", "w", "v"],
            "C": [0.1, 0.2, 0.3, 0.4, 0.5],
        }
    )


@pytest.fixture
def tips_df() -> pd.DataFrame:
    """Deterministic stand-in for the classic tips dataset."""
    rng = np.random.default_rng(0)
    size = 60
    return pd.DataFrame(
        {
            "total_bill": np.round(rng.uniform(5, 50, size), 2),
            "tip": np.round(rng.uniform(1, 10, size), 2),
            "sex": rng.choice(["Male", "Female"], size),
            "smoker": rng.choice(["Yes", "No"], size),
            "day": rng.choice(["Thur", "Fri", "Sat", "Sun"], size),
            "size": rng.integers(1, 6, size),
        }
    )


@pytest.fixture
def missing_values_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "A": [1.0, 2.0, np.nan, 4.0],
            "B": ["x", None, "z", "w"],
            "C": [np.inf, 1.0, -np.inf, 2.0],
        }
    )


@pytest.fixture
def wide_df() -> pd.DataFrame:
    """120 columns — exercises the prompt-context column budget."""
    rng = np.random.default_rng(1)
    return pd.DataFrame({f"feature_{index}": rng.normal(size=25) for index in range(120)})


# --------------------------------------------------------------------------- #
# Session fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def session() -> Session:
    """A live session, disposed afterwards so state never leaks between tests."""
    created = session_manager.create()
    yield created
    session_manager.drop(created.id)


@pytest.fixture
def loaded_session(session: Session, simple_df: pd.DataFrame) -> Session:
    session.add_dataset("dataset.csv", simple_df.copy())
    return session


@pytest.fixture(autouse=True)
def _never_reach_a_package_index(monkeypatch):
    """Stops an approved `library_install` gate from running a real `pip`.

    Consent to install is now acted on in the parent process rather than
    reactively inside the daemon, which put a real network call directly behind
    a gate that several tests approve on purpose. The suite ran `pip install`
    against PyPI for six hundred seconds once before this existed.

    Stubbed rather than pinned through settings, because the setting also
    decides whether the gate is *offered* — turning it off would silence the
    consent tests instead of protecting them.
    """
    from src.core.tools import packages

    monkeypatch.setattr(packages, "install", lambda *args, **kwargs: (True, "install stubbed for tests"))


@pytest.fixture(autouse=True)
def _clean_database():
    """Keeps cross-test pollution out of the shared cache.

    This has to go through ``semantic_cache.clear()`` rather than
    ``db_mgr.clear_cache()``: ``store()`` writes to the SQLite table *and* to the
    in-process exact-match cache, so clearing only the table leaves a live entry
    that makes a later test with the same question take the cache-hit path.

    The usage ledger is cleared for the same reason: it accumulates per session
    id, and a test asserting on a turn's cost must not inherit another's tokens.

    Skill candidates get the same treatment for a sharper version of the same
    reason: they deliberately have no ``session_id`` — "you keep doing this" is a
    claim about many sessions — so nothing else clears them, and an occurrence
    count carried forward makes the promotion threshold fire in a test that never
    asked a question twice.

    Outstanding consent requests are released last. A test that ends while a run
    is parked on one would otherwise leave a future nobody resolves, and the next
    test to touch that session id would inherit it.
    """
    yield
    semantic_cache.clear()
    usage_ledger.clear()
    credential_store.reload()
    db_mgr.clear_skill_candidates()
    db_mgr.clear_skill_usage()
    skill_registry.clear_user_skills()
    # The install index and the staging root live in the config directory, which
    # `clear_user_skills` does not touch: it removes skill *files* by path, and
    # neither of these is one. A record carried forward would offer an update for
    # a skill the next test does not have, and a staged skill would show up in a
    # pending list a test wrote nothing into.
    install_index.clear()
    skill_install.clear_pending()
    # Cleared, not merely reloaded. Connections persist to disk on purpose --
    # they are configuration, not session data -- so without this a connection
    # saved by one test is still there for the next, which sees a name conflict
    # rather than the empty store it was written against.
    connection_store.clear()
    consent_broker.reset()


@pytest.fixture
def stub_llm(monkeypatch):
    """Installs a scripted LLM into every module that reached for the provider.

    Shared rather than per-file: the loop tests and the workflow tests drive the
    same orchestrator, and two copies of this fixture would drift.
    """
    from stubs import ScriptedLLM

    def install(responses: list[str]) -> ScriptedLLM:
        stub = ScriptedLLM(responses)
        for target in ("src.core.agent.orchestrator.llm_provider", "src.core.agent.flow.llm_provider"):
            module, _, attribute = target.rpartition(".")
            monkeypatch.setattr(f"{module}.{attribute}", stub, raising=False)
        return stub

    return install


@pytest.fixture
def csv_file(tmp_path: Path, simple_df: pd.DataFrame) -> Path:
    path = tmp_path / "sample.csv"
    simple_df.to_csv(path, index=False)
    return path
