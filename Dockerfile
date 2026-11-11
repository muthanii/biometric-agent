# The LAN-side biometric agent.
#
# This image runs on a machine inside the office network -- the only place that
# can reach both the ZKTeco terminals on port 4370 and the hosted site over
# HTTPS. It is built entirely from this standalone project; it needs no Frappe
# app checkout, bench, or database.
#
# Build from this directory:
#   docker build -t tk-biometric-agent .

FROM python:3.14-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY biometric_agent ./biometric_agent

# The state file is the sync cursor. It lives on a volume because losing it
# means re-reading every terminal from the import start date -- survivable, but
# a wasted first run every time the container is recreated.
RUN useradd --create-home --uid 10001 agent \
	&& mkdir -p /var/lib/tk-biometric-agent \
	&& chown agent:agent /var/lib/tk-biometric-agent
USER agent

VOLUME ["/var/lib/tk-biometric-agent"]

# Unbuffered, because the container log is the only log this thing keeps.
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "biometric_agent.agent"]
CMD ["--config", "/etc/tk-biometric/config.toml"]
