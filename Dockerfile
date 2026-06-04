# Headless Age of Time client — runtime image.
#
# All configuration is read from the CONTAINER ENVIRONMENT (no .env is baked in);
# provide the variables with `docker run -e ...` or `--env-file`. Required:
#   AOT_SERVER_HOST, AOT_SERVER_PORT, AOT_USERNAME, AOT_PASSWORD
# See .env.example for the full list.
FROM python:3.12-slim

# Container-friendly Python: no .pyc, unbuffered stdout so logs stream live.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Only what's needed to build/install the package (keeps the image small and the
# build cache friendly). Source captures/tests/docs/tools are intentionally not
# copied — they aren't needed at runtime.
COPY pyproject.toml README.md LICENSE ./
COPY aotbot ./aotbot

# Install the package, then create an unprivileged user to run as.
RUN pip install . \
    && useradd --create-home --uid 10001 app

USER app

# stdin is not a TTY here, so the interactive REPL stays off automatically and
# the bot runs headless. The app installs SIGINT/SIGTERM handlers for a clean
# disconnect (run with `docker run --init` if you want a PID-1 reaper too).
ENTRYPOINT ["aotbot"]
