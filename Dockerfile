# Dockerfile (Python backend + Node helper)
FROM python:3.11-slim

# Use a non-interactive frontend for apt
ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install system dependencies (curl, ca-certificates, build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    build-essential \
    gcc \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 18 LTS from NodeSource
RUN curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Copy Python requirements first for caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy only server package.json/package-lock.json (if present) to leverage Docker cache
# This avoids reinstalling Node deps when other files change.
COPY server/package*.json ./server/ 2>/dev/null || true

# Install Node dependencies in server/ if package.json exists
RUN if [ -f ./server/package.json ]; then \
      cd server && npm ci --only=production || npm install --production ; \
    else \
      echo "No server/package.json found, skipping npm install"; \
    fi

# Copy the rest of the application code
COPY . .

# Expose the port your app uses
EXPOSE 5000

# Use production environment by default (optional)
ENV PYTHONUNBUFFERED=1
ENV NODE_ENV=production

# Command to run the app
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "5000"]
