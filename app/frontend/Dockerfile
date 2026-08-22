FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
# Vite inlines VITE_* at BUILD time, so this cannot be a runtime environment
# variable — a container started with it set would still serve a bundle that
# never saw it. It comes in as a build arg from compose instead. Not a secret:
# the publishable key identifies the Clerk instance to the browser and is
# readable in any built bundle. The secret key must never appear here.
ARG VITE_CLERK_PUBLISHABLE_KEY
ENV VITE_CLERK_PUBLISHABLE_KEY=$VITE_CLERK_PUBLISHABLE_KEY
# Tests before the build, so a broken one stops the image the way a type error
# does. They are fast (jsdom, no network) and the alternative is a suite nobody
# runs.
RUN npm run test
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
