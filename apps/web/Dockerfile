# FleetPulse web tier: five static apps + API proxy in ONE container.
#
# Replaces six containers (the gateway plus five per-app nginx containers).
# Those had no independent scaling profile, no state, and no independent
# lifecycle -- and since they all embed packages/web-shared, a change to the
# shared package rebuilt all five anyway.
#
# Build from the REPO ROOT so both apps/ and packages/ are in scope:
#     docker build -f apps/web/Dockerfile -t fp-web .

FROM nginx:1.27-alpine

RUN rm /etc/nginx/conf.d/default.conf
COPY apps/web/nginx.conf         /etc/nginx/conf.d/default.conf
COPY apps/web/api_locations.conf /etc/nginx/api_locations.conf
COPY apps/web/app_proxy.conf     /etc/nginx/app_proxy.conf

# Launcher at the root.
COPY apps/web/index.html          /usr/share/nginx/html/index.html
COPY packages/web-shared/base.css /usr/share/nginx/html/base.css

# Each app into its own subdirectory, with the shared package copied in
# ALONGSIDE it rather than only at the root.
#
# That duplication is deliberate and is what makes this a zero-code-change
# refactor: the apps use relative paths (./api.js, ./base.css), which resolve
# inside whichever directory the page was served from. A single shared copy at
# the root would 404 for every app once they moved into subdirectories.
# Three small text files x 5 = a few KB.
COPY packages/web-shared/  /usr/share/nginx/html/merchant/
COPY apps/merchant-portal/ /usr/share/nginx/html/merchant/
COPY packages/web-shared/  /usr/share/nginx/html/hub/
COPY apps/hub-app/         /usr/share/nginx/html/hub/
COPY packages/web-shared/  /usr/share/nginx/html/driver/
COPY apps/driver-app/      /usr/share/nginx/html/driver/
COPY packages/web-shared/  /usr/share/nginx/html/track/
COPY apps/customer-portal/ /usr/share/nginx/html/track/
COPY packages/web-shared/  /usr/share/nginx/html/admin/
COPY apps/admin-console/   /usr/share/nginx/html/admin/

# Build-context leftovers that must never be served.
RUN find /usr/share/nginx/html \( -name 'Dockerfile' -o -name 'nginx.conf' \) -delete

EXPOSE 80

# 127.0.0.1, NOT localhost: in this image `localhost` resolves to ::1 while
# nginx's `listen 80` binds IPv4 only, so a localhost check always reports
# unhealthy on a container that is serving perfectly.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1/healthz >/dev/null 2>&1 || exit 1
