#!/bin/bash
# LEA M&A Sourcing Tool — Startup Script
# Usage: chmod +x run.sh && ./run.sh

set -e

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   LEA M&A Sourcing Tool — Starting Up        ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
  echo "❌ Python 3 not found. Install from python.org"
  exit 1
fi

cd "$(dirname "$0")/backend"

# Install deps
echo "📦 Installing dependencies..."
pip3 install -r requirements.txt -q --break-system-packages 2>/dev/null || \
pip3 install -r requirements.txt -q 2>/dev/null || \
echo "  (Some packages may already be installed)"

# Create .env if missing
if [ ! -f .env ]; then
  echo ""
  echo "⚙  Creating .env file (add API keys here later)"
  cat > .env << 'EOF'
# LEA Sourcing Tool — API Keys
# Add these to enable full scraping:

# NewsAPI for press coverage (free at newsapi.org)
NEWS_API_KEY=

# Yelp API — FREE, 500 req/day, best free source (yelp.com/developers)
YELP_API_KEY=

# Google Places API (get from console.cloud.google.com)
GOOGLE_API_KEY=

# SerpAPI for Google rank checking (serpapi.com)
SERP_API_KEY=

# Optional: SpyFu for ad spend data
SPYFU_API_KEY=
EOF
fi

echo ""
echo "🚀 Starting LEA backend on http://localhost:8000"
echo "   Open your browser to: http://localhost:8000"
echo "   API docs at: http://localhost:8000/docs"
echo ""
echo "   Press Ctrl+C to stop"
echo ""

python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
