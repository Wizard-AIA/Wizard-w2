"""Resource limits derived from the machine, rather than chosen for a server.

`SYSTEM_PROFILE` was declared in config, in .env.example and in docker-compose,
and read by absolutely nothing -- so every limit was a constant: eight inference
threads on a four-core laptop, and `SESSION_MAX_ACTIVE=32` at 2 GB each, which
permits sixty-four gigabytes of containers on a machine that may have four.
"""

from __future__ import annotations

import pytest

from src.config import Settings, format_memory, parse_memory, tier_for_parameter_size
from src.utils.hostinfo import host_info


@pytest.fixture(autouse=True)
def _no_developer_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here asserts what Settings derives from the machine.

    Settings also reads `.env` and `backend/.env`, so a developer's own file
    (say `LLM_NUM_CTX=4096`) overrode the derived value and failed the suite on
    that one machine while CI, which has no `.env`, stayed green.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)


# --------------------------------------------------------------------------- #
# Memory strings
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2g", 2 * 1024**3),
        ("512m", 512 * 1024**2),
        ("2GiB", 2 * 1024**3),
        ("2gb", 2 * 1024**3),
        ("1.5g", int(1.5 * 1024**3)),
        ("4096", 4096),
    ],
)
def test_memory_strings_parse(text: str, expected: int) -> None:
    assert parse_memory(text) == expected


@pytest.mark.parametrize("junk", ["", "   ", "junk", "gg", None, 0, -5])
def test_unparseable_memory_is_none_not_an_exception(junk) -> None:
    """A typo in a hand-edited .env should cost a default, not a failed boot."""
    assert parse_memory(junk) is None


