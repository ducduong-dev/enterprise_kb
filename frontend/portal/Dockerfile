# The portal is a static bundle: Vite inlines its configuration at *build* time, so the API
# and issuer URLs are build arguments rather than runtime environment. Setting them at runtime
# looks like it works and silently ships the defaults.
FROM node:20-slim AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
ARG VITE_API_BASE=/api
ARG VITE_OIDC_ISSUER=/realms/kb
ENV VITE_API_BASE=$VITE_API_BASE \
    VITE_OIDC_ISSUER=$VITE_OIDC_ISSUER
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
# nginx serves on 80; compose publishes it as 5173 to match the dev server's port.
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
