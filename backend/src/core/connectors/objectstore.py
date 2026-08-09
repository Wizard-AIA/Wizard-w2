"""S3 and anything that speaks its API.

The object-storage third of the reference set: MinIO, Cloudflare R2, Backblaze B2
and S3 itself are the same connector with a different endpoint, which is the
point. It proves the interface holds for a source whose "tables" are files rather
than a catalog.

An object store is the one reference kind where the target names a *file format*
as much as a location, so the read delegates to pandas by extension rather than
inventing a second loader beside ``ingest/loader.py``.
"""

from __future__ import annotations

import io
from typing import Any
from urllib.parse import urlsplit

import pandas as pd

from src.config import settings

from .base import DEFAULT_SAMPLE_ROWS, refuse_write
from .registry import ConnectorKind, register
from .spec import LOOPBACK_HOSTS, ConnectionSchema, ConnectionSpec, ConnectorError, DriverMissing, TargetInfo


#: Objects the connector will try to read. Anything else in the bucket is listed
#: by `discover` but not offered as readable -- a bucket usually holds far more
#: than its tabular files, and silently trying to parse a JPEG as CSV is worse
#: than saying which objects are candidates.
READABLE_SUFFIXES = (".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".parquet", ".feather")

#: How many objects `discover` lists. A bucket can hold millions, and enumerating
#: all of them to populate a picker is a bill as well as a wait.
LIST_LIMIT = 500


def _boto3() -> Any:
    try:
        import boto3
    except ImportError as exc:
        raise DriverMissing("objectstore", "boto3") from exc
    return boto3


class ObjectStoreConnector:
    """An S3-compatible bucket wearing the ``Connector`` interface."""

    def __init__(self, spec: ConnectionSpec, secret: str = ""):
        self.spec = spec
        self._secret = secret
        self._client: Any = None

    # ------------------------------------------------------------------ #
    def _bucket(self) -> str:
        bucket = str(self.spec.options.get("bucket") or "").strip()
        if not bucket:
            raise ConnectorError(
                "This connection names no bucket.",
                detail="Set the bucket this connection should read from.",
            )
        return bucket

    def _require_safe_endpoint(self, endpoint: str) -> None:
        """Refuses a cleartext endpoint that is not on this machine.

        boto3 honours the scheme it is given, so an ``http://`` endpoint sends
        the SigV4 authorization header -- derived from the secret key -- over the
        wire in the clear. On loopback that is nobody else's business; to a
        remote host it hands the credential to anything on the path.

        MinIO on ``http://127.0.0.1:9000`` is the common development setup and
        keeps working, which is why this checks the *host* rather than banning
        plaintext outright.
        """
        lowered = endpoint.lower()
        if not lowered.startswith("http://"):
            return
        # A manual split on "/" then ":" reads userinfo as the host --
        # "http://localhost:9000@attacker.example" would split to "localhost",
        # while the authority botocore actually connects to is
        # "attacker.example". `urlsplit` parses the authority correctly and
        # `.hostname` never includes userinfo, so this compares the real host.
        parsed = urlsplit(lowered)
        if parsed.username or parsed.password:
            raise ConnectorError(
                f"Refusing an endpoint with embedded credentials in the URL: {endpoint}",
                detail="Put the access key and secret in this connection's own fields, not in the endpoint URL.",
            )
        if (parsed.hostname or "") not in LOOPBACK_HOSTS:
            raise ConnectorError(
                f"Refusing a cleartext endpoint for a remote host: {endpoint}",
                detail=(
                    "An http:// endpoint sends the request signature, which is derived from your "
                    "secret key, unencrypted. Use https://, or point this connection at localhost."
                ),
            )

    def _connect(self) -> Any:
        if self._client is None:
            boto3 = _boto3()
            options = self.spec.options
            endpoint = str(options.get("endpoint_url") or "").strip()
            self._require_safe_endpoint(endpoint)
            try:
                from botocore.config import Config

                # botocore's own defaults are 60s with retries on top, so an
                # unreachable endpoint would sit for minutes. One retry, because
                # this is a user waiting on a page rather than a batch job.
                timeout = int(settings.CONNECTOR_TIMEOUT)
                self._client = boto3.client(
                    "s3",
                    config=Config(
                        connect_timeout=timeout,
                        read_timeout=timeout,
                        retries={"max_attempts": 1},
                    ),
                    # Empty means "the AWS default chain" -- environment, profile,
                    # instance role. A user on EC2 or with `aws configure` already
                    # done should not have to paste a key Wizard would then store.
                    endpoint_url=endpoint or None,
                    region_name=str(options.get("region") or "").strip() or None,
                    aws_access_key_id=str(options.get("access_key_id") or "").strip() or None,
                    aws_secret_access_key=self._secret or None,
                )
            except Exception as exc:
                raise ConnectorError("Could not open the connection.", detail=str(exc)) from exc
        return self._client

    # ------------------------------------------------------------------ #
    def test(self) -> None:
        client = self._connect()
        try:
            client.head_bucket(Bucket=self._bucket())
        except Exception as exc:
            raise ConnectorError("Could not reach the bucket.", detail=str(exc)) from exc

    def discover(self) -> ConnectionSchema:
        client = self._connect()
        bucket = self._bucket()
        prefix = str(self.spec.options.get("prefix") or "").strip()
        try:
            response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=LIST_LIMIT)
        except Exception as exc:
            raise ConnectorError("Could not list the bucket.", detail=str(exc)) from exc

        targets: list[TargetInfo] = []
        for entry in response.get("Contents") or []:
            key = str(entry.get("Key") or "")
            if not key or not key.lower().endswith(READABLE_SUFFIXES):
                continue
            targets.append(TargetInfo(name=key, namespace=bucket))
        return ConnectionSchema(targets=targets)

    def sample(self, target: str, limit: int = DEFAULT_SAMPLE_ROWS) -> pd.DataFrame:
        # An object store has no server-side row limit -- the smallest unit it
        # will return is the object -- so unlike the relational connector
        # there is nothing to push down in general. A delimited text format
        # is the one exception: it can be parsed from a byte prefix without
        # reading the rest, so a preview of a multi-GB CSV costs a bounded
        # Range request instead of the whole download. Parquet/Feather/JSON
        # are not splittable at an arbitrary offset this way and still read
        # whole.
        lowered = target.lower()
        if lowered.endswith((".csv", ".tsv")):
            return self._sample_delimited(target, lowered.endswith(".tsv"), int(limit))
        frame = self._read_object(target)
        return frame.head(int(limit))

    def _sample_delimited(self, key: str, tab_separated: bool, limit: int) -> pd.DataFrame:
        """Parses only the first `CONNECTOR_SAMPLE_RANGE_BYTES` of a CSV/TSV object."""
        client = self._connect()
        range_bytes = max(1, int(settings.CONNECTOR_SAMPLE_RANGE_BYTES))
        try:
            response = client.get_object(Bucket=self._bucket(), Key=key, Range=f"bytes=0-{range_bytes - 1}")
            payload = response["Body"].read()
        except Exception as exc:
            raise ConnectorError(f"Could not read '{key}'.", detail=str(exc)) from exc

        # A range short of the whole object almost certainly split the final
        # row mid-line -- `ContentRange` is `bytes 0-N/total`, so comparing N+1
        # against the reported total says whether this range *was* the whole
        # object. When it wasn't, the partial trailing line is dropped rather
        # than handed to the CSV parser as a short row.
        content_range = str(response.get("ContentRange") or "")
        total = content_range.rsplit("/", 1)[-1] if "/" in content_range else ""
        truncated = not total.isdigit() or len(payload) < int(total)

        text = payload.decode("utf-8", errors="replace")
        if truncated and "\n" in text:
            text = text.rsplit("\n", 1)[0]
        try:
            frame = pd.read_csv(io.StringIO(text), sep="\t" if tab_separated else ",")
        except Exception as exc:
            raise ConnectorError(f"Could not parse '{key}'.", detail=str(exc)) from exc
        return frame.head(limit)

    def fetch(self, query: str) -> pd.DataFrame:
        """Reads one object whole. ``query`` is its key."""
        return self._read_object(query)

    def write(self, target: str, df: pd.DataFrame) -> None:
        refuse_write(self.spec)
        client = self._connect()
        # Dispatched by suffix, the same way `_read_object` dispatches the read.
        # Writing CSV bytes under every non-Parquet key looked harmless and was
        # not: `READABLE_SUFFIXES` advertises .json and .feather as readable, so
        # the reader would then call `read_json` on CSV and a write-then-read
        # round trip through this connector failed.
        lowered = target.lower()
        buffer = io.BytesIO()
        if lowered.endswith(".parquet"):
            df.to_parquet(buffer, index=False)
        elif lowered.endswith(".feather"):
            df.to_feather(buffer)
        elif lowered.endswith((".jsonl", ".ndjson")):
            buffer.write(df.to_json(orient="records", lines=True).encode("utf-8"))
        elif lowered.endswith(".json"):
            buffer.write(df.to_json(orient="records").encode("utf-8"))
        elif lowered.endswith(".tsv"):
            buffer.write(df.to_csv(index=False, sep="\t").encode("utf-8"))
        elif lowered.endswith(".csv"):
            buffer.write(df.to_csv(index=False).encode("utf-8"))
        else:
            # Refused rather than guessed. A key this connector cannot read back
            # is a write it should not have made.
            raise ConnectorError(
                f"Cannot write '{target}': unsupported object type.",
                detail=f"Use one of: {', '.join(READABLE_SUFFIXES)}.",
            )
        try:
            client.put_object(Bucket=self._bucket(), Key=target, Body=buffer.getvalue())
        except Exception as exc:
            raise ConnectorError(f"Could not write to '{target}'.", detail=str(exc)) from exc

    def close(self) -> None:
        self._client = None

    # ------------------------------------------------------------------ #
    def _read_object(self, key: str) -> pd.DataFrame:
        client = self._connect()
        # Size-checked before the read, not after. `CONNECTOR_MAX_ROWS` cannot
        # protect this path: the smallest unit an object store returns is the
        # whole object, so by the time rows could be counted the bytes are
        # already resident -- in the API process, which is the one that is not
        # sandboxed and has no memory ceiling.
        ceiling = int(settings.CONNECTOR_MAX_OBJECT_BYTES)
        try:
            size = int(client.head_object(Bucket=self._bucket(), Key=key).get("ContentLength") or 0)
        except Exception as exc:
            raise ConnectorError(f"Could not read '{key}'.", detail=str(exc)) from exc
        if size > ceiling:
            raise ConnectorError(
                f"'{key}' is {size / 1024 / 1024:,.0f} MB, over the {ceiling / 1024 / 1024:,.0f} MB limit.",
                detail="Raise CONNECTOR_MAX_OBJECT_BYTES, or point this connection at a smaller object.",
            )

        try:
            response = client.get_object(Bucket=self._bucket(), Key=key)
            payload = response["Body"].read()
        except Exception as exc:
            raise ConnectorError(f"Could not read '{key}'.", detail=str(exc)) from exc

        buffer = io.BytesIO(payload)
        lowered = key.lower()
        try:
            if lowered.endswith(".parquet"):
                return pd.read_parquet(buffer)
            if lowered.endswith(".feather"):
                return pd.read_feather(buffer)
            if lowered.endswith((".jsonl", ".ndjson")):
                return pd.read_json(buffer, lines=True)
            if lowered.endswith(".json"):
                return pd.read_json(buffer)
            return pd.read_csv(buffer, sep="\t" if lowered.endswith(".tsv") else ",")
        except Exception as exc:
            raise ConnectorError(f"Could not parse '{key}'.", detail=str(exc)) from exc


register(
    ConnectorKind(
        kind="objectstore",
        label="S3-compatible storage",
        factory=ObjectStoreConnector,
        module="boto3",
        distribution="boto3",
        fields=("bucket", "prefix", "region", "endpoint_url", "access_key_id"),
        requires_secret=False,
        description=(
            "Amazon S3, MinIO, Cloudflare R2 and other S3-compatible stores. "
            "CSV, TSV, JSON, Parquet and Feather objects are readable as tables."
        ),
    )
)


__all__ = ["ObjectStoreConnector"]
