ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .
COPY migrations ./migrations
ENV LEO_MIGRATIONS_DIR=/app/migrations

# Test image: adds pytest + tests. Used by `make test-memory` against a throwaway database.
FROM base AS test
RUN pip install ".[test]"
COPY tests ./tests
USER nobody
CMD ["pytest", "-q", "-p", "no:cacheprovider"]

FROM base AS runtime
USER nobody
EXPOSE 8000
CMD ["uvicorn", "leo_memory.api:app", "--host", "0.0.0.0", "--port", "8000"]
