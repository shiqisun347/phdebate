FROM node:24-alpine AS dependencies
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install

FROM dependencies AS builder
COPY . .
RUN npm run build

FROM node:24-alpine AS runner
ENV NODE_ENV=production PORT=3300 HOSTNAME=0.0.0.0
WORKDIR /app
RUN addgroup -S agent && adduser -S agent -G agent
COPY --from=builder --chown=agent:agent /app/.next/standalone ./
COPY --from=builder --chown=agent:agent /app/.next/static ./.next/static
USER agent
CMD ["node", "server.js"]
