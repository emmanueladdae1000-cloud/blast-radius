# Same base image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY selector.py /app/selector.py
COPY map /app/map
# Python sources of sqlglot at the pinned commit (eb11aec), used to apply each
# patch and syntax-check the result.
COPY src.tar.gz /tmp/src.tar.gz
RUN python -c "import tarfile; tarfile.open('/tmp/src.tar.gz').extractall('/app/src', filter='data')" \
    && rm /tmp/src.tar.gz && chmod -R a+rX /app
ENV BR_SRC=/app/src
CMD ["python", "-S", "/app/selector.py"]
