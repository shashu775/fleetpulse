# Gateway: app launcher + reverse proxy to the 4 apps and 2 services.
# Build from the repo root:  docker build -f apps/gateway/Dockerfile -t fp-gateway .
FROM nginx:1.27-alpine

RUN rm /etc/nginx/conf.d/default.conf
COPY apps/gateway/nginx.conf      /etc/nginx/conf.d/gateway.conf
COPY apps/gateway/app_proxy.conf     /etc/nginx/app_proxy.conf
COPY apps/gateway/api_locations.conf /etc/nginx/api_locations.conf
COPY packages/web-shared/base.css /usr/share/nginx/html/base.css
COPY apps/gateway/index.html      /usr/share/nginx/html/index.html

EXPOSE 80

# NOTE: `localhost` resolves to ::1 in this image, but nginx's `listen 80`
# binds IPv4 only -- so this MUST use 127.0.0.1 or the check always fails
# while the container is actually serving fine.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1/healthz >/dev/null 2>&1 || exit 1
