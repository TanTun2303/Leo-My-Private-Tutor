ARG PLAYWRIGHT_TAG=v1.63.0-noble
FROM mcr.microsoft.com/playwright/python:${PLAYWRIGHT_TAG}
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore PIP_DISABLE_PIP_VERSION_CHECK=1
# Baked in: the ui-test container runs on the internal network with no internet.
RUN pip install --break-system-packages "playwright==1.63.0" "pytest==9.1.1" "pytest-playwright==0.9.0" "httpx==0.28.1" "pydantic==2.13.5"
WORKDIR /tests
CMD ["pytest", "-q", "-p", "no:cacheprovider"]
