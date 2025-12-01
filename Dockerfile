# Dockerfile (fixed)
FROM python:3.11-slim

# Use a non-interactive frontend for apt
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV NODE_ENV=production

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
  && apt-get update && apt-get install -y --no-install-recommends nodejs \
  && rm -rf /var/lib/apt/lists/*

# Copy Python requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project (we need server/package.json to be present in build context if you want node deps)
COPY . .

# If server/package.json exists, install Node deps inside /app/server
RUN if [ -f ./server/package.json ]; then \
      echo "Found server/package.json — installing Node dependencies..."; \
      cd server && npm ci --only=production || npm install --production; \
    else \
      echo "No server/package.json found — skipping npm install"; \
    fi

# Expose the port your app uses (change if needed)
EXPOSE 5000

# Command to run the app
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "5000"]
