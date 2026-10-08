FROM node:22-alpine3.24 AS build
WORKDIR /ui
COPY package.json package-lock.json ./
RUN npm ci --no-fund && npm audit --audit-level=high
COPY index.html tsconfig.json vite.config.ts ./
COPY src ./src
RUN npm run build

FROM nginx:1.31-alpine-slim
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /ui/dist /usr/share/nginx/html
EXPOSE 8080
