# The LAN-side biometric agent.
#
# This image runs on a machine inside the office network -- the only place that
# can reach both the ZKTeco terminals on port 4370 and the hosted site over
# HTTPS. It contains the app package purely so `agent.py` can import `zk_client`
# from it; nothing here needs frappe, bench, a database or a site.
#
# Build from the repository root:
#   docker build -f docker/biometric-agent/Dockerfile -t tk-biometric-agent .

FROM python:3.14-slim

# `frappe` is commented out of [project.dependencies] in pyproject.toml (bench
# manages it), so installing the package here pulls in pyzk and requests and
# nothing else. That is also what keeps the pyzk pin in one place instead of
# repeated in this file.
WORKDIR /src
COPY pyproject.toml README.md license.txt ./
COPY teebatkarbala ./teebatkarbala
RUN pip install --no-cache-dir ".[agent]" && rm -rf /src

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

ENTRYPOINT ["python", "-m", "teebatkarbala.biometric_attendance.agent"]
CMD ["--config", "/etc/tk-biometric/config.toml"]
