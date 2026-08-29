"""High-level entry points that wrap the async orchestrator.

Kept so the CLI and the non-streaming REST path have a simple surface, and so
existing imports of ``science_agent`` continue to resolve.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pandas as pd

from src.config import settings
from src.core.agent.events import EventCollector
from src.core.agent.orchestrator import RunResult, orchestrator
from src.core.data_mode import should_redact
from src.core.execution import ExecutionResult
from src.core.llm import LLMRole, llm_provider
from src.core.prompts import create_cleaning_prompt
from src.core.tools import runtime as runtime_backend
from src.core.tools.catalog import CatalogEngine
from src.utils.logging import logger, trace_agent


if TYPE_CHECKING:
    from src.core.session import Session


def _run_sync(coro):
    """Runs a coroutine from synchronous code.

    Only valid when there is no loop already running on this thread, which is the
    case for the CLI and for handlers dispatched through ``asyncio.to_thread``.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("_run_sync called from inside a running event loop; await the coroutine instead.")


#: Object columns sampled when deciding whether a frame needs cleaning. The
#: check has to be cheap enough to be worth doing -- it exists to *avoid* work.
_CLEAN_CHECK_ROWS = 200
_CLEAN_CHECK_COLUMNS = 40


def _needs_cleaning(df: pd.DataFrame) -> bool:
    """Whether anything the cleaning prompt asks for actually applies here.

    Mirrors the three concrete rules in :func:`create_cleaning_prompt`: missing
    values, text that is really a number or a date, and untrimmed whitespace.
    Anything else the model might do is out of scope by that prompt's own rules,
    so finding none of these means the call would return ``pass``.

    Deliberately conservative: an unreadable column counts as "might need
    cleaning" and the model is asked. Skipping a needed clean is a data bug;
    running an unneeded one only costs time.
    """
    if df.empty:
        return False
    if df.isna().to_numpy().any():
        return True

    for column in list(df.columns)[:_CLEAN_CHECK_COLUMNS]:
        series = df[column]
        if not (pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)):
            continue
        sample = series.dropna().head(_CLEAN_CHECK_ROWS)
        if sample.empty:
            continue
        text = sample.astype(str)
        if (text != text.str.strip()).any():
            return True
        try:
            if pd.to_numeric(text, errors="coerce").notna().all():
                return True
            if pd.to_datetime(text, errors="coerce", format="mixed").notna().all():
                return True
        except (ValueError, TypeError):
            return True

    return False


class ScientificAgent:
    """Synchronous façade over :class:`AnalysisOrchestrator`."""

    @trace_agent("ScientificAgent")
    def run(
        self,
        instruction: str,
        session: Session,
        mode: str = "planning",
        approved_plan: str | None = None,
    ) -> tuple[str, str, str | None, str | None, str]:
        """Legacy 5-tuple interface: ``(result, code, image, thought, status)``."""
        collector = EventCollector()
        result: RunResult = _run_sync(
            orchestrator.run(
                session=session,
                instruction=instruction,
                mode=mode,
                emitter=collector,
                approved_plan=approved_plan,
            )
        )
        ui_status = "waiting_confirmation" if result.status == "awaiting_approval" else result.status
        answer = result.answer or result.plan
        return answer, result.code, result.image, result.thought, ui_status

    # ------------------------------------------------------------------ #
    @staticmethod
    def clean_dataset(
        df: pd.DataFrame, session: Session, dataset: str | None = None
    ) -> tuple[pd.DataFrame, dict[str, Any], str]:
        """Profiles the frame and applies a model-authored cleaning script.

        The script is executed through :class:`CodeExecutor`, so it is statically
        screened and runs inside the container. The previous implementation called
        the builtin ``exec()`` directly in the API process with no screening at
        all -- on every upload, with column names from the uploaded file already
        embedded in the prompt.
        """
        logger.info("Starting semantic cleaning")
        catalog = CatalogEngine.analyze(df)

        # Asked before the model is: every upload used to buy a worker
        # round-trip and a sandbox execution to be told the data was already
        # fine, which on a local model is the slowest thing that happens between
        # choosing a file and seeing it appear. The prompt's own rules name
        # exactly three problems, so all three can simply be looked for.
        if not _needs_cleaning(df):
            return df, catalog, "No cleaning was necessary."

        worker_provider = settings.resolve_provider(session.models.worker_provider)
        try:
            response = llm_provider.complete(
                create_cleaning_prompt(
                    df,
                    catalog,
                    # Cleaning is the first prompt a file is ever put into, so it
                    # has to honour that file's own policy, not the session's.
                    redact=should_redact(session.data_mode, session.data_policy, worker_provider, dataset=dataset),
                ),
                role=LLMRole.WORKER,
                model=session.models.worker,
                provider=session.models.worker_provider,
                max_tokens=settings.output_budget("code"),
                data_mode=session.data_mode,
                session_id=session.id,
            )
        except Exception as exc:
            logger.warning("Cleaning skipped, model unavailable", error=str(exc))
            return df, catalog, "Automatic cleaning was skipped because the model was unavailable."

        code = orchestrator._extract_code(response)
        if not code or code.strip() in {"pass", ""}:
            return df, catalog, "No cleaning was necessary."

        # Persist the frame so the sandbox sees it, then run the script and read back.
        session.add_dataset("dataset.csv", df, catalog=catalog, make_active=True)
        session.executor.reload_dataset()

        # Not a literal `/workspace`: a local runtime writes into the session's
        # own directory, and the container path resolves to nowhere there.
        write_target = runtime_backend.workspace_path(session.id, "cleaned.csv")
        wrapped = f"{code}\n\ndf.to_csv({write_target!r}, index=False)\nprint('CLEANING_OK', len(df))\n"
        result: ExecutionResult = session.executor.execute(wrapped, df)

        if not result.ok:
            # The *tail*, not the head: Python prints the exception last, so the
            # first 300 characters of a traceback are stack frames from inside
            # pandas and never say what actually went wrong.
            logger.warning("Cleaning script failed; keeping the raw data", detail=result.output[-600:])
            return df, catalog, "Automatic cleaning was skipped because the generated script failed."

        cleaned_path = session.workspace / "cleaned.csv"
        if not cleaned_path.exists():
            return df, catalog, "Automatic cleaning produced no output; the original data was kept."

        try:
            cleaned = pd.read_csv(cleaned_path)
        except Exception as exc:
            logger.warning("Could not read cleaned output", error=str(exc))
            return df, catalog, "Automatic cleaning output could not be read; the original data was kept."
        finally:
            cleaned_path.unlink(missing_ok=True)

        if cleaned.empty:
            return df, catalog, "Automatic cleaning emptied the dataset, so the original was kept."

        # Reject a script that discarded most of the data.
        if len(cleaned) < len(df) * 0.5:
            logger.warning("Cleaning dropped too many rows", before=len(df), after=len(cleaned))
            return df, catalog, "Automatic cleaning was rejected because it removed too many rows."

        rows_removed = len(df) - len(cleaned)
        summary = (
            f"Cleaned: {rows_removed:,} row(s) removed, {len(cleaned.columns)} columns retained."
            if rows_removed
            else "Cleaned: types normalised, no rows removed."
        )
        return cleaned, CatalogEngine.analyze(cleaned), summary


science_agent = ScientificAgent()
