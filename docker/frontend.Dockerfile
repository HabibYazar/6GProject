# NSG platform UI (Next.js 16).
FROM node:22-slim AS build
WORKDIR /app

# --legacy-peer-deps is required, not cosmetic: the PDF export path (jsPDF and
# its fflate dependency) does not satisfy strict peer resolution here.
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --legacy-peer-deps

COPY ui/ ./

# NEXT_PUBLIC_* is inlined into the client bundle at BUILD time, so this cannot
# be a runtime environment variable — and it must be the URL a BROWSER can
# reach. The browser runs on the host, not inside the compose network, so this
# is the published host address (http://localhost:8000), never the service name
# `backend`. Change it with the NSG_PUBLIC_API_URL build arg when the platform
# is served under a hostname other than localhost.
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN npm run build

FROM node:22-slim
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app ./
EXPOSE 3000
CMD ["npm", "run", "start"]
