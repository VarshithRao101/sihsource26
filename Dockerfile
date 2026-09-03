# The REAL backend, containerised - the one that solves.
#
# This exists because Vercel cannot run it. A serverless function freezes when
# it returns, has no writable disk, does not support WebSockets, and caps the
# bundle at 250 MB. This project needs numba compiling kernels, rasterio
# reading terrain, torch holding the surrogate, a run folder it can write, a
# process that stays alive to hold the run registry, and a WebSocket. That is a
# container, not a function.
#
# Pair it with the pages on Vercel: set SIH_CORS_ORIGINS to the Vercel origin
# here, and point config.js at this service's URL. See docs/DEPLOY.md.

FROM python:3.13-slim

# gcc and friends: most of the scientific stack ships manylinux wheels, but
# anything that does not needs a compiler, and a build that fails at pip time
# is far easier to read than one that fails at import time.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so a code change does not reinstall 2 GB of wheels.
COPY requirements.txt ./

# TORCH IS THE ONE ENTRY THAT CANNOT BE INSTALLED AS PINNED. requirements.txt
# says torch==2.6.0+cu124 - a CUDA build, from the machine that trained the
# surrogate on a GPU. That wheel is not on PyPI and there is no GPU here, so it
# is installed from PyTorch's CPU index and then filtered out of the file so pip
# does not try again and fail. The surrogate runs on CPU: slower than the 20 ms
# it manages on CUDA, still far faster than the solver.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir torch==2.6.0 \
         --index-url https://download.pytorch.org/whl/cpu \
    && grep -v '^torch==' requirements.txt > /tmp/requirements-nocuda.txt \
    && pip install --no-cache-dir -r /tmp/requirements-nocuda.txt

COPY . .

# Run folders are written here. On Render this is ephemeral unless a disk is
# attached at this path - see docs/DEMO_RUNBOOK.md. Without one, finished runs
# vanish on the next deploy or restart, which is survivable for a demo and not
# for anything else.
# 777 rather than root-owned: several container hosts run the process as a
# non-root user, and a run folder the process cannot write is a solve that dies
# at the last step after doing all the work.
RUN mkdir -p /app/outputs && chmod -R 777 /app/outputs

# Every one of these redirects a library that would otherwise write into $HOME.
# Where $HOME is not writable - which is common for non-root container users -
# each fails at IMPORT time with an error naming a cache directory rather than
# the real problem.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NUMBA_CACHE_DIR=/tmp/numba_cache \
    MPLCONFIGDIR=/tmp/mpl \
    XDG_CACHE_HOME=/tmp/cache \
    HOME=/tmp

EXPOSE 8000

# Render, Railway and Fly inject $PORT and expect the process to bind it on
# 0.0.0.0; 8000 is the fallback for anything that does not. Binding 127.0.0.1
# or a fixed port is the usual reason a container deploys green and then fails
# its health check.
#
# ONE WORKER, deliberately. RunRegistry holds live runs in process memory, so a
# second worker would answer /api/runs/{id}/status for a run it has never heard
# of. Concurrency here means concurrent VIEWERS, which one worker serves fine;
# it does not mean concurrent solves.
CMD ["sh", "-c", "python -m uvicorn modules.04_backend.api:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
