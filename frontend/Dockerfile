ARG BASE_IMAGE_REGISTRY=docker.io/library
FROM ${BASE_IMAGE_REGISTRY}/node:24-alpine AS build
ARG NPM_REGISTRY=https://registry.npmjs.org
WORKDIR /web
COPY package.json package-lock.json .npmrc ./
RUN npm ci --registry=${NPM_REGISTRY}
COPY . ./
RUN npm run build

FROM ${BASE_IMAGE_REGISTRY}/nginx:1.28-alpine AS runtime
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
RUN chown -R nginx:nginx /var/cache/nginx /var/run /usr/share/nginx/html \
    && touch /var/run/nginx.pid \
    && chown nginx:nginx /var/run/nginx.pid
USER nginx
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
