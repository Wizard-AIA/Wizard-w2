# Frontend image.
#
# The runner stage previously copied the whole of `node_modules` out of the
# builder, which meant shipping every build-time dependency -- typescript,
# eslint, tailwind, the Next compiler -- into the production image to serve a
# handful of prerendered routes.
#
# `output: "standalone"` in next.config.ts makes Next trace which files the
# server actually loads at runtime and emit exactly those, so only that is
# copied here.
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS builder

WORKDIR /app

# corepack ships with Node 20; `prepare --activate` pins the exact pnpm build
# rather than trusting whatever "latest" resolves to on the day the image builds.
RUN corepack enable && corepack prepare pnpm@10.33.3 --activate

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .

RUN pnpm run build

# Production image.
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS runner
WORKDIR /app

ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# The standalone bundle carries its own minimal node_modules and a server.js.
# `static` and `public` are not traced into it -- Next expects them to be placed
# alongside, and their absence shows up as unstyled pages rather than a crash.
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

# Runs unprivileged: unlike the backend, nothing here needs the Docker socket.
USER node

EXPOSE 3000

CMD ["node", "server.js"]
