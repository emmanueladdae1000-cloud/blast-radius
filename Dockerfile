FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN python -c "import urllib.request, tarfile, io, shutil; \
url='https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz'; \
data=urllib.request.urlopen(url).read(); \
tf=tarfile.open(fileobj=io.BytesIO(data), mode='r:gz'); \
tf.extractall('/tmp'); \
shutil.move('/tmp/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa', '/sqlglot')"

COPY coverage_map.json.gz /app/coverage_map.json.gz
COPY selector.py /app/selector.py
RUN chmod -R a+rX /app /sqlglot

CMD ["python", "/app/selector.py"]
