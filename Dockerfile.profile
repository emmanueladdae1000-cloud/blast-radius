FROM rwx-under120-candidate
COPY requirements.lock /tmp/profile-requirements.lock
RUN pip install --no-cache-dir -r /tmp/profile-requirements.lock
RUN python -c "import urllib.request,tarfile,io; data=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz').read(); tarfile.open(fileobj=io.BytesIO(data),mode='r:gz').extractall('/baseline',filter='data')"