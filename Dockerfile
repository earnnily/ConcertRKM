FROM python:3.12-slim
RUN useradd --uid 10001 --create-home app && mkdir /data && chown app:app /data
WORKDIR /app
COPY --chown=app:app app/ /app/
USER app
ENV DATA_DIR=/data BIND_HOST=0.0.0.0 PORT=8080
EXPOSE 8080
CMD ["python", "server.py"]
