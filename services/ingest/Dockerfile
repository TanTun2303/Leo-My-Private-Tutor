ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore PIP_DISABLE_PIP_VERSION_CHECK=1
# Marker 2.0 runs its heavy VLM in a separate inference server (surya-server, llama.cpp CUDA).
# Only small helper models run here, so CPU-only torch keeps the image small.
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch==2.14.0" "torchvision==0.29.0" \
 && pip install "marker-pdf[full]==2.0.0" "pypdf==6.19.0" "httpx==0.28.1"
WORKDIR /app
COPY ingest.py .
ENTRYPOINT ["python", "ingest.py"]
CMD ["run"]
