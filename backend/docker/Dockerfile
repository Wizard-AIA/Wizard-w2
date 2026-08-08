# Execution sandbox image.
#
# Generated analysis code runs here, never in the API process. The library set is
# pre-installed rather than pip-installed on demand: the default PLOT_FORMAT is
# "html", which needs plotly, so a bare image forced a network install before the
# very first chart could render -- and that install is impossible when
# SANDBOX_NETWORK_DISABLED is on.
#
# The set is layered, because "everything a data analyst might want" is a lot of
# gigabytes to ask of a laptop that also has to hold a language model:
#
#   SANDBOX_TIER=core      pandas, numpy, pyarrow, matplotlib, duckdb, openpyxl
#   SANDBOX_TIER=standard  + stats, ML, plotly/seaborn, networkx  (default)
#   SANDBOX_TIER=full      + survival analysis and geospatial
#
# Nothing needs to be kept in step with the prompt by hand any more: the daemon
# reports which modules it can import and the model is told exactly that, so a
# `core` image simply advertises less. That coupling used to be manual and was
# twice wrong in both directions -- scikit-learn and statsmodels sat installed
# and unmentioned for months, and duckdb was advertised to a process without it.
#
#   docker build --build-arg SANDBOX_TIER=core -t wizard-sandbox:core backend/docker
#
# The tag carries the tier (see `settings.sandbox_image`), so switching tiers
# builds a new image rather than silently reusing one with different libraries.
FROM ghcr.io/astral-sh/uv:0.12.0@sha256:606e70c71c852d03f611b1e56a195d08648507018a7057fab82c4974c4eae105 AS uv

FROM python:3.11-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff

ARG SANDBOX_TIER=standard

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /workspace

COPY --from=uv /uv /usr/local/bin/uv

# `--compile-bytecode` is explicit rather than left to the installer's default,
# because PYTHONDONTWRITEBYTECODE is set above and the two interact. Bytecode
# costs some image size and buys it back on every container start: without it,
# each new session re-parses the whole of pandas and matplotlib from source
# before it can answer anything, which on a laptop is seconds of dead time per
# session. uv, not pip: same version pins, an order of magnitude faster.
ARG PIP="uv pip install --system --no-cache --compile-bytecode"

# Tier: core. Enough to load, reshape, query and chart a dataset.
RUN $PIP \
    "pandas==2.2.2" \
    "numpy==1.26.4" \
    "pyarrow==17.0.0" \
    "matplotlib==3.9.1.post1" \
    "duckdb==1.1.0" \
    "openpyxl==3.1.5"

# Tier: standard. Statistics, machine learning and the interactive chart stack.
#
# xgboost-cpu, not xgboost: the default wheel bundles a CUDA-enabled
# libxgboost.so and is 154 MB compressed against 4.5 MB here. The import name is
# identical, so nothing in the prompt or in generated code changes.
RUN if [ "$SANDBOX_TIER" != "core" ]; then $PIP \
    "scipy==1.14.0" \
    "statsmodels==0.14.2" \
    "scikit-learn==1.5.1" \
    "xgboost-cpu==2.1.1" \
    "lightgbm==4.5.0" \
    "plotly==5.23.0" \
    "seaborn==0.13.2" \
    "networkx==3.3" \
    "xlsxwriter==3.2.0" \
    "tabulate==0.9.0" \
    "pillow==10.4.0" \
    ; fi

# Tier: full. Survival analysis, and geospatial -- which carries GDAL and PROJ
# through pyogrio/pyproj, around 45 MB of compressed wheels.
RUN if [ "$SANDBOX_TIER" = "full" ]; then $PIP \
    "lifelines==0.29.0" \
    "geopandas==1.0.1" \
    "shapely==2.0.6" \
    ; fi

# Warm the matplotlib font cache so the first plot is not penalised by it.
RUN python -c "import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt; plt.plot([0,1],[0,1]); plt.close('all')"

# Deliberately left as root *inside the container*. Switching to a fixed UID
# breaks writes to the bind-mounted workspace on Linux hosts, where the mount is
# owned by the invoking user. Containment comes from the runtime flags applied by
# SandboxPool instead: cap_drop=ALL, no-new-privileges, and memory/PID limits.
# Set SANDBOX_DOCKER_RUNTIME=runsc to add gVisor kernel isolation on top.
CMD ["sleep", "infinity"]
