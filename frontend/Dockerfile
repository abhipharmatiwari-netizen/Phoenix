# Stage 1: Build the React application
FROM node:18-alpine AS build

WORKDIR /app

# Copy dependency manifests.
COPY package*.json ./

# Install dependencies. Prefer the lockfile when it exists.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

# Copy the rest of the application code
COPY . .

# Build the application
RUN npm run build

# Stage 2: Serve the application
FROM node:18-alpine

WORKDIR /app

# Copy the built application from the build stage
COPY --from=build /app/build ./build
COPY package*.json ./

# Install runtime dependencies. Prefer the lockfile when it exists.
RUN if [ -f package-lock.json ]; then npm ci --omit=dev; else npm install --omit=dev; fi

# Expose the port the app runs on
EXPOSE 3000

# Serve the application
CMD ["npx", "serve", "-s", "build", "-l", "3000"]
