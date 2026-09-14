FROM python:3.12.13-slim-bookworm
WORKDIR /app
COPY pyproject.toml /app/
COPY src /app/src
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home relay \
    && mkdir /data && chown relay:relay /data
USER relay
EXPOSE 8765
CMD ["python", "-m", "remoteinput", "relay", "--host", "0.0.0.0", "--behind-proxy", "--database", "/data/remoteinput.sqlite3"]