def test_formatted_memory_is_a_value_someone_could_have_typed() -> None:
    """A derived limit printed as `2057637k` reads like a measurement it is not."""
    assert format_memory(int(15.7 * 1024**3) // 8) == "1g"
    assert format_memory(3 * 1024**3) == "3g"
    assert format_memory(700 * 1024**2) == "512m"
    assert parse_memory(format_memory(2 * 1024**3)) == 2 * 1024**3


# --------------------------------------------------------------------------- #
# Host detection and sizing
# --------------------------------------------------------------------------- #
def test_host_is_measured_not_assumed() -> None:
    host = host_info()
    assert host.cores >= 1
    assert host.logical_cores >= host.cores
    if host.ram_bytes is not None:
        assert host.ram_bytes > 512 * 1024**2
    assert host.profile in ("laptop", "server", "hpc")


def test_resource_limits_are_derived_from_the_machine() -> None:
    """These used to be constants chosen for a server.

    Eight inference threads on a four-core laptop is contention, and the shipped
    `SESSION_MAX_ACTIVE=32` at `SANDBOX_MEM_LIMIT=2g` permitted sixty-four
    gigabytes of containers on a machine that may have four.
    """
    settings = Settings()
    host = host_info()

    assert settings.LLM_NUM_THREAD <= max(2, min(16, host.cores))
    assert settings.QUEUE_MAX_WORKERS >= 1

    if host.ram_bytes:
        per_runtime = parse_memory(settings.SANDBOX_MEM_LIMIT)
        assert per_runtime is not None
        # Concurrent runtimes must not be able to claim more than half of RAM.
        assert settings.SESSION_MAX_ACTIVE * per_runtime <= host.ram_bytes


def test_an_explicit_value_always_wins() -> None:
    settings = Settings(LLM_NUM_THREAD=3, SANDBOX_MEM_LIMIT="7g", SESSION_MAX_ACTIVE=99)
    assert settings.LLM_NUM_THREAD == 3
    assert settings.SANDBOX_MEM_LIMIT == "7g"
    assert settings.SESSION_MAX_ACTIVE == 99


def test_a_blank_setting_is_treated_as_unset() -> None:
    """`docker-compose.yml` passes optional knobs as `${VAR:-}`.

    An empty environment variable is still *present*, so without this every
    compose deployment would look as though the operator had chosen "" and
    would run with no memory limit at all.
    """
    settings = Settings(SANDBOX_MEM_LIMIT="")
    assert parse_memory(settings.SANDBOX_MEM_LIMIT) is not None


def test_system_profile_resolves_auto_but_honours_a_pin() -> None:
    assert Settings(SYSTEM_PROFILE="hpc").system_profile == "hpc"
    assert Settings(SYSTEM_PROFILE="auto").system_profile == host_info().profile


def test_the_agent_tier_is_still_the_models_business_not_the_hosts() -> None:
    """Host sizing must not have leaked into the *reasoning* budget.

    How many iterations the agent gets depends on the model behind it; how much
    memory a runtime gets depends on the machine. Conflating them would give a
    frontier gateway model a compact leash for running on a laptop.
    """
    assert tier_for_parameter_size("70B") == "full"
    assert Settings(SYSTEM_PROFILE="laptop").budget_for("auto", "70B").tier == "full"


# --------------------------------------------------------------------------- #
# Inference sizing
#
# These are not comfort settings. `LLM_NUM_CTX` is a *load-time* parameter: it
# fixes the KV cache the provider reserves for every resident model, so asking
# for more than the prompts reach evicts the worker to make room for the manager
# on every iteration of the loop -- and each eviction costs a reload from disk.
# --------------------------------------------------------------------------- #
def test_context_length_is_sized_to_the_machine() -> None:
    settings = Settings()
    assert settings.LLM_NUM_CTX in (8192, 16384, 32768)
    assert settings.LLM_NUM_CTX == {"laptop": 8192, "server": 16384, "hpc": 32768}[host_info().profile]


def test_an_explicit_context_length_is_respected() -> None:
    assert Settings(LLM_NUM_CTX=4096).LLM_NUM_CTX == 4096


def test_a_zero_means_derive_it() -> None:
    """`0` is the shipped default and reads as "unset" rather than "no context".

    A plain absence would work too, but `.env` files are copied between machines
    and an explicit `0` states the intent to let the host decide.
    """
    assert Settings(LLM_NUM_CTX=0).LLM_NUM_CTX >= 8192
    assert Settings(LLM_NUM_THREAD=0).LLM_NUM_THREAD >= 2


def test_the_docker_hostname_is_not_used_outside_a_container() -> None:
    """`host.docker.internal` is a name Docker Desktop adds to the hosts file.

    It resolves on a dev machine that has Docker and fails outright on one that
    does not -- which is exactly the Docker-less install the local execution
    backend exists to serve. The shipped default therefore has to be corrected
    for where the backend actually is, which is already measured.
    """
    settings = Settings()
    if host_info().containerised:
        pytest.skip("running inside a container, where the Docker hostname is correct")
    assert "host.docker.internal" not in settings.OLLAMA_BASE_URL
    assert "host.docker.internal" not in settings.LMSTUDIO_BASE_URL
    assert "127.0.0.1" in settings.OLLAMA_BASE_URL


def test_an_explicit_url_is_never_rewritten() -> None:
    """Compose passes the Docker hostname itself, and must keep it."""
    pinned = Settings(OLLAMA_BASE_URL="http://host.docker.internal:11434")
    assert pinned.OLLAMA_BASE_URL == "http://host.docker.internal:11434"


# --------------------------------------------------------------------------- #
# Output budgets
# --------------------------------------------------------------------------- #
def test_each_kind_of_call_gets_its_own_output_budget() -> None:
    """One number for every call meant a decision could run to 4096 tokens."""
    settings = Settings()
    assert settings.output_budget("decision") < settings.output_budget("code")
    assert settings.output_budget("review") < settings.output_budget("answer")
    for purpose in ("plan", "decision", "code", "answer", "review"):
        assert settings.output_budget(purpose) < settings.MAX_TOKENS


def test_an_unknown_purpose_falls_back_to_the_ceiling() -> None:
    assert Settings().output_budget("something-new") == Settings().MAX_TOKENS


# --------------------------------------------------------------------------- #
# Tier shape
# --------------------------------------------------------------------------- #
def test_only_a_compact_model_is_spared_the_decision_round_trip() -> None:
    """Asking a 1.5B model to choose an action costs a call and buys nothing."""
    assert not Settings(AGENT_TIER="compact").budget_for("auto").allow_decisions
    assert Settings(AGENT_TIER="balanced").budget_for("auto").allow_decisions
    assert Settings(AGENT_TIER="full").budget_for("auto").allow_decisions


def test_deep_restores_the_decision_round_trip_even_on_a_compact_model() -> None:
    """The tier's answer is a default about cost, and `deep` accepts the cost.

    Leaving it off would make the composer's Deep control a no-op on exactly the
    setup where someone is most likely to reach for it -- a small model that gave
    a shallow first answer.
    """
    deep = Settings(AGENT_TIER="compact").budget_for("deep")
    assert deep.allow_decisions
    assert deep.iterations > Settings(AGENT_TIER="compact").budget_for("auto").iterations


def test_fast_never_pays_for_a_decision() -> None:
    """One iteration, so there is no next action to choose."""
    assert Settings(AGENT_TIER="full").budget_for("fast").iterations == 1
