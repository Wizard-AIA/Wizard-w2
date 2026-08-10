"""What the user has agreed may leave this machine.

Before this, "local-first" was a property of how someone happened to configure
their .env. A cloud provider assigned to a role would simply be used, and a
prompt carrying real rows would go to it. The promise had no mechanism.

The mode is chosen explicitly and enforced in one place — ``LLMProvider.resolve``,
which every call already passes through, so a session that sets its own
``manager_provider`` cannot route around it.

Three axes, deliberately separate:

* **mode** — which providers a role may resolve to at all.
* **policy** — how much of the data a cloud-bound prompt may carry.
* **tools** — whether a tool that itself calls out may run.

Lives under ``core/`` rather than ``core/llm/`` because Milestone 2's permission
profiles and Milestone 4's connectors are governed by the same choice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.config import settings
from src.providers import CLOUD_PROVIDERS, LOCAL_PROVIDERS, is_cloud, label_for


DATA_MODES: tuple[str, ...] = ("local-only", "cloud-only", "hybrid")

#: Tools that reach the network themselves, independent of which model is behind
#: the role asking for them. Under `local-only` these are unavailable rather than
#: merely unchosen — the spec is explicit that the agent "deciding not to" is not
#: the same guarantee.
OUTBOUND_TOOLS: frozenset[str] = frozenset({"web_search"})


def normalize(mode: str | None) -> str:
    """A known mode, falling back to the configured default."""
    candidate = (mode or "").strip().lower()
    return candidate if candidate in DATA_MODES else settings.data_mode


@dataclass
class DataPolicy:
    """How much of the data a cloud-bound prompt may carry.

    Settable per source as well as per session, because sources are not alike: a
    published reference table and a payroll export do not deserve the same
    answer, and forcing one setting on both means picking the wrong one for one
    of them. Milestone 4's connections are sources too and reuse this field.
    """

    schema_only: bool = True
    per_dataset: dict[str, bool] = field(default_factory=dict)

    @staticmethod
    def _key(name: str) -> str:
        """Case- and whitespace-normalized key, so ``Dataset.CSV`` and ``dataset.csv`` collide.

        Filesystems that hand back the exact bytes a user typed (and browsers,
        and re-uploads) can vary case on a name that is otherwise the same
        dataset. An exact-match dict silently misses those and falls back to
        the session default — which for a cloud provider means real values
        leaking where a schema-only policy was intended.
        """
        return name.strip().lower()

    def schema_only_for(self, dataset: str | None = None, origin: str | None = None) -> bool:
        """The policy in force for one table: its own, then its source's, then the session's.

        ``origin`` is the connection a table was imported from, passed
        explicitly rather than split back out of the dataset name. A prefix test
        would look equivalent and be wrong in a way nobody would notice: an
        uploaded ``sales.csv`` must never inherit a policy set for a connection
        that happens to be called ``sales``.
        """
        if dataset:
            key = self._key(dataset)
            if key in self.per_dataset:
                return self.per_dataset[key]
        if origin:
            key = self._key(origin)
            if key in self.per_dataset:
                return self.per_dataset[key]
        return self.schema_only

    def set_for(self, dataset: str, schema_only: bool) -> None:
        self.per_dataset[self._key(dataset)] = schema_only

    def clear_for(self, dataset: str) -> bool:
        """Drops the override so the dataset follows the session default again."""
        return self.per_dataset.pop(self._key(dataset), None) is not None

    def forget(self, dataset: str) -> None:
        """Called when the dataset is removed, so a re-upload does not inherit it."""
        self.per_dataset.pop(self._key(dataset), None)

    def rekey(self, old_name: str, new_name: str) -> None:
        """Moves an override from ``old_name`` to ``new_name`` (e.g. a connection rename)."""
        overridden = self.per_dataset.pop(self._key(old_name), None)
        if overridden is not None:
            self.per_dataset[self._key(new_name)] = overridden

    def to_dict(self) -> dict[str, object]:
        return {"schema_only": self.schema_only, "per_dataset": dict(self.per_dataset)}


def check_provider(mode: str, provider: str, role: str = "") -> str | None:
    """The refusal sentence for this pairing, or ``None`` when it is allowed.

    Names the mode, the role and the provider: "refused" without those three is
    undebuggable from the UI.
    """
    resolved = normalize(mode)
    where = f" for the {role} role" if role else ""
    label = label_for(provider)

    if resolved == "local-only" and is_cloud(provider):
        return (
            f"{label} is a cloud provider and this session is set to local-only, "
            f"so it cannot be used{where}. Choose a local provider, or change the data mode."
        )
    if resolved == "cloud-only" and not is_cloud(provider):
        return (
            f"{label} runs on this machine and this session is set to cloud-only, "
            f"so it cannot be used{where}. Choose a cloud provider, or change the data mode."
        )
    return None


def allows_provider(mode: str, provider: str) -> bool:
    return check_provider(mode, provider) is None


def allowed_providers(mode: str) -> frozenset[str]:
    resolved = normalize(mode)
    if resolved == "local-only":
        return LOCAL_PROVIDERS
    if resolved == "cloud-only":
        return CLOUD_PROVIDERS
    return LOCAL_PROVIDERS | CLOUD_PROVIDERS


def tool_allowed(mode: str, tool: str) -> bool:
    """Whether a tool that reaches the network itself may run under this mode."""
    return not (normalize(mode) == "local-only" and tool in OUTBOUND_TOOLS)


def tool_refusal(tool: str) -> str:
    return (
        f"This session is set to local-only, so {tool.replace('_', ' ')} is unavailable — "
        "it would send the query off this machine. Change the data mode to allow it."
    )


def disabled_tools(mode: str) -> list[str]:
    """Tools this mode switches off, so the UI can say so before one is reached."""
    return sorted(tool for tool in OUTBOUND_TOOLS if not tool_allowed(mode, tool))


def should_redact(
    mode: str, policy: DataPolicy, provider: str, dataset: str | None = None, origin: str | None = None
) -> bool:
    """Whether a prompt bound for ``provider`` must be stripped of real values.

    Decided per prompt from where that prompt is going, not once per session: under
    hybrid with a cloud manager and a local worker, the planner gets schema only
    and the code generator gets the full picture.
    """
    if not is_cloud(provider):
        return False
    if normalize(mode) == "local-only":
        # Unreachable in practice — resolution refuses first — but a redaction
        # helper that says "send everything" for a forbidden provider is the
        # wrong thing to leave lying around.
        return True
    return policy.schema_only_for(dataset, origin)


def describe_mode(mode: str) -> str:
    """One sentence, for the UI and for an error the user has to act on."""
    return {
        "local-only": "Every model runs on this machine. Nothing is sent anywhere.",
        "cloud-only": "Every model is called over the network.",
        "hybrid": "You assign each role its own provider. Cloud-bound prompts follow the data policy.",
    }.get(normalize(mode), "")


__all__ = [
    "DATA_MODES",
    "OUTBOUND_TOOLS",
    "DataPolicy",
    "allowed_providers",
    "allows_provider",
    "check_provider",
    "describe_mode",
    "disabled_tools",
    "normalize",
    "should_redact",
    "tool_allowed",
    "tool_refusal",
]
