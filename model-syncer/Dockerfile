# Syncs the upstream provider's model catalog into CLIProxyAPI (Management API)
# and seeds new models into Open WebUI as hidden workspace models.
# Metadata (display names, reasoning levels, modalities) comes from models.dev.

FROM python:3.12-alpine

WORKDIR /app
COPY sync.py .

CMD ["python", "-u", "sync.py"]
