# Static app image. Build from the REPO ROOT so packages/web-shared is in scope:
#     docker build -f apps/customer-portal/Dockerfile -t fp-customer-portal .
#
# No build step, no node_modules -- nginx serves the files as authored.
FROM nginx:1.27-alpine

RUN rm /etc/nginx/conf.d/default.conf
COPY apps/customer-portal/nginx.conf /etc/nginx/conf.d/app.conf

# Shared design system + API client, copied in at build time so no app
# depends on another being up at runtime.
COPY packages/web-shared/ /usr/share/nginx/html/

# App-specific files last so they win on any name collision.
COPY apps/customer-portal/ /usr/share/nginx/html/
RUN rm -f /usr/share/nginx/html/Dockerfile /usr/share/nginx/html/nginx.conf

EXPOSE 80

# NOTE: `localhost` resolves to ::1 in this image, but nginx's `listen 80`
# binds IPv4 only -- so this MUST use 127.0.0.1 or the check always fails
# while the container is actually serving fine.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1/healthz >/dev/null 2>&1 || exit 1
