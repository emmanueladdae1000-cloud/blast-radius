# Same base image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
# Runtime libraries sqlglot's tests use, at the versions in requirements.lock
# (the labeling environment). Only needed to run candidate tests while deciding.
RUN pip install --no-cache-dir duckdb==2.0.0 numpy==2.5.5 pandas==3.1.1 \
        python-dateutil==4.0.0 pytz==2026.6 tzdata==2026.6
# sqlglot and its tests at the pinned commit eb11aec4be8cb2122afe713a4816662c3f0fb2fa
COPY src.tar.gz /tmp/src.tar.gz
RUN python -c "import tarfile; tarfile.open('/tmp/src.tar.gz').extractall('/app/src', filter='data')" \
    && rm /tmp/src.tar.gz \
    && python -m compileall -q /app/src \
    && chmod -R a+rX /app
COPY selector.py verify.py /app/
COPY map /app/map
RUN python -m compileall -q /app/verify.py && chmod -R a+rX /app
ENV BR_SRC=/app/src
CMD ["python", "/app/selector.py"]
