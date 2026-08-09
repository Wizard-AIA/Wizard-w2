"""Single owner of text embeddings, in whatever form this install can provide.

Every consumer -- semantic cache, feedback few-shots, trajectory memory, RAG
retrieval, document search -- goes through one service, so a vector is produced
the same way wherever it is compared.

Why this no longer defaults to ``sentence-transformers``
-------------------------------------------------------
That package depends on torch, and on Linux/x86_64 torch declares eleven
``nvidia-*-cu12`` wheels as hard requirements, installed whether or not the
machine has a GPU. Measured against the pinned versions that is ~2.8 GB of
compressed wheels -- roughly six gigabytes installed -- to run a 90 MB MiniLM
model. It was the single largest thing in the backend image, larger than the
entire analysis sandbox.

The model server this app already talks to can embed, so it is asked instead:

1. **the selected provider** -- Ollama's ``POST /api/embed``, or the OpenAI-style
   ``POST /v1/embeddings`` that LM Studio and every gateway expose. Costs
   nothing on disk and follows whichever backend the user chose.
2. **local sentence-transformers**, if the user installed it deliberately.
3. **a hashing encoder**, which is not semantic but is stable, instant and
   works with no model and no network -- so nothing ever hard-fails.

Nothing here raises. A retrieval feature degrading is always better than a
question failing.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import numpy as np

from src.config import settings
from src.core.data_mode import allows_provider
from src.utils.logging import logger


HASH_DIM = 384  # matches all-MiniLM-L6-v2 so stored vectors stay interchangeable in size

#: Caps the connection pool of the module-process-lifetime httpx client below.
#: Left unbounded, a burst of concurrent encode calls (several per question,
#: across sessions) opens one socket each with no ceiling -- unbounded socket
#: growth against a single embedding endpoint is a resource-exhaustion vector
#: in its own right, independent of anything the remote host does.
_HTTP_LIMITS_MAX_CONNECTIONS = 20
_HTTP_LIMITS_MAX_KEEPALIVE = 10


class RemoteHostBlocked(Exception):
    """Raised when a configured embedding endpoint resolves to a host we refuse to call."""


def _assert_host_allowed(url: str) -> None:
    """Refuses link-local and other non-routable targets before they are dialled.

    The embedding endpoint's base URL is provider config a user can point
    anywhere from the settings UI, and this call happens server-side with no
    further review -- the shape of SSRF. This app is local-first and
    routinely talks to a model server on the loopback or local network, so
    private/loopback ranges stay reachable; what is refused is the class of
    address that exists specifically to be reachable *only* from the host
    itself for purposes other than "the user's own model server" -- cloud
    metadata services live at a link-local address (169.254.169.254) for
    exactly that reason.
    """
    host = urlsplit(url).hostname
    if not host:
        raise RemoteHostBlocked(f"Embedding endpoint URL has no host: {url!r}")
    try:
        addresses = {str(info[4][0]) for info in socket.getaddrinfo(host, None)}
    except OSError as exc:
        raise RemoteHostBlocked(f"Could not resolve embedding endpoint host {host!r}: {exc}") from exc

    for raw in addresses:
        address = ipaddress.ip_address(raw.split("%", 1)[0])
        if address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved:
            raise RemoteHostBlocked(f"Embedding endpoint host {host!r} resolves to a disallowed address {address}.")

#: How long to wait before retrying a remote encoder that could not be reached.
#: Without this every single encode on an offline machine pays a connect
#: timeout, and encodes happen several times per question.
REMOTE_RETRY_SECONDS = 120.0

#: Consecutive failures back off from ``REMOTE_RETRY_SECONDS`` up to this. A
#: fixed window means a machine whose provider genuinely cannot embed pays the
#: full timeout again every two minutes, forever. Doubling turns that into a
#: handful of attempts and then near-silence, while still recovering on its own
#: if the model server is started later.
REMOTE_RETRY_MAX_SECONDS = 1800.0


def cosine_similarity(a: np.ndarray | None, b: np.ndarray | None) -> float:
    """Cosine similarity that is total: unusable inputs score 0.0 rather than raising."""
    if a is None or b is None:
        return 0.0
    a = np.asarray(a, dtype=np.float32).ravel()
    b = np.asarray(b, dtype=np.float32).ravel()
    if a.size == 0 or b.size == 0 or a.size != b.size:
        return 0.0
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


class _HashingEncoder:
    """Offline fallback: bag-of-tokens hashed into a fixed-width vector.

    Not semantically meaningful across paraphrases, but it is stable, cheap and
    makes exact/near-duplicate lookups work without any model download.
    """

    dimension = HASH_DIM

    def encode(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dimension, dtype=np.float32)
        for token in str(text).lower().split():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[index] += sign
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        return vec


class _RemoteEncoder:
    """Embeddings from the model server the app is already configured against.

    Ollama and the OpenAI-compatible servers differ in both the path and the
    response shape, which is the whole of the difference between them here.
    """

    def __init__(self, provider: str, model: str):
        self.provider = provider
        self.model = model
        self._client: Any = None
        self._lock = threading.Lock()

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"

    def _http(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    import httpx

                    self._client = httpx.Client(
                        timeout=settings.EMBEDDING_TIMEOUT,
                        limits=httpx.Limits(
                            max_connections=_HTTP_LIMITS_MAX_CONNECTIONS,
                            max_keepalive_connections=_HTTP_LIMITS_MAX_KEEPALIVE,
                        ),
                    )
        return self._client

    def _endpoint_and_payload(self, texts: list[str]) -> tuple[str, dict[str, Any]]:
        if self.provider == "ollama":
            root = settings.provider_root_url(self.provider).rstrip("/")
            payload: dict[str, Any] = {"model": self.model, "input": texts}
            keep_alive = self._keep_alive()
            if keep_alive:
                payload["keep_alive"] = keep_alive
            return f"{root}/api/embed", payload
        base = settings.provider_openai_base_url(self.provider).rstrip("/")
        return f"{base}/embeddings", {"model": self.model, "input": texts}

    def _keep_alive(self) -> str:
        """How long Ollama should hold this model, sized from the shared residency plan.

        Ollama's own server default is `-1` (forever) when nothing is sent at
        all -- which is exactly what let an embedding model permanently
        occupy a slot regardless of what the manager/worker were doing. Best
        effort: a planning failure must not stop an embed call, so this falls
        back to the app's own deliberate default rather than Ollama's silent
        "forever".
        """
        if self.provider != "ollama":
            return ""
        try:
            from src.core.llm import llm_provider

            return llm_provider.resident_plan(self.provider).keep_alive
        except Exception as exc:  # pragma: no cover - planning must never block an embed call
            logger.debug("Could not size embedding keep-alive; using the default", error=str(exc))
            return settings.LLM_KEEP_ALIVE

    @staticmethod
    def _vectors_from(payload: dict[str, Any]) -> list[list[float]]:
        # Ollama: {"embeddings": [[...]]}. OpenAI-compatible: {"data": [{"embedding": [...]}]},
        # which is *not* guaranteed to come back in request order, so it is sorted by index.
        if isinstance(payload.get("embeddings"), list):
            return payload["embeddings"]
        data = payload.get("data")
        if isinstance(data, list):
            ordered = sorted(data, key=lambda row: row.get("index", 0) if isinstance(row, dict) else 0)
            return [row.get("embedding", []) for row in ordered if isinstance(row, dict)]
        return []

    def encode_many(self, texts: list[str], timeout: float | None = None) -> list[np.ndarray] | None:
        """Vectors for ``texts``, or ``None`` if this encoder could not serve them.

        ``timeout`` overrides the client default for this call alone. The first
        call to a model server is not the same operation as the rest: it has to
        load the model off disk first, which on a cold laptop takes longer than
        any sane steady-state timeout. Judging both by one number rejected an
        encoder that answers in 50ms once it is resident.
        """
        url, payload = self._endpoint_and_payload(texts)
        try:
            _assert_host_allowed(url)
        except RemoteHostBlocked as exc:
            logger.warning("Refusing to call embedding endpoint", provider=self.provider, error=str(exc))
            return None
        headers = {}
        key = settings.provider_api_key(self.provider)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            request_timeout = settings.EMBEDDING_TIMEOUT if timeout is None else timeout
            response = self._http().post(url, json=payload, headers=headers, timeout=request_timeout)
            response.raise_for_status()
            vectors = self._vectors_from(response.json())
        except Exception as exc:
            logger.warning("Remote embedding failed", provider=self.provider, model=self.model, error=str(exc))
            return None

        if len(vectors) != len(texts) or not all(vectors):
            logger.warning(
                "Remote embedding returned an unusable payload",
                provider=self.provider,
                expected=len(texts),
                received=len(vectors),
            )
            return None
        return [np.asarray(vector, dtype=np.float32) for vector in vectors]


class EmbeddingService:
    def __init__(self, model_name: str | None = None, *, use_fallback: bool = False):
        """
        Args:
            model_name: sentence-transformers model id, used only on the local path.
            use_fallback: skip every real encoder and always use the hashing one.
                Needed for air-gapped deployments and for tests, which must not
                download a model or contact a model server.
        """
        self.model_name = model_name or settings.EMBEDDING_MODEL_NAME
        self._model = None
        self._remote: _RemoteEncoder | None = None
        self._remote_checked = False
        self._remote_failed_at = 0.0
        self._remote_failures = 0
        self._fallback: _HashingEncoder | None = None
        self._lock = threading.Lock()
        self._load_failed = use_fallback
        self._forced_fallback = use_fallback
        self._warming = False
        self._warmed = threading.Event()
        if use_fallback:
            self._warmed.set()

    # ------------------------------------------------------------------ #
    @property
    def is_semantic(self) -> bool:
        """True when a real model produces the vectors, remote or local."""
        return self._remote is not None or self._model is not None

    @property
    def backend(self) -> str:
        """What is actually producing vectors, for the diagnostics readout."""
        if self._remote is not None:
            return f"provider:{self._remote.label}"
        if self._model is not None:
            return f"local:{self.model_name}"
        return "lexical"

    @property
    def ready(self) -> bool:
        """Whether resolution has finished, so vectors are the ones we will keep using."""
        return self._warmed.is_set()

    def resolved_remote(self) -> tuple[str, str] | None:
        """The (provider, model) actually serving embeddings, or None off the remote path.

        Used by `LLMProvider.resident_plan` to fold the embedding model into
        the shared residency math without reaching into a private field.
        """
        remote = self._remote
        return (remote.provider, remote.model) if remote is not None else None

    # ------------------------------------------------------------------ #
    def warm(self, *, block: bool = False, timeout: float | None = None) -> None:
        """Resolves the encoder ahead of the first question.

        Cold-starting an encoder is expensive in a way that steady-state use is
        not -- discovery, then a model load off disk that can take minutes on a
        laptop. Doing it lazily meant the first question of every boot paid all
        of it, and paid it *badly*: the load overran the request timeout, the
        provider was written off as broken, and the install spent the rest of
        its life on lexical retrieval.

        Runs on a daemon thread, so startup is not delayed either. Until it
        finishes, ``encode`` answers from the hashing encoder rather than
        blocking -- see ``encode_many``.
        """
        if self._forced_fallback or self._warmed.is_set():
            return
        with self._lock:
            if self._warming:
                return
            self._warming = True

        def resolve() -> None:
            started = time.monotonic()
            try:
                self._resolve(cold=True)
            except Exception as exc:  # noqa: BLE001 -- warming must never take the app down
                logger.warning("Embedding warm-up failed", error=str(exc))
            finally:
                self._warming = False
                self._warmed.set()
                logger.info(
                    "Embeddings ready",
                    backend=self.backend,
                    seconds=round(time.monotonic() - started, 1),
                )

        thread = threading.Thread(target=resolve, name="embedding-warmup", daemon=True)
        thread.start()
        if block:
            self._warmed.wait(timeout)

    def _resolve(self, *, cold: bool) -> None:
        """Picks an encoder: provider, then local model, then nothing (hashing)."""
        if self._get_remote(cold=cold) is not None:
            return
        self._get_model()

    # ------------------------------------------------------------------ #
    def _get_remote(self, *, cold: bool = False) -> _RemoteEncoder | None:
        """Resolves an embedding model on the configured provider, once.

        A provider that has no embedding model installed is the common case, so
        the negative result is remembered and only retried occasionally --
        otherwise every encode on a machine with no such model pays a discovery
        round-trip, several times per question.
        """
        if self._forced_fallback or not settings.EMBEDDINGS_REMOTE_ENABLED:
            return None
        if self._remote is not None:
            return self._remote
        if self._remote_checked and (time.monotonic() - self._remote_failed_at) < self._retry_after:
            return None

        with self._lock:
            if self._remote is not None:
                return self._remote
            self._remote_checked = True

            provider = settings.resolve_provider(settings.EMBEDDING_PROVIDER or None)
            if not allows_provider(settings.data_mode, provider):
                # Embeddings are a role like any other, and text sent to be
                # embedded is data. Degrade to the hashing encoder rather than
                # raise: retrieval getting worse is survivable, a failed question
                # is not.
                logger.info(
                    "Embedding provider is not allowed by the data mode; using the local encoder",
                    provider=provider,
                    data_mode=settings.data_mode,
                )
                self._note_remote_failure()
                return None

            model = settings.EMBEDDING_REMOTE_MODEL.strip() or self._discover_model(provider)
            if not model:
                self._note_remote_failure()
                return None

            candidate = _RemoteEncoder(provider, model)
            # Prove it works before adopting it: a name that classifies as an
            # embedding model is not the same as one the server will embed with.
            # The probe is also what loads the model, so on the warm-up path it
            # is allowed to take as long as a cold load actually takes.
            timeout = settings.EMBEDDING_COLD_TIMEOUT if cold else None
            if candidate.encode_many(["wizard embedding probe"], timeout=timeout) is None:
                self._note_remote_failure()
                return None
            self._remote = candidate
            self._remote_failures = 0
            logger.info("Embeddings served by provider", provider=provider, model=model)
            return self._remote

    @property
    def _retry_after(self) -> float:
        """How long to wait before trying the provider again, doubling per failure."""
        window = REMOTE_RETRY_SECONDS * (2 ** max(0, self._remote_failures - 1))
        return min(window, REMOTE_RETRY_MAX_SECONDS)

    def _note_remote_failure(self) -> None:
        # Stamped *after* the attempt, not before: the probe itself can take a
        # cold-load's worth of time, and counting that as part of the retry
        # window shortens it by exactly the amount the failure cost.
        self._remote_failures += 1
        self._remote_failed_at = time.monotonic()

    @staticmethod
    def _discover_model(provider: str) -> str:
        """An installed embedding model on ``provider``, or ``""``.

        Uses the registry's own classification -- LM Studio reports the type
        outright, and for Ollama it is inferred from the tag -- rather than a
        second, differently-wrong list of model names kept here.
        """
        try:
            from src.core.llm import model_registry

            models = model_registry.list_models(provider=provider)
        except Exception as exc:
            logger.warning("Embedding model discovery failed", provider=provider, error=str(exc))
            return ""
        for model in models:
            if "embedding" in (model.capabilities or []):
                return model.name
        return ""

    def _get_model(self):
        """The optional local sentence-transformers model, if it is installed."""
        if self._model is not None or self._load_failed:
            return self._model
        with self._lock:
            if self._model is not None or self._load_failed:
                return self._model
            try:
                logger.info("Loading SentenceTransformer model", model=self.model_name)
                self._model = self._load_sentence_transformer()
                logger.info("SentenceTransformer ready", model=self.model_name)
            except Exception as exc:
                # Not installed (the normal case now), no network for the first
                # download, or too little memory.
                self._load_failed = True
                logger.info(
                    "No local embedding model; using the provider or the hashing encoder",
                    model=self.model_name,
                    detail=str(exc),
                )
        return self._model

    def _load_sentence_transformer(self):
        """Loads the local model, without fetching it unless that was asked for.

        The default constructor downloads a missing model. That turned a
        provider timeout into a 90 MB download inside somebody's first question
        -- and the half-warm model it produced then failed to encode, so the
        wait bought nothing. Offline is the honest default here; the provider
        path is better anyway, and a deliberate offline install can set the flag.
        """
        from sentence_transformers import SentenceTransformer

        if settings.EMBEDDING_ALLOW_DOWNLOAD:
            return SentenceTransformer(self.model_name)

        # huggingface_hub reads this at call time, and every version honours it,
        # which a constructor keyword does not. Restored immediately after.
        import os

        previous = os.environ.get("HF_HUB_OFFLINE")
        os.environ["HF_HUB_OFFLINE"] = "1"
        try:
            return SentenceTransformer(self.model_name)
        finally:
            if previous is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = previous

    def _get_fallback(self) -> _HashingEncoder:
        if self._fallback is None:
            self._fallback = _HashingEncoder()
        return self._fallback

    # ------------------------------------------------------------------ #
    def encode(self, text: str) -> np.ndarray:
        """Encodes a single string. Never raises."""
        return self.encode_many([text])[0]

    def encode_many(self, texts: list[str]) -> list[np.ndarray]:
        """Encodes a batch. Never raises, and always returns one vector per input."""
        if not texts:
            return []

        # While warm-up is still resolving an encoder, answer from the hashing
        # one rather than queueing behind a model load. A question must never
        # wait on retrieval infrastructure: worst case it retrieves less well,
        # and `rank` re-encodes any vector stored at the wrong width later.
        if self._warming and self._remote is None and self._model is None:
            fallback = self._get_fallback()
            return [fallback.encode(text) for text in texts]

        remote = self._get_remote()
        if remote is not None:
            vectors = remote.encode_many(texts)
            if vectors is not None:
                return vectors
            # A working encoder that just failed is a transient problem, not a
            # reason to keep paying for it on every subsequent call.
            self._remote = None
            self._note_remote_failure()

        model = self._get_model()
        if model is not None:
            try:
                return [np.asarray(vector, dtype=np.float32) for vector in model.encode(texts)]
            except Exception as exc:
                # Stop calling an encoder that is loaded but cannot encode --
                # otherwise every question pays for it and still falls through.
                self._model = None
                self._load_failed = True
                logger.warning("Local embedding failed, using fallback", error=str(exc))

        fallback = self._get_fallback()
        return [fallback.encode(text) for text in texts]

    def similarity(self, a: np.ndarray | None, b: np.ndarray | None) -> float:
        return cosine_similarity(a, b)

    def rank(self, query: str, candidates: list[tuple[str, np.ndarray | None]]) -> list[tuple[float, int]]:
        """Scores ``candidates`` against ``query``; returns (score, index) sorted desc.

        A stored vector is re-encoded when it is missing *or* when its width does
        not match the query's. Switching encoder -- MiniLM's 384 dimensions to a
        768-dimension provider model, or back to the hashing fallback -- would
        otherwise score every previously-stored row at exactly 0.0, silently
        emptying the semantic cache and the trajectory memory rather than
        rebuilding them.
        """
        if not candidates:
            return []
        query_vec = self.encode(query)
        width = query_vec.size

        stale = [
            index
            for index, (_, vector) in enumerate(candidates)
            if vector is None or len(vector) == 0 or np.asarray(vector).size != width
        ]
        refreshed: dict[int, np.ndarray] = {}
        if stale:
            encoded = self.encode_many([candidates[index][0] for index in stale])
            refreshed = dict(zip(stale, encoded, strict=False))

        scored = [
            (self.similarity(query_vec, refreshed.get(index, vector)), index)
            for index, (_, vector) in enumerate(candidates)
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored


embedding_service = EmbeddingService(use_fallback=settings.EMBEDDINGS_FORCE_FALLBACK)
