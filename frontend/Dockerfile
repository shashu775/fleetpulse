# Static UI + reverse proxy.
#
# No build step: the frontend is plain HTML/CSS/JS, so there is nothing to
# compile, no node_modules, and no npm supply chain to worry about. nginx just
# serves three files and proxies the two APIs.

FROM nginx:1.27-alpine

# Replace the stock default site with ours.
RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/fleetpulse.conf

COPY index.html styles.css app.js /usr/share/nginx/html/

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost/healthz || exit 1
